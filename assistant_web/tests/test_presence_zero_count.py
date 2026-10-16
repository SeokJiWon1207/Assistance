"""Conditional existence wording must not become an issue keyword."""

import unittest

from backend.intent.search_terms import build_keyword_terms
from backend.query_planning import build_query_plan, infer_operation
from backend.understanding import extract_query_constraints


class PresenceZeroCountTests(unittest.TestCase):
    def test_conditional_display_request_uses_structured_count(self):
        question = (
            "삼성증권 2026년 8월 등록 이슈 중 아직 종료되지 않은 우리 팀 건이 있는지 확인해줘. "
            "있으면 번호와 제목, 없으면 없다고 알려줘."
        )
        constraints = extract_query_constraints(question)
        self.assertFalse(any(clause["field"] in {"keyword", "title"}
                             for clause in constraints["filter_clauses"]))
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertEqual(infer_operation(question), "count")
        plan = build_query_plan(question, {"source": "mantis"}, constraints,
                                {"source": "mantis", "operation": "search", "terms": []})
        self.assertEqual(plan["operation"], "count")

    def test_real_topic_survives_conditional_display_request(self):
        question = "삼성증권 2026년 8월 로그인 이슈가 있는지 확인해줘. 있으면 번호와 제목, 없으면 없다고 알려줘."
        constraints = extract_query_constraints(question)
        self.assertFalse(any(clause["field"] in {"keyword", "title"}
                             for clause in constraints["filter_clauses"]))
        self.assertEqual(build_keyword_terms(question, constraints), ["로그인"])
        self.assertEqual(infer_operation(question), "count")

    def test_explicit_quoted_title_filter_survives_presence_wording(self):
        question = (
            "삼성증권 2026년 8월 제목에 '로그인' 포함된 이슈가 있는지 확인해줘. "
            "있으면 번호와 제목, 없으면 없다고 알려줘."
        )
        constraints = extract_query_constraints(question)
        self.assertIn({"field": "title", "operator": "contains", "value": "로그인"},
                      constraints["filter_clauses"])
        self.assertFalse(any(clause["field"] == "keyword" for clause in constraints["filter_clauses"]))
        self.assertEqual(infer_operation(question), "count")

    def test_unquoted_title_exclusion_does_not_exclude_generic_issue_word(self):
        question = (
            "코스콤 2026년 4월 등록 건에서 제목에 정기점검이 들어간 건은 빼고 집계해줘. "
            "우리 팀 기준으로 남는 건수와 번호·제목을 알려줘."
        )
        constraints = extract_query_constraints(question)
        self.assertIn({"field": "title", "operator": "not_contains", "value": "정기점검"},
                      constraints["filter_clauses"])
        self.assertFalse(any(clause["field"] == "keyword" for clause in constraints["filter_clauses"]))
        self.assertEqual(infer_operation(question), "count")

    def test_absent_platform_with_conditional_result_wording(self):
        question = (
            "현대차증권에서 2026년 4월에 등록된 우리 팀 이슈 중 매체구분이 MTS인 게 있어? "
            "있으면 번호와 제목을, 없으면 해당 조건은 없다고 알려줘."
        )
        constraints = extract_query_constraints(question)
        self.assertEqual(constraints["platform_key"], "mts")
        self.assertFalse(any(clause["field"] in {"title", "keyword"}
                             for clause in constraints["filter_clauses"]))
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertEqual(infer_operation(question), "count")

    def test_only_existence_with_excluded_status_uses_count(self):
        question = (
            "우리 팀의 LS증권 2026년 8월 등록 내역을 확인해서, "
            "종료 상태를 제외하고 남는 이슈가 있는지만 알려줘."
        )
        constraints = extract_query_constraints(question)
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertEqual(infer_operation(question), "count")

    def test_title_platform_marker_is_not_platform_scope(self):
        question = (
            "DB금융투자 2026년 4월 등록 이슈에서 제목에 [MTS]가 포함된 것은 제외해줘. "
            "우리 팀 기준으로 남는 건수와 번호·제목을 모두 보여줘."
        )
        constraints = extract_query_constraints(question)
        self.assertIsNone(constraints["platform_key"])
        self.assertIn({"field": "title", "operator": "not_contains", "value": "[MTS]"},
                      constraints["filter_clauses"])
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertEqual(infer_operation(question), "count")

    def test_all_rows_wording_does_not_exclude_a_keyword(self):
        question = ("유진투자증권에서 2026년 7월에 등록된 우리 팀 이슈는 전부 몇 건이야? "
                    "번호와 제목을 빠짐없이 보여줘.")
        constraints = extract_query_constraints(question)
        self.assertFalse(any(clause["field"] in {"keyword", "title"}
                             for clause in constraints["filter_clauses"]))
        self.assertEqual(build_keyword_terms(question, constraints), [])

    def test_title_bracket_inclusion_is_not_a_platform_filter(self):
        question = ("KB증권 2026년 8월 등록 이슈 중 제목에 [MTS]라고 적힌 것만 골라줘. "
                    "우리 팀 담당 건수와 각 번호·제목을 알려줘.")
        constraints = extract_query_constraints(question)
        self.assertIsNone(constraints["platform_key"])
        self.assertIn({"field": "title", "operator": "contains", "value": "[MTS]"},
                      constraints["filter_clauses"])
        self.assertEqual(build_keyword_terms(question, constraints), [])

    def test_remaining_result_count_does_not_mean_open_status(self):
        from backend.reports.work_queries import work_operation
        question = ("다올투자증권의 2026년 8월 등록 이슈를 우리 팀 기준으로 세되 "
                    "제목에 서버정기점검이 들어간 건은 제외해줘. 남은 건수와 번호·제목은?")
        constraints = extract_query_constraints(question)
        self.assertEqual(constraints["statuses"], [])
        self.assertIn({"field": "title", "operator": "not_contains", "value": "서버정기점검"},
                      constraints["filter_clauses"])
        self.assertEqual(infer_operation(question), "count")
        self.assertIsNone(work_operation(question, constraints))
        open_question = "아직 끝나지 않아 남은 건을 알려줘"
        self.assertEqual(extract_query_constraints(open_question)["statuses"], ["open"])

    def test_including_bracketed_title_does_not_filter_all_results(self):
        question = ("2026년 7월 KB증권에서 등록된 우리 팀 이슈는 몇 건이야? "
                    "제목에 [MTS]가 붙은 건도 포함해서 각 번호와 제목을 알려줘.")
        constraints = extract_query_constraints(question)
        self.assertIsNone(constraints["platform_key"])
        self.assertFalse(any(clause["field"] in {"title", "keyword"}
                             for clause in constraints["filter_clauses"]))
        self.assertEqual(build_keyword_terms(question, constraints), [])

    def test_bracketed_title_prefix_inclusion_is_not_platform_or_keyword(self):
        question = ("동부증권에서 2026년 9월에 등록된 우리 팀 이슈 전체 건수와 번호·제목을 보여줘. "
                    "[MTS] 제목도 빼지 말고 포함해줘.")
        constraints = extract_query_constraints(question)
        self.assertIsNone(constraints["platform_key"])
        self.assertFalse(any(item["field"] in {"keyword", "title"}
                             for item in constraints["filter_clauses"]))
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertEqual(infer_operation(question), "count")

    def test_include_maintenance_issue_does_not_turn_it_into_the_only_keyword(self):
        question = ("교보증권의 2026년 9월 우리 팀 등록 이슈를 전부 세고 번호·제목을 보여줘. "
                    "정기점검 건도 포함해줘.")
        constraints = extract_query_constraints(question)
        self.assertEqual(build_keyword_terms(question, constraints), [])
        self.assertEqual(infer_operation(question), "count")

    def test_title_equals_exclusion_is_not_a_generic_keyword(self):
        question = ("하나금융증권 2026년 9월 등록 이슈에서 제목이 서버정기점검인 건을 빼고 "
                    "우리 팀 건수와 번호·제목을 정리해줘.")
        constraints = extract_query_constraints(question)
        self.assertIn({"field": "title", "operator": "not_contains", "value": "서버정기점검"},
                      constraints["filter_clauses"])
        self.assertFalse(any(clause["field"] == "keyword" for clause in constraints["filter_clauses"]))

    def test_remaining_numbers_after_title_exclusion_is_a_count_not_open_work(self):
        from backend.reports.work_queries import work_operation
        question = ("케이프증권 2026년 9월 우리 팀 등록 이슈에서 제목에 서버정기점검이 "
                    "들어간 것만 빼면 몇 건이야? 남은 번호와 제목을 알려줘.")
        constraints = extract_query_constraints(question)
        self.assertIn({"field": "title", "operator": "not_contains", "value": "서버정기점검"},
                      constraints["filter_clauses"])
        self.assertEqual(constraints["statuses"], [])
        self.assertEqual(infer_operation(question), "count")
        self.assertIsNone(work_operation(question, constraints))


if __name__ == "__main__":
    unittest.main()
