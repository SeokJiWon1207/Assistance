"""검색 완료 여부를 추정하지 않고 시점·부분 조회를 답변에 전달한다."""

import unittest

from backend.evaluation.data_scope import build_answer_data_scope


class AnswerDataScopeTests(unittest.TestCase):
    def test_snapshot_date_and_partial_coverage_are_preserved(self):
        rows = build_answer_data_scope({"stages": [{"source": "mantis_snapshot", "status": "ok", "meta": {
            "fetchedAt": "2026-10-06T00:00:00Z", "search_complete": False,
        }}]})
        self.assertEqual(rows[0]["basis"], "저장본")
        self.assertEqual(rows[0]["coverage"], "부분 조회")
        self.assertEqual(rows[0]["asOf"], "2026-10-06T00:00:00Z")
        self.assertTrue(rows[0]["warning"])

    def test_unknown_coverage_is_not_reported_as_complete(self):
        rows = build_answer_data_scope({"stages": [{"source": "mantis_customer_profile", "status": "ok"}]})
        self.assertEqual(rows[0]["coverage"], "범위 미확인")

    def test_notion_cache_and_incomplete_body_remain_visible_after_search(self):
        rows = build_answer_data_scope({"stages": [{"source": "notion", "status": "ok", "meta": {
            "search_complete": True, "database_ids": ["a", "b"], "content_complete": False,
            "query_cache_hit": True, "query_cache_age_seconds": 15.5,
        }}]})
        self.assertEqual(rows[0]["coverage"], "선택 범위 조회 완료")
        self.assertEqual(rows[0]["databaseCount"], 2)
        self.assertTrue(rows[0]["warning"])
        self.assertIn("15.5초", rows[0]["notes"][1])

    def test_unapplied_filter_is_visible_even_when_database_scan_completed(self):
        rows = build_answer_data_scope({"stages": [{"source": "notion", "status": "ok", "meta": {
            "search_complete": True, "filter_audit": {"unapplied_filters": [{"field": "customer"}]},
        }}]})
        self.assertEqual(rows[0]["coverage"], "부분 조회")
        self.assertIn("일부 검색 조건을 적용하지 못했습니다.", rows[0]["notes"])
