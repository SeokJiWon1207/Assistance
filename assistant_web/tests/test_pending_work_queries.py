"""잔여 업무 구어체를 상태 조건으로 해석하고 누락을 검증한다."""

import unittest
from datetime import date
from unittest.mock import patch

from backend.intent.constraints import extract_query_constraints, merge_query_plan_constraints
from backend.intent.search_terms import build_keyword_terms
from backend.query_planning import build_query_plan, validate_query_plan
from backend.reports.work_queries import build_work_query_report, work_operation
from backend.sources.mantis import issue_matches_constraints
from backend.sources.mantis_collection import IssueBatch


class PendingWorkQueryTests(unittest.TestCase):
    def test_excluding_closed_issues_keeps_open_scope_through_work_report(self):
        questions = (
            "2026년 6월 등록 SK증권 이슈에서 종료된 건을 빼면 우리 팀 담당으로 몇 건 남아? 이슈 번호, 제목, 지금 상태를 알려줘.",
            "2026년 6월 등록 SK증권 이슈에서 종료된 이슈를 제외하면 몇 건 남아?",
            "2026년 6월 등록 SK증권 이슈에서 종료된 건은 빼고 몇 건 남아?",
        )
        rows = [
            {"id": 31218, "handler": {"real_name": "석지원"}, "project": {"name": "108SK(SK증권)"},
             "status": {"name": "confirmed"}, "created_at": "2026-06-26"},
            {"id": 31203, "handler": {"real_name": "강수현"}, "project": {"name": "108SK(SK증권)"},
             "status": {"name": "closed"}, "created_at": "2026-06-25"},
            {"id": 31204, "handler": {"real_name": "왕승엽"}, "project": {"name": "108SK(SK증권)"},
             "status": {"name": "confirmed"}, "created_at": "2026-06-25"},
        ]
        for question in questions:
            with self.subTest(question=question):
                constraints = extract_query_constraints(question)
                plan = build_query_plan(question, {"source": "mantis"}, constraints)
                merged = merge_query_plan_constraints(constraints, plan)
                self.assertEqual(constraints["statuses"], ["open"])
                self.assertEqual(plan["filters"]["statuses"], ["open"])
                self.assertEqual(merged["statuses"], ["open"])
                self.assertEqual(work_operation(question, merged), "open")
                self.assertEqual([row["id"] for row in rows if issue_matches_constraints(row, merged)], [31218])

    def test_closed_issues_remain_selected_without_exclusion(self):
        question = "2026년 6월 등록 SK증권 종료된 이슈만 몇 건이야?"
        constraints = extract_query_constraints(question)
        self.assertEqual(constraints["statuses"], ["closed"])
        self.assertEqual(build_query_plan(question, {"source": "mantis"}, constraints)["filters"]["statuses"], ["closed"])

    def test_closed_exclusion_work_report_uses_only_remaining_team_issue(self):
        question = "2026년 6월 등록 SK증권 이슈에서 종료된 건을 빼면 우리 팀 담당으로 몇 건 남아? 이슈 번호, 제목, 지금 상태를 알려줘."
        rows = [
            {"id": 31218, "summary": "[4103] 유지보수 내에 조건검색식 추가 관련 문의", "handler": {"real_name": "석지원"},
             "project": {"name": "108SK(SK증권)"}, "status": {"name": "confirmed"}, "created_at": "2026-06-26"},
            {"id": 31203, "summary": "종료된 이슈", "handler": {"real_name": "강수현"},
             "project": {"name": "108SK(SK증권)"}, "status": {"name": "closed"}, "created_at": "2026-06-25"},
        ]
        snapshot = IssueBatch(rows, {"search_complete": True, "snapshot_complete": True,
                                     "fetchedAt": "2026-10-15T03:06:25+00:00"})
        with patch("backend.reports.work_queries.read_complete_store", return_value=snapshot):
            report = build_work_query_report(question, extract_query_constraints(question), today=date(2026, 10, 15))
        self.assertIsNotNone(report)
        self.assertEqual(report["retrieval"]["stages"][0]["meta"]["matched_ids"], [31218])
        self.assertIn("1건", report["content"])
        self.assertIn("#31218", report["content"])
        self.assertIn("확인된 이슈", report["content"])
        self.assertNotIn("#31203", report["content"])

    def test_colloquial_pending_phrases_keep_customer_platform_and_open_status(self):
        phrases = (
            "남은거", "남은 거", "남은 건", "남은 것", "남아있는 이슈", "남아 있는 건",
            "아직 남아있는 것", "잔여 이슈", "미처리 이슈", "미해결 이슈", "미완료 이슈",
            "아직 처리 안 된 거", "아직 해결 안 된 이슈", "완료 안 된 건", "종료 안 된 것",
            "처리되지 않은 이슈", "해결되지 않은 건", "완료되지 않은 것", "종료되지 않은 이슈",
            "아직 처리 못 한 건", "해결하지 못한 이슈", "안 끝난 이슈", "끝나지 않은 이슈",
            "이슈가 아직 남아 있는 건",
        )
        for phrase in phrases:
            with self.subTest(phrase=phrase):
                q = f"지금 NH에서 MTS {phrase} 몇개야"
                c = extract_query_constraints(q)
                self.assertEqual(c["customer_key"], "NH")
                self.assertEqual(c["platform_key"], "mts")
                self.assertEqual(c["statuses"], ["open"])
                plan = build_query_plan(q, {"source": "mantis"}, c, {
                    "filter_clauses": [{"field": "status", "operator": "equals", "value": "closed"}],
                })
                merged = merge_query_plan_constraints(c, plan)
                self.assertEqual(merged["statuses"], ["open"])
                self.assertTrue(validate_query_plan(q, plan)["valid"])

    def test_non_status_remaining_words_do_not_set_open(self):
        for text in ("NH MTS 남은 기간 알려줘", "NH MTS 잔여 공수 알려줘", "남은 시간 계산해줘",
                     "이미 처리한 이슈 보여줘"):
            with self.subTest(text=text):
                self.assertEqual(extract_query_constraints(text)["statuses"], [])

    def test_specific_positive_status_is_preserved(self):
        for text, expected in (("개발완료 이슈", "dev_done"), ("종료된 이슈", "closed"),
                               ("이미 완료된 이슈", "closed"),
                               ("해결된 이슈", "resolved"), ("종료 안 된 할당 이슈", "assigned")):
            self.assertEqual(extract_query_constraints(text)["statuses"], [expected])

    def test_pending_words_are_not_literal_search_keywords(self):
        for phrase in ("남은 거", "아직 처리 안 된 거", "해결되지 않은 이슈", "미처리 이슈"):
            q = f"NH MTS {phrase} 차트 보여줘"
            with self.subTest(phrase=phrase):
                self.assertEqual(build_keyword_terms(q, extract_query_constraints(q)), ["차트"])

    def test_closed_and_resolved_rows_are_excluded_from_count_candidates(self):
        c = extract_query_constraints("지금 NH에서 MTS 이슈 남은거 몇개야")
        for status in ("assigned", "confirmed", "developed", "closed", "resolved"):
            row = {"handler": {"real_name": "석지원"}, "project": {"name": "120NH(NH증권)"}, "status": {"name": status},
                   "custom_fields": [{"field": {"name": "매체구분"}, "value": "MTS"}]}
            self.assertEqual(issue_matches_constraints(row, c), status not in {"closed", "resolved"})

    def test_missing_status_filter_cannot_pass_verification(self):
        q = "지금 NH에서 MTS 이슈 남은거 몇개야"
        plan = build_query_plan(q, {"source": "mantis"}, extract_query_constraints(q))
        for location in ("filters", "clauses", "both"):
            altered = {**plan, "filters": dict(plan["filters"]), "filter_clauses": list(plan["filter_clauses"])}
            if location in {"filters", "both"}:
                altered["filters"]["statuses"] = []
            if location in {"clauses", "both"}:
                altered["filter_clauses"] = [c for c in altered["filter_clauses"] if c["field"] != "status"]
            checks = validate_query_plan(q, altered)["checks"]
            self.assertEqual(next(c for c in checks if c["id"] == "status_intent")["status"], "fail")


if __name__ == "__main__":
    unittest.main()
