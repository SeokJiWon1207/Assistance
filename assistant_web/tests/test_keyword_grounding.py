"""검색 결과가 없어도 질문에 없던 키워드 조건을 검증한다."""

import unittest

from backend.evaluation.verification import build_verification_report, ungrounded_constraint_labels
from backend.query_planning import build_query_plan
from backend.understanding import extract_query_constraints


class KeywordGroundingTests(unittest.TestCase):
    def test_unresolved_words_inherited_as_filters_are_not_grounded(self):
        question = "NH MTS 개발완료 이슈 몇 건이야?"
        constraints = extract_query_constraints(question)
        terms = ["아직", "손봐야", "할"]
        constraints["inherited_terms"] = terms
        constraints["filter_clauses"].append({"field": "keyword", "operator": "contains", "value": terms})
        self.assertEqual(
            ungrounded_constraint_labels(question, constraints, "mantis"),
            ["검색어=아직", "검색어=손봐야", "검색어=할"],
        )

    def test_explicit_title_and_excluded_keyword_are_grounded(self):
        question = 'NH MTS 제목에 "차트" 포함, 본문에 "로그인" 제외 이슈 몇 건이야?'
        constraints = extract_query_constraints(question)
        self.assertTrue(any(c["field"] == "title" for c in constraints["filter_clauses"]))
        self.assertEqual(ungrounded_constraint_labels(question, constraints, "mantis"), [])

    def test_invented_exclusion_is_not_grounded(self):
        question = "NH MTS 이슈 몇 건이야?"
        constraints = extract_query_constraints(question)
        constraints["filter_clauses"].extend([
            {"field": "keyword", "operator": "not_contains", "value": "로그인"},
            {"field": "title", "operator": "contains", "value": ["차트", "주문"]},
        ])
        self.assertEqual(
            ungrounded_constraint_labels(question, constraints, "mantis"),
            ["검색어=로그인", "제목=차트", "제목=주문"],
        )

    def test_confirmed_keyword_in_retrieval_text_remains_grounded(self):
        question = 'NH MTS 개발완료 키워드 "차트" 포함 그중 몇 건이야?'
        constraints = extract_query_constraints(question)
        constraints["inherited_terms"] = ["차트"]
        self.assertEqual(ungrounded_constraint_labels(question, constraints, "mantis"), [])

    def count_report(self, question, constraints):
        retrieval = {
            "query": question,
            "constraints": constraints,
            "query_plan": build_query_plan(question, {"source": "mantis"}, constraints),
            "stages": [{
                "source": "mantis_snapshot", "status": "ok",
                "meta": {"operation": "count", "total_count": 0, "applied_filters": constraints,
                         "search_complete": True, "coverage": "full"},
            }],
        }
        return build_verification_report(question, "조건에 맞는 이슈는 총 0건입니다.", [], retrieval,
                                         "deterministic_mantis_count")

    def test_matching_zero_count_does_not_hide_ungrounded_filters(self):
        question = "NH MTS 개발완료 이슈 몇 건이야?"
        constraints = extract_query_constraints(question)
        constraints["filter_clauses"].append(
            {"field": "keyword", "operator": "contains", "value": ["아직", "손봐야", "할"]},
        )
        report = self.count_report(question, constraints)
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(checks["count_result"]["status"], "pass")
        self.assertEqual(checks["count_filters"]["status"], "pass")
        self.assertEqual(checks["constraint_grounding"]["status"], "warning")
        self.assertNotEqual(report["status"], "pass")

    def test_grounded_zero_count_is_not_warned_just_for_being_empty(self):
        question = "NH MTS 개발완료 이슈 몇 건이야?"
        report = self.count_report(question, extract_query_constraints(question))
        self.assertEqual(report["status"], "pass")

    def test_legacy_search_confirmation_has_no_completed_verification(self):
        report = build_verification_report(
            "그중 개발완료만 몇 건이야?", "이전 대화의 검색어 조건을 확인해야 합니다.", [],
            {"constraints": {"unconfirmed_search_terms": ["아직", "손봐야", "할"]}, "stages": []},
            "search_terms_clarification",
        )
        self.assertEqual(report["status"], "warning")
        self.assertEqual(report["completedDimensions"], 0)
        self.assertEqual(report["checks"][0]["label"], "검색어 확인 대기")

    def test_unconfirmed_search_scope_cannot_pass_as_a_count(self):
        question = "NH MTS 개발완료 이슈 몇 건이야?"
        constraints = extract_query_constraints(question)
        constraints["unconfirmed_search_terms"] = ["아직", "손봐야", "할"]
        report = self.count_report(question, constraints)
        self.assertEqual(report["status"], "warning")


if __name__ == "__main__":
    unittest.main()
