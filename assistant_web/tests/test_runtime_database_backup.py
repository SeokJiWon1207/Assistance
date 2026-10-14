import json
import sqlite3
from contextlib import closing
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import runtime_databases

from scripts.runtime_databases import backup_runtime


class RuntimeDatabaseBackupTests(unittest.TestCase):
    def test_backup_ignores_unpublished_snapshot_generation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state, backup = root / 'state', root / 'backup'
            state.mkdir()
            for name in ('users.sqlite3', 'mantis_complete.sqlite3'):
                with sqlite3.connect(state / name) as db:
                    db.execute('CREATE TABLE evidence(value TEXT)')
                    db.execute('INSERT INTO evidence VALUES (?)', (name,))
            # 원자 게시 전의 수집 파일은 정식 DB가 아니며 hot journal을 가질 수 있다.
            (state / 'mantis-complete-in-progress.sqlite3').write_bytes(b'not-yet-published')
            backup_runtime(state, backup)
            self.assertEqual(json.loads((backup / 'manifest.json').read_text()), ['mantis_complete.sqlite3', 'users.sqlite3'])
            self.assertFalse((backup / 'mantis-complete-in-progress.sqlite3').exists())
            for name in ('users.sqlite3', 'mantis_complete.sqlite3'):
                with sqlite3.connect(backup / name) as db:
                    self.assertEqual(db.execute('SELECT value FROM evidence').fetchone(), (name,))

    def test_preflight_uses_private_state_disk_and_removes_copies_on_success_and_failure(self):
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as temporary:
                state = Path(temporary) / "state"
                state.mkdir()
                original = state / "users.sqlite3"
                with closing(sqlite3.connect(original)) as db, db:
                    db.execute("CREATE TABLE evidence(value TEXT)")
                    db.execute("INSERT INTO evidence VALUES ('original')")
                copied_locations = []

                def migrate(_app, copied, *, locations=copied_locations, state=state, fail=fail):
                    locations.append(copied)
                    self.assertEqual(copied.parent.parent, state)
                    self.assertEqual(copied.parent.stat().st_mode & 0o777, 0o700)
                    self.assertEqual((copied / "users.sqlite3").stat().st_mode & 0o777, 0o600)
                    with closing(sqlite3.connect(copied / "users.sqlite3")) as db, db:
                        self.assertEqual(db.execute("SELECT value FROM evidence").fetchone(), ("original",))
                        db.execute("UPDATE evidence SET value='migrated'")
                    if fail:
                        raise RuntimeError("migration failed")

                argv = ["runtime_databases.py", "check", "--state", str(state), "--app", str(Path(temporary))]
                with patch("sys.argv", argv), patch.object(runtime_databases, "initialize_runtime", side_effect=migrate):
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "migration failed"):
                            runtime_databases.main()
                    else:
                        runtime_databases.main()
                self.assertFalse(copied_locations[0].parent.exists())
                with closing(sqlite3.connect(original)) as db:
                    self.assertEqual(db.execute("SELECT value FROM evidence").fetchone(), ("original",))
