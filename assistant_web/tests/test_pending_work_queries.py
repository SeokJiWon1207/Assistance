"""잔여 업무 구어체를 상태 조건으로 해석하고 누락을 검증한다."""

import unittest

from backend.intent.constraints import extract_query_constraints, merge_query_plan_constraints
from backend.intent.search_terms import build_keyword_terms
from backend.query_planning import build_query_plan, validate_query_plan
from backend.sources.mantis import issue_matches_constraints


class PendingWorkQueryTests(unittest.TestCase):
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
