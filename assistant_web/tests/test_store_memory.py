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

    def write_large_text_rows(self, count=400):
        self.write_rows(count)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE issues SET payload=json_set(payload, '$.description', ?, '$.notes', json(?))",
                       ("stream target", json.dumps([{"text": "x" * 32000}])))


    def test_snapshot_search_counts_every_match_without_retaining_every_body(self):
        from backend.common import normalize_chroma_issue_document
        from backend.config import CHROMA_DB_PATH
        from backend.tools.mantis_snapshot import scan_mantis_documents
        self.write_large_text_rows()
        gc.collect()
        tracemalloc.start()
        try:
            result = scan_mantis_documents(CHROMA_DB_PATH, lambda text: "stream target" in text,
                                          normalize_chroma_issue_document, example_limit=3)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result["matched_count"], 399)
        self.assertEqual([row["id"] for row in result["issues"]], [399, 398, 397])
        self.assertTrue(result["search_complete"])
        self.assertLess(peak, 3 * 1024 * 1024, "검색 표시 건수와 무관한 본문은 스트림으로 처리해야 한다")


    def test_page_search_keeps_all_match_metadata_but_only_the_best_bodies(self):
        from backend.sources.mantis import execute_mantis_page_scan
        self.write_large_text_rows()
        gc.collect()
        tracemalloc.start()
        try:
            result = execute_mantis_page_scan("stream target", limit=3)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result["meta"]["found_count"], 400)
        self.assertEqual(result["meta"]["matched_issue_ids"], list(range(1, 400)))
        self.assertEqual(len(result["meta"]["matched_customer_groups"]), 399)
        self.assertEqual([row["id"] for row in result["issues_list"]], [399, 398, 397])
        self.assertTrue(result["meta"]["search_complete"])
        self.assertLess(peak, 3 * 1024 * 1024, "일치한 모든 본문을 결과 대기열에 남기지 않아야 한다")


    def test_stream_scope_pins_generation_and_closes_on_request_exit(self):
        self.write_rows(1)
        replacement = self.path.with_name("replacement.sqlite3")
        with store.store_request_scope():
            with store.stream_complete_store() as first:
                self.assertEqual(len(first), 1)
                self.assertEqual([row["id"] for row in first], [0])
                with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(replacement)) as dest:
                    source.backup(dest)
                with closing(sqlite3.connect(replacement)) as db, db:
                    db.execute("UPDATE issues SET id=8, payload=json_set(payload, '$.id', 8)")
                replacement.replace(self.path)
            with store.stream_complete_store() as second:
                self.assertEqual([row["id"] for row in second], [0])
                self.assertEqual(second.meta, first.meta)
        with self.assertRaises(sqlite3.ProgrammingError):
            list(first)
        with store.stream_complete_store() as latest:
            self.assertEqual([row["id"] for row in latest], [8])


    def test_stream_rejects_wrong_connection_expiry_and_incomplete_count(self):
        self.write_rows(1)
        with patch.object(store, "connection_key", return_value="other"):
            with store.stream_complete_store() as rows:
                self.assertIsNone(rows)
        with patch.object(store, "MAX_AGE_SECONDS", -1):
            with store.stream_complete_store() as rows:
                self.assertIsNone(rows)
            with store.stream_complete_store(allow_stale=True) as rows:
                self.assertFalse(rows.meta["search_complete"])
                self.assertEqual(rows.meta["incomplete_reasons"], ["stale_snapshot"])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DELETE FROM issues")
        with store.stream_complete_store(allow_stale=True) as rows:
            self.assertIsNone(rows)


    def test_cancelled_stream_stops_before_decoding_another_row(self):
        from backend.execution import execution_scope, RequestStopped
        self.write_rows(3)
        with store.stream_complete_store() as rows, execution_scope() as budget:
            iterator = iter(rows)
            self.assertEqual(next(iterator)["id"], 2)
            budget.cancel()
            with self.assertRaises(RequestStopped):
                next(iterator)


    def test_partly_consumed_stream_can_close_after_its_connection(self):
        self.write_rows(3)
        with store.stream_complete_store() as rows:
            iterator = iter(rows)
            self.assertEqual(next(iterator)["id"], 2)
        iterator.close()


    def test_parallel_scans_share_one_request_generation_without_mixing_cursors(self):
        from contextvars import copy_context
        self.write_rows(30)
        def scan():
            with store.stream_complete_store() as rows:
                return [row["id"] for row in rows]
        with store.store_request_scope(), ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(copy_context().run, scan) for _ in range(2)]
            self.assertEqual([future.result() for future in futures], [list(range(29, -1, -1))] * 2)
