"""제목 조회를 위한 목록을 요청 밖에서 갱신하고 잠금·용량·실패 경계를 지킨다."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.notion import snapshot_maintenance as maintenance
from backend.notion.body_index import BodyIndex
from backend.notion.body_index_sync import _ReadFailure
from test_notion_body_index import page


class NotionCatalogRefreshTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.index = BodyIndex(self.root / 'index.sqlite3', fingerprint='connection')
        self.index.upsert_catalog('db', [page('p', database='db')], complete=True)
        self.enterContext(patch('backend.config.RUNTIME_DIR', self.root))
        self.enterContext(patch('backend.config.get_notion_settings', return_value={
            'token': 'fixture-token', 'database_ids': ['db']}))
        self.enterContext(patch('backend.notion.read_scope.ensure_notion_read_scope', return_value=({'db': 'db'}, True)))
        self.enterContext(patch('backend.notion.body_index.open_body_index', return_value=self.index))

    def refresh(self):
        refresh = getattr(maintenance, 'refresh_catalog_cache', None)
        self.assertTrue(callable(refresh), '요청 밖에서 목록과 하위 문서 속성을 갱신하는 경로가 필요합니다')
        return refresh()

    def test_fresh_catalog_needs_no_network_refresh(self):
        with patch.object(maintenance, 'sync_notion_body_index') as sync:
            self.assertEqual(self.refresh()['status'], 'fresh')
        sync.assert_not_called()

    def test_stale_catalog_refreshes_all_databases_without_crawling_bodies(self):
        with closing(sqlite3.connect(self.index.path)) as db, db:
            db.execute("UPDATE catalogs SET updated_at='2020-01-01T00:00:00+00:00'")
        def sync(database_ids, **kwargs):
            self.assertEqual(database_ids, ['db'])
            self.assertEqual(kwargs['max_pages'], 0)
            self.index.upsert_catalog('db', [page('new', database='db')], complete=True)
        with patch.object(maintenance, 'sync_notion_body_index', side_effect=sync):
            self.assertEqual(self.refresh()['status'], 'refreshed')
        with self.index._database() as db:
            self.assertEqual([row[0] for row in db.execute('SELECT page_id FROM pages')], ['new'])

    def test_active_snapshot_maintenance_is_not_started_twice(self):
        import fcntl
        with (self.root / 'notion-snapshot.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with patch.object(maintenance, 'sync_notion_body_index') as sync:
                self.assertEqual(self.refresh()['status'], 'already_running')
            sync.assert_not_called()

    def test_low_disk_space_keeps_existing_catalog(self):
        from collections import namedtuple
        usage = namedtuple('usage', 'total used free')(1024, 1023, 1)
        with patch('shutil.disk_usage', return_value=usage), patch.object(maintenance, 'sync_notion_body_index') as sync:
            self.assertEqual(self.refresh()['status'], 'insufficient_disk_space')
        sync.assert_not_called()

    def test_descendant_metadata_is_reverified_without_reading_its_body(self):
        parent = page('p', database='db')
        child = page('child', title='배포 계획', database='db')
        child['parent'] = {'type': 'page_id', 'page_id': 'p'}
        self.index.upsert_page('db', child, parent_pages={'p': parent})
        with closing(sqlite3.connect(self.index.path)) as db, db:
            db.execute("UPDATE pages SET updated_at='2020-01-01T00:00:00+00:00' WHERE page_id='child'")
        calls = []
        def request(endpoint, *_args, **_kwargs):
            calls.append(endpoint)
            return {'pages/p': parent, 'pages/child': child}[endpoint]
        with (patch.object(maintenance, '_request', side_effect=request),
              patch('backend.notion.body_index_sync._request', side_effect=request)):
            result = self.refresh()
        self.assertEqual(result['status'], 'refreshed')
        self.assertEqual(calls, ['pages/child', 'pages/p'])
        with self.index._database() as db:
            raw = json.loads(db.execute("SELECT raw_page FROM pages WHERE page_id='child'").fetchone()[0])
        self.assertEqual(raw['properties']['제목']['title'][0]['plain_text'], '배포 계획')

    def test_transient_child_refresh_failure_does_not_renew_its_timestamp(self):
        parent = page('p', database='db')
        child = page('child', database='db')
        child['parent'] = {'type': 'page_id', 'page_id': 'p'}
        self.index.upsert_page('db', child, parent_pages={'p': parent})
        with closing(sqlite3.connect(self.index.path)) as db, db:
            db.execute("UPDATE pages SET updated_at='2020-01-01T00:00:00+00:00' WHERE page_id='child'")
        with patch.object(maintenance, '_request', side_effect=_ReadFailure('http_429')):
            self.assertEqual(self.refresh()['status'], 'incomplete')
        with self.index._database() as db:
            self.assertEqual(db.execute("SELECT updated_at FROM pages WHERE page_id='child'").fetchone()[0],
                             '2020-01-01T00:00:00+00:00')
