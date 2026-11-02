"""DB별 연결 옵션, 호출자 트랜잭션 경계와 스키마 소유권 계약."""
import ast
import importlib
import json
import sqlite3
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from architecture_graph import collect_python_edges, strongly_connected_components
from backend.db_migrations import run_sqlite_migrations

APP = Path(__file__).resolve().parents[1]


class StorageContractTests(unittest.TestCase):
    def storage(self):
        self.assertTrue((APP / 'backend/storage/sqlite.py').exists(), '연결 정책 소유자가 필요합니다')
        return importlib.import_module('backend.storage.sqlite')

    def test_backend_has_no_runtime_cycles(self):
        self.assertEqual(strongly_connected_components(collect_python_edges(APP / 'backend')), [])

    def test_only_connection_owner_calls_sqlite_connect(self):
        # 별칭을 통한 connect도 놓치지 않도록 AST import 이름을 함께 해석한다.
        paths = [path for directory in (APP / 'backend', APP / 'scripts', APP.parent / 'scripts')
                 for path in directory.rglob('*.py')]
        self.assertTrue(paths)
        for path in paths:
            if path == APP / 'backend/storage/sqlite.py':
                continue
            tree = ast.parse(path.read_text())
            aliases = {'sqlite3': 'sqlite3'}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    aliases.update({a.asname or a.name: a.name for a in node.names})
                if isinstance(node, ast.ImportFrom) and node.module == 'sqlite3':
                    aliases.update({a.asname or a.name: 'sqlite3.' + a.name for a in node.names})
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    text = ast.unparse(node.func)
                    head, _, tail = text.partition('.')
                    resolved = aliases.get(head, head) + ('.' + tail if tail else '')
                    self.assertNotEqual(resolved, 'sqlite3.connect', str(path))

    def test_workspace_owners_do_not_import_services_or_reports(self):
        self.storage()
        edges = collect_python_edges(APP / 'backend')
        self.assertEqual([(e.importer, e.imported) for e in edges
                          if e.importer.startswith('backend.storage.') and
                          (e.imported == 'backend.workspace' or e.imported.startswith(('backend.reports.', 'backend.services.')))], [])

    def test_legacy_workspace_and_brief_paths_share_their_owners(self):
        self.storage()
        from backend import workspace, conversation_history, workspace_notifications
        from backend.reports import team_brief
        from backend.storage import workspace_schema, workspace_repository, workspace_rows
        from backend.utils.dates import local_timestamp
        for name in ('workspace_db_path', 'init_workspace_db', '_create_workspace_schema', '_create_team_brief_schema', '_create_playbook_schema'):
            self.assertIs(getattr(workspace, name), getattr(workspace_schema, name))
        for name in ('_issue_summary', '_is_open', '_parse_date', '_due_date'):
            self.assertIs(getattr(workspace, name), getattr(workspace_rows, name))
        self.assertIs(workspace._now, local_timestamp)
        self.assertIs(team_brief.mark_weekly_team_brief_read, workspace_repository.mark_weekly_team_brief_read)
        self.assertIs(conversation_history.migrate_messages, workspace_schema.migrate_messages)
        self.assertIs(workspace_notifications.create_notification_schema, workspace_schema.create_notification_schema)

    def test_default_connection_leaves_commit_close_and_file_permissions_to_caller(self):
        storage = self.storage()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'contract.sqlite3'
            with closing(sqlite3.connect(path)) as original:
                original.execute('CREATE TABLE records(value TEXT)')
            path.chmod(0o640)
            db = storage.open_sqlite(path, profile=storage.ConnectionProfile())
            try:
                self.assertEqual(db.autocommit, sqlite3.LEGACY_TRANSACTION_CONTROL)
                self.assertEqual(db.isolation_level, '')
                self.assertFalse(db.in_transaction)
                self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)
                self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'delete')
                db.execute("INSERT INTO records VALUES('pending')")
                with closing(sqlite3.connect(path)) as observer:
                    self.assertEqual(observer.execute('SELECT * FROM records').fetchall(), [])
                db.rollback()
                with self.assertRaisesRegex(ValueError, 'rollback'), db:
                    db.execute("INSERT INTO records VALUES('rolled back')")
                    raise ValueError('rollback')
                self.assertEqual(db.execute('SELECT * FROM records').fetchall(), [])
                with db:
                    db.execute("INSERT INTO records VALUES('committed')")
                # with connection은 commit만 담당하고 닫지는 않는다.
                self.assertEqual(db.execute('SELECT * FROM records').fetchall(), [('committed',)])
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o640)
            finally:
                db.close()
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')

    def test_read_uri_rows_timeout_and_pragmas_are_profile_specific(self):
        storage = self.storage()
        from backend.storage.profiles import NOTION_BODY, MANTIS_STORE_STREAM
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'profile.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('CREATE TABLE records(value TEXT)')
            with closing(storage.open_sqlite(path.as_uri() + '?mode=ro', profile=NOTION_BODY)) as db:
                row = db.execute('SELECT 1 AS value').fetchone()
                self.assertIsInstance(row, sqlite3.Row)
                self.assertEqual(row['value'], 1)
                self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 30000)
                self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
                self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0], 2)
                self.assertFalse(db.in_transaction)
                with self.assertRaises(sqlite3.OperationalError):
                    db.execute("INSERT INTO records VALUES('forbidden')")
            with closing(storage.open_sqlite(path.as_uri() + '?mode=ro', profile=MANTIS_STORE_STREAM)) as db:
                with ThreadPoolExecutor(max_workers=1) as pool:
                    self.assertEqual(pool.submit(lambda: db.execute('SELECT 1').fetchone()[0]).result(), 1)
                self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)

    def test_failed_profile_setup_closes_connection_without_changing_exception(self):
        storage = self.storage()
        original = sqlite3.connect
        opened = []
        def connect(*args, **kwargs):
            db = original(*args, **kwargs)
            opened.append(db)
            return db
        with patch.object(storage.sqlite3, 'connect', side_effect=connect):
            with self.assertRaises(sqlite3.OperationalError):
                storage.open_sqlite(':memory:', profile=storage.ConnectionProfile(pragmas=('invalid SQL',)))
        self.assertEqual(len(opened), 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            opened[0].execute('SELECT 1')

    def test_empty_and_legacy_databases_match_pre_refactor_schema_rows_and_permissions(self):
        from storage_contract_cases import migration_cases
        expected = json.loads((APP / 'tests/fixtures/storage_migrations_golden.json').read_text())
        self.assertEqual(migration_cases(), expected)

    def test_brief_first_writer_refresh_and_user_read_state_are_preserved(self):
        self.storage()
        from backend.storage import workspace_repository as repository, workspace_schema
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            'os.environ', {'WORKSPACE_DB_PATH': str(Path(directory) / 'brief.sqlite3')}
        ):
            workspace_schema.init_workspace_db()
            self.assertIsNone(repository.read_team_brief('2026-11-02'))
            self.assertEqual(repository.store_team_brief('2026-11-02', '{"first":1}', 'first', replace=False), ('{"first":1}',))
            self.assertEqual(repository.store_team_brief('2026-11-02', '{"second":2}', 'second', replace=False), ('{"first":1}',))
            self.assertEqual(repository.store_team_brief('2026-11-02', '{"refresh":3}', 'third', replace=True), ('{"refresh":3}',))
            self.assertEqual(repository.read_team_brief('2026-11-02')['payload_json'], '{"refresh":3}')
            self.assertIsNone(repository.read_team_brief('2026-11-02', refresh=True))
            repository.mark_weekly_team_brief_read('user-a', '2026-11-02')
            self.assertTrue(repository.team_brief_viewed('user-a', '2026-11-02'))
            self.assertFalse(repository.team_brief_viewed('user-b', '2026-11-02'))

    def test_repository_closes_connections_after_reads_and_failed_writes(self):
        storage = self.storage()
        from backend.storage import workspace_repository as repository, workspace_schema
        opened = []
        def connect(*args, **kwargs):
            db = storage.open_sqlite(*args, **kwargs)
            opened.append(db)
            return db
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            'os.environ', {'WORKSPACE_DB_PATH': str(Path(directory) / 'brief.sqlite3')}
        ), patch.object(repository, 'open_sqlite', side_effect=connect):
            workspace_schema.init_workspace_db()
            repository.read_team_brief('2026-11-02')
            with self.assertRaises(sqlite3.IntegrityError):
                repository.store_team_brief('2026-11-02', None, 'invalid', replace=True)
            self.assertFalse(repository.team_brief_viewed('user-a', '2026-11-02'))
        self.assertEqual(len(opened), 3)
        for db in opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute('SELECT 1')

    def test_failed_migration_rolls_back_ddl_rows_and_version_then_can_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollback.sqlite3'
            def first(db):
                db.execute('CREATE TABLE records(value TEXT)')
                db.execute("INSERT INTO records VALUES('kept')")
            def failed(db):
                db.execute('ALTER TABLE records ADD COLUMN owner TEXT')
                db.execute("UPDATE records SET value='lost'")
                raise RuntimeError('migration failure')
            with self.assertRaisesRegex(RuntimeError, 'migration failure'):
                run_sqlite_migrations(path, [first, failed], database_name='test')
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
                self.assertEqual(db.execute('SELECT * FROM records').fetchall(), [('kept',)])
            def second(db):
                db.execute("ALTER TABLE records ADD COLUMN owner TEXT DEFAULT 'owner'")
            run_sqlite_migrations(path, [first, second], database_name='test')
            run_sqlite_migrations(path, [first, second], database_name='test')
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT * FROM records').fetchall(), [('kept', 'owner')])
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)

    def test_concurrent_start_applies_each_non_idempotent_migration_once(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'concurrent.sqlite3'
            barrier = Barrier(4)
            def migration(db):
                db.execute('CREATE TABLE records(value TEXT)')
                db.execute("INSERT INTO records VALUES('once')")
            def initialize():
                barrier.wait(timeout=5)
                run_sqlite_migrations(path, [migration], database_name='test')
            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(lambda _: initialize(), range(4)))
            with closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT * FROM records').fetchall(), [('once',)])
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
