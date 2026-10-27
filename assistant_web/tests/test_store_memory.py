"""저장본 교체 시 이전 세대·가변 사본이 겹쳐 적재되는 회귀를 검증한다."""
import gc
import json
import sqlite3
import tempfile
import tracemalloc
import unittest
import weakref
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from threading import Event
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.sources import mantis_store as store
from backend import workspace_data


class StoreMemoryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.path = Path(temp.name) / "store.sqlite3"
        for name, value in (("store_path", self.path), ("connection_key", "test")):
            mock = patch.object(store, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)
        store._generation_cache.clear()
        self.addCleanup(store._generation_cache.clear)
        self.addCleanup(workspace_data.clear_workspace_snapshot_cache)
        for name, value in (("store_path", self.path), ("connection_key", "test")):
            mock = patch.object(workspace_data, name, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def write_rows(self, count=1200):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE IF NOT EXISTS issues (id INTEGER PRIMARY KEY, payload TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (value TEXT)")
            db.execute("DELETE FROM issues")
            db.execute("DELETE FROM metadata")
            db.execute("INSERT INTO metadata VALUES (?)", (json.dumps({
                "connection": "test", "count": count,
                "fetchedAt": datetime.now(timezone.utc).isoformat(),
            }),))
            for index in range(count):
                row = {"id": index, "summary": f"이슈 {index}", "custom_fields": [
                    {"field": {"id": field, "name": f"업무 항목 {field}"}, "value": f"값 {index}-{field}"}
                    for field in range(12)
                ]}
                db.execute("INSERT INTO issues VALUES (?, ?)", (index, json.dumps(row)))

    def test_decoding_does_not_keep_a_second_mutable_copy_of_the_whole_snapshot(self):
        self.write_rows()
        gc.collect()
        tracemalloc.start()
        try:
            rows = store.read_complete_store(summary_only=True)
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(len(rows), 1200)
        self.assertLess(peak - current, 2 * 1024 * 1024, "전체 가변 사본을 동시에 보관하지 않아야 한다")
        with self.assertRaises(TypeError):
            rows[0]["custom_fields"][0]["field"]["name"] = "변조"

    def test_unreferenced_old_generation_is_released_before_loading_the_next(self):
        self.write_rows(1)
        previous = weakref.ref(store.read_complete_store()[0])
        self.write_rows(2)
        load = store._load_complete_store

        def check_release(**kwargs):
            gc.collect()
            self.assertIsNone(previous(), "새 세대 적재 전에 이전 캐시를 해제해야 한다")
            return load(**kwargs)

        with patch.object(store, "_load_complete_store", side_effect=check_release):
            self.assertEqual(len(store.read_complete_store()), 2)

    def test_full_and_summary_views_share_fields_without_dropping_searchable_text(self):
        for first_summary in (True, False):
            store._generation_cache.clear()
            self.write_rows(1)
            with closing(sqlite3.connect(self.path)) as db, db:
                db.execute("UPDATE issues SET payload=json_set(payload, '$.description', '본문', '$.notes', json(?))",
                           ('[{"text":"검색할 메모"}]',))
            first = store.read_complete_store(summary_only=first_summary)
            second = store.read_complete_store(summary_only=not first_summary)
            full, summary = (second, first) if first_summary else (first, second)
            self.assertIs(full[0]["custom_fields"], summary[0]["custom_fields"])
            self.assertEqual(full[0]["description"], "본문")
            self.assertEqual(full[0]["notes"][0]["text"], "검색할 메모")
            self.assertNotIn("description", summary[0])
            self.assertNotIn("notes", summary[0])

    def test_replacement_during_second_view_load_never_returns_mixed_generations(self):
        self.write_rows(1)
        summary = store.read_complete_store(summary_only=True)
        load = store._load_complete_store

        def replace_before_read(**kwargs):
            self.write_rows(2)
            return load(**kwargs)

        with patch.object(store, "_load_complete_store", side_effect=replace_before_read):
            self.assertIsNone(store.read_complete_store())
        current = store.read_complete_store()
        self.assertEqual([row["id"] for row in current], [1, 0])
        self.assertEqual([row["id"] for row in summary], [0])
        self.assertIsNot(current[1]["custom_fields"], summary[0]["custom_fields"])

    def test_simultaneous_workspace_requests_normalize_one_shared_generation(self):
        from backend.sources import mantis
        self.write_rows(1)
        first_entered, second_entered, release = Event(), Event(), Event()
        original = mantis.normalize_issue

        def normalize(row):
            (second_entered if first_entered.is_set() else first_entered).set()
            release.wait(2)
            return original(row)

        with patch.object(mantis, "normalize_issue", side_effect=normalize) as convert:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(workspace_data.load_complete_workspace)
                self.assertTrue(first_entered.wait(2))
                second = pool.submit(workspace_data.load_complete_workspace)
                second_entered.wait(0.1)
                release.set()
                left, right = first.result(), second.result()
            self.assertEqual(convert.call_count, 1)
            self.assertIs(left[0][0], right[0][0])

    def test_workspace_releases_its_old_normalized_cache_before_reading_new_rows(self):
        self.write_rows(1)
        previous = weakref.ref(workspace_data.load_complete_workspace()[0][0])
        self.write_rows(2)
        read = workspace_data.read_scoped_store

        def check_release(**kwargs):
            self.assertIsNone(previous())
            return read(**kwargs)

        with patch.object(workspace_data, "read_scoped_store", side_effect=check_release):
            self.assertEqual(len(workspace_data.load_complete_workspace()[0]), 2)
