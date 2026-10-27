"""실제 SQLite 원본으로 화면·보고서 실행 후 남는 메모리를 확인한다."""
from contextlib import closing
from datetime import datetime, timezone
import gc
import json
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch
import os

from backend import workspace_data
from backend.reports.work_queries import build_work_query_report
from backend.reports.report_data import load_report_issues, report_scope_label
from backend.sources import mantis_store as store


class RequestMemoryRetentionTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "store.sqlite3"
        for module in (store, workspace_data):
            self.enterContext(patch.object(module, "store_path", return_value=self.path))
            self.enterContext(patch.object(module, "connection_key", return_value="memory-test"))
        store._generation_cache.clear()
        workspace_data.clear_workspace_snapshot_cache()
        self.addCleanup(store._generation_cache.clear)
        self.addCleanup(workspace_data.clear_workspace_snapshot_cache)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TABLE issues (id INTEGER PRIMARY KEY, payload TEXT)")
            db.execute("CREATE TABLE metadata (value TEXT)")
            db.execute("INSERT INTO metadata VALUES (?)", (json.dumps({
                "connection": "memory-test", "count": 600,
                "fetchedAt": datetime.now(timezone.utc).isoformat(),
            }),))
            for index in range(600):
                row = {"id": index, "summary": f"오류 {index}", "status": {"name": "assigned"},
                       "project": {"name": "테스트"}, "description": "본문 " * 8192,
                       "custom_fields": [{"field": {"id": field, "name": f"항목 {field}"},
                                          "value": f"값 {index}-{field}"} for field in range(30)]}
                db.execute("INSERT INTO issues VALUES (?, ?)", (index, json.dumps(row)))

    def measured(self, operation):
        gc.collect()
        tracemalloc.start()
        try:
            result = operation()
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            return result, current, peak
        finally:
            tracemalloc.stop()

    def test_workspace_retains_normalized_rows_without_raw_corpus(self):
        result, current, peak = self.measured(workspace_data.load_complete_workspace)
        self.assertEqual(len(result[0]), 600)
        self.assertEqual(result[0][0]["id"], 599)
        self.assertTrue(result[1]["search_complete"])
        self.assertLess(current, 6 * 1024 ** 2, "화면용 결과 외 전체 원본을 캐시하지 않아야 한다")
        self.assertLess(peak, 8 * 1024 ** 2, "전체 원본을 적재한 뒤 버리는 것도 피해야 한다")

    def test_filtered_report_releases_unrelated_bodies_between_requests(self):
        def run():
            return build_work_query_report("#3 원인", {"issue_ids": [3]}, operation="cause")
        for _ in range(3):
            result, current, peak = self.measured(run)
            self.assertEqual(result["retrieval"]["stages"][0]["meta"]["matched_ids"], [3])
            self.assertLess(current, 2 * 1024 ** 2, "요청이 끝난 뒤 전체 본문을 보관하지 않아야 한다")
            self.assertLess(peak, 4 * 1024 ** 2, "조건과 무관한 본문은 한 행씩 해제해야 한다")

    def test_report_source_keeps_full_coverage_without_materializing_rows(self):
        def unexpected_live(**kwargs):
            self.fail("유효한 전체 저장본 대신 라이브 일부만 조회하면 안 된다")

        def run():
            with store.store_request_scope():
                rows = load_report_issues(store.read_scoped_store, unexpected_live)
                self.assertEqual(len(rows), 600)
                self.assertIn("전체 600건", report_scope_label(rows))
                self.assertEqual(sum(1 for _ in rows), 600)
                self.assertEqual(sum(1 for _ in rows), 600)

        _, current, peak = self.measured(run)
        self.assertLess(current, 1024 ** 2)
        self.assertLess(peak, 3 * 1024 ** 2, "보고서 공통 로더도 전체 원본 목록을 만들면 안 된다")

    def test_scoped_reader_pins_generation_and_closes_after_exception(self):
        reader = None
        with self.assertRaisesRegex(ValueError, "취소"):
            with store.store_request_scope():
                reader = store.read_scoped_store(summary_only=True)
                self.assertEqual(next(iter(reader))["id"], 599)
                replacement = self.path.with_suffix(".next")
                with closing(sqlite3.connect(self.path)) as source, closing(sqlite3.connect(replacement)) as target:
                    source.backup(target)
                    with target:
                        target.execute("UPDATE issues SET payload=json_set(payload, '$.summary', '새 세대')")
                os.replace(replacement, self.path)
                self.assertEqual(next(iter(store.read_scoped_store(summary_only=True)))["summary"], "오류 599")
                raise ValueError("취소")
        with self.assertRaises(sqlite3.ProgrammingError):
            reader.db.execute("SELECT 1")
        with store.store_request_scope():
            self.assertEqual(next(iter(store.read_scoped_store(summary_only=True)))["summary"], "새 세대")

    def test_summary_and_full_readers_keep_their_distinct_fields(self):
        with store.store_request_scope():
            summary = next(iter(store.read_scoped_store(summary_only=True)))
            full = next(iter(store.read_scoped_store()))
            self.assertNotIn("description", summary)
            self.assertEqual(full["description"], "본문 " * 8192)
            self.assertEqual(summary["custom_fields"], full["custom_fields"])
        with self.assertRaisesRegex(RuntimeError, "store_request_scope"):
            store.read_scoped_store()
