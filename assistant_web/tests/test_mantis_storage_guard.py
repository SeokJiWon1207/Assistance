"""실제 임시 DB·잠금으로 자동 정리와 공간 부족 시 이전 세대 보존을 검증한다."""
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
import fcntl
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from backend.sources import mantis_store as store


class MantisStorageGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(self.directory) / 'mantis_complete.sqlite3'
        self.enterContext(patch.object(store, 'store_path', return_value=self.path))
        self.enterContext(patch.object(store, 'connection_key', return_value='fixture'))
        self.free = 10 * 1024**3
        self.enterContext(patch('shutil.disk_usage', side_effect=lambda _: SimpleNamespace(free=self.free)))
        self.sync()

    def sync(self, fetch=None):
        return store.sync_complete_store(fetch or (lambda page: [{'id': 1}] if page == 1 else []), workers=1)

    def orphan(self, suffix='.sqlite3'):
        p = self.path.parent / ('mantis-complete-abcdefgh' + suffix)
        p.write_bytes(b'orphan')
        return p

    def test_removes_only_owned_regular_temporary_files(self):
        doomed = [self.orphan(s) for s in ('.sqlite3', '.sqlite3-journal', '.sqlite3-wal', '.sqlite3-shm')]
        kept = [self.path.parent / n for n in ('users.sqlite3', 'mantis-complete-abcdefgh.sqlite3.gz', 'mantis-complete-notours.sqlite3')]
        for p in kept:
            p.write_bytes(b'keep')
        link = self.path.parent / 'mantis-complete-linklink.sqlite3'
        link.symlink_to(kept[0])
        self.assertEqual(self.sync()['status'], 'complete')
        self.assertFalse(any(p.exists() for p in doomed))
        self.assertTrue(link.is_symlink())
        self.assertTrue(all(p.read_bytes() == b'keep' for p in kept))
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT id FROM issues').fetchall(), [(1,)])

    def test_lock_owner_is_required_before_cleanup_or_capacity_check(self):
        orphan = self.orphan()
        self.free = 1
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for call in (self.sync, store.refresh_changed_issues):
                self.assertEqual(call()['status'], 'running')
                self.assertTrue(orphan.exists())

    def test_low_space_defers_both_writers_without_fetching_or_changing_snapshot(self):
        original = self.path.read_bytes()
        self.free = 1
        fetched = []
        def fetch(page):
            fetched.append(page)
            return []
        for call in (lambda: self.sync(fetch), lambda: store.refresh_changed_issues(fetch)):
            result = call()
            self.assertEqual(result['status'], 'deferred')
            self.assertEqual(result['reason'], 'insufficient_disk_space')
            self.assertGreater(result['requiredBytes'], result['freeBytes'])
            self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(fetched, [])

    def test_cleanup_recovers_capacity_before_deciding_to_defer(self):
        orphan = self.orphan()
        with patch('shutil.disk_usage', side_effect=lambda _: SimpleNamespace(free=1 if orphan.exists() else 10*1024**3)):
            self.assertEqual(self.sync()['status'], 'complete')
        self.assertFalse(orphan.exists())

    def test_failed_attempt_cleans_sqlite_sidecars_and_preserves_previous_generation(self):
        original = self.path.read_bytes()
        def fail(_):
            temporary = next(self.path.parent.glob('mantis-complete-*.sqlite3'))
            Path(str(temporary) + '-shm').write_bytes(b'leftover')
            raise RuntimeError('fixture failure')
        with self.assertRaises(RuntimeError):
            self.sync(fail)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('mantis-complete-*')), [])

    def test_space_exhaustion_during_collection_preserves_previous_generation(self):
        original = self.path.read_bytes()
        def fetch(_):
            self.free = 1
            return [{'id': 2}]
        with self.assertRaises(OSError):
            self.sync(fetch)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('mantis-complete-*')), [])

    def test_next_refresh_retries_after_capacity_recovers(self):
        self.free = 1
        self.assertEqual(store.refresh_store(lambda _: [])['status'], 'deferred')
        self.free = 10 * 1024**3
        result = store.refresh_store(lambda _: [{'id': 2, 'updated_at': '2026-10-14T23:00:00+00:00'}])
        self.assertEqual(result['status'], 'complete')
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute('SELECT id FROM issues ORDER BY id').fetchall(), [(1,), (2,)])

    def test_space_exhaustion_during_incremental_backup_preserves_generation(self):
        original = self.path.read_bytes()
        def fetch(_):
            self.free = 1
            return []
        with self.assertRaises(OSError):
            store.refresh_changed_issues(fetch)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.glob('mantis-complete-*')), [])

    def test_required_space_scales_with_existing_snapshot(self):
        from backend.sources.mantis_store_storage import store_write_guard
        with self.path.open('r+b') as file:
            file.truncate(400 * 1024**2)
        self.free = 500 * 1024**2
        with store_write_guard(self.path) as result:
            self.assertEqual(result['status'], 'deferred')
            self.assertGreater(result['requiredBytes'], self.free)

    def test_initial_sync_defers_without_creating_partial_snapshot(self):
        self.path.unlink()
        self.free = 1
        self.assertEqual(self.sync()['status'], 'deferred')
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.glob('mantis-complete-*')), [])

    @unittest.skipUnless(hasattr(store, 'upgrade_search_projection'), '운영 검색 투영 경로 전용')
    def test_projection_upgrade_obeys_same_lock_and_space_guard(self):
        original = self.path.read_bytes()
        orphan = self.orphan()
        self.free = 1
        with self.path.with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(store.upgrade_search_projection()['status'], 'running')
            self.assertTrue(orphan.exists())
        self.assertEqual(store.upgrade_search_projection()['status'], 'deferred')
        self.assertFalse(orphan.exists())
        self.assertEqual(self.path.read_bytes(), original)
