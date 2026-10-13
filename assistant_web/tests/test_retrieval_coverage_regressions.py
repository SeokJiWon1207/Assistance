"""전체 원본 재조회가 끝나면 오래된 인덱스를 최종 근거에 섞지 않는다."""

import unittest
from unittest.mock import patch

from backend import retrieval
from backend.answers import format_deterministic_issue_answer
from scripts.accuracy.dynamic_accuracy_check import AccuracyCase, grade


class SupersededSnapshotTests(unittest.TestCase):
    def search(self, *, complete, latest_ids):
        old = {"source": "mantis_snapshot", "status": "ok", "issues_list": [
            {"id": 100, "summary": "삭제된 저장본", "project": "NH증권"}],
            "meta": {"tool": "mantis_chroma_snapshot", "search_complete": False,
                     "matched_issue_ids": [100], "found_count": 1}}
        fresh = {"source": "mantis_page_scan", "status": "ok", "issues_list": [
            {"id": issue_id, "summary": "현재 원본", "project": "NH증권"} for issue_id in latest_ids],
            "meta": {"search_complete": complete, "matched_issue_ids": latest_ids,
                     "scanned_issue_ids": latest_ids, "found_count": len(latest_ids)}}
        with (
            patch.object(retrieval, "execute_mantis_snapshot_search", return_value=old),
            patch.object(retrieval, "revalidate_mantis_snapshot_stage", side_effect=lambda stage, _: stage),
            patch.object(retrieval, "get_mantis_snapshot_freshness", return_value="2026-08-19"),
            patch.object(retrieval, "execute_mantis_filter_search", return_value={
                "source": "mantis_filter", "status": "skipped", "meta": {}}),
            patch.object(retrieval, "execute_mantis_page_scan", return_value=fresh),
        ):
            return retrieval.run_retrieval_pipeline("NH증권 최근 이슈 2개 보여줘", 2,
                                                    {"mantis": True}, {"source": "mantis"})

    def test_complete_scan_supersedes_old_snapshot_in_sources_count_and_grading(self):
        result = self.search(complete=True, latest_ids=[200])
        sources = retrieval.build_sources(result)
        self.assertEqual([source["issue_id"] for source in sources], [200])
        self.assertEqual(retrieval.count_retrieved_issues(result), 1)
        answer = format_deterministic_issue_answer(result["query"], result["constraints"], result, 2)
        self.assertNotIn("전체 건수와 최신순은 확정할 수 없습니다", answer)
        case = AccuracyCase("latest", "mantis", "최신", result["query"], "ordered_issue_ids", [200])
        self.assertTrue(grade(case, {"content": answer, "sources": sources, "retrieval": result})["passed"])
        self.assertEqual(result["superseded_stages"][0]["source"], "mantis_snapshot")

    def test_complete_empty_scan_does_not_revive_a_deleted_snapshot_row(self):
        result = self.search(complete=True, latest_ids=[])
        self.assertEqual(retrieval.build_sources(result), [])
        self.assertEqual(retrieval.count_retrieved_issues(result), 0)

    def test_partial_scan_keeps_snapshot_coverage_warning(self):
        result = self.search(complete=False, latest_ids=[200])
        self.assertEqual({row["id"] for row in retrieval.collect_retrieved_issues(result, None)}, {100, 200})
        self.assertFalse(result.get("superseded_stages"))
        self.assertTrue(any(stage.get("meta", {}).get("search_complete") is False for stage in result["stages"]))
