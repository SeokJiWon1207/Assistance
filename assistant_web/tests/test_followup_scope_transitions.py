"""고객사·기간·담당자·매체를 바꾸는 연속 질문의 검색 결과 회귀."""

import unittest
from contextlib import nullcontext
from unittest.mock import patch

from backend.intent.confirmed_context import confirmed_constraints_scope
from backend.evaluation.data_scope import build_answer_data_scope
from backend.intent.constraints import extract_query_constraints
from backend.intent.search_terms import build_keyword_terms
from backend.intent.context import is_follow_up_question
from backend.query_planning import build_query_plan, validate_query_plan
from backend.reports.aggregate import build_mantis_count_report
from backend.sources.mantis_collection import IssueBatch
from backend.schemas import ChatMessage, ChatRequest
from backend.services import chat_service as chat
from backend.sources.mantis import select_mantis_rows


BASE = "2025년 SK증권 HTS 우동헌 담당 이슈 몇 건이야?"


def raw(identifier, *, customer="108SK(SK증권)", owner="우동헌", media="HTS", created="2025-06-10"):
    return {"id": identifier, "summary": "차트 조회", "project": {"name": customer},
            "handler": {"real_name": owner}, "created_at": created,
            "custom_fields": [{"field": {"name": "매체구분"}, "value": media}]}


class FollowupScopeTransitionsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("backend.intent.entities.load_known_people", return_value=(
            ["우동헌", "정희진", "하영훈", "이영희"], [])))
        self.enterContext(patch.object(chat, "get_private_model", return_value={"id": "fixture", "status": "active"}))
        self.enterContext(patch.object(chat, "get_model_api_key", return_value="fixture"))
        self.enterContext(patch.object(chat, "resolve_reference_from_context", return_value=None))
        self.enterContext(patch.object(chat, "rewrite_follow_up_query_with_model", side_effect=AssertionError("unexpected rewrite")))
        self.rows = [raw(1), raw(2, created="2025-07-10"), raw(3, owner="정희진"),
                     raw(4, media="MTS"), raw(5, customer="120NH(NH증권)"), raw(6, created="2024-06-10"),
                     raw(7, owner="정희진", customer="120NH(NH증권)"), raw(8, owner="정희진", created="2024-06-10"),
                     raw(9, owner="정희진", media="MTS")]

    def plan(self, text, constraints):
        with confirmed_constraints_scope(text, constraints):
            result = build_query_plan(text, {"source": "mantis"}, constraints)
        result.update(resolved_constraints=constraints, terms=build_keyword_terms(text, constraints))
        return result

    def initial(self, question=BASE):
        return self.plan(question, extract_query_constraints(question))

    def follow(self, question, prior=None, previous=BASE):
        request = ChatRequest(modelId="fixture", contextAuditId="a" * 32, messages=[
            ChatMessage(role="user", content=previous), ChatMessage(role="assistant", content="이전 조회 결과"),
            ChatMessage(role="user", content=question)])
        with patch.object(chat, "load_audit_query_plan", return_value=prior or self.initial()):
            context = chat.prepare_chat_pipeline(request, question, {"uid": "fixture-user"})
        return context, self.plan(context.retrieval_text, context.query_constraints)

    def test_scope_change_words_never_become_keywords_across_turns(self):
        previous, plan = BASE, self.initial()
        for question in ("그럼 고객사만 KB증권으로 바꿔서 몇 건이야?",
                         "그럼 담당자는 하영훈으로 바꿔서 몇 건이야?",
                         "플랫폼만 MTS로 바꿔서 몇 건이야?",
                         "그럼 기간만 2024년으로 바꿔서 몇 건이야?"):
            context, plan = self.follow(question, plan, previous)
            with self.subTest(question=question):
                self.assertEqual(plan["terms"], [])
                self.assertFalse(any(c["field"] == "keyword" for c in context.query_constraints["filter_clauses"]))
            previous = question

    def test_long_keep_rest_request_only_changes_named_owner(self):
        context, _ = self.follow("고객사와 기간은 그대로 두고 담당자만 정희진으로 바꿔서 몇 건이야?")
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, context.query_constraints)], [3])
        self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), [])

    def test_long_keep_rest_request_only_changes_named_platform(self):
        context, _ = self.follow("나머지는 그대로 두고 플랫폼만 MTS로 바꿔서 몇 건이야?")
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, context.query_constraints)], [4])
        self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), [])

    def test_each_explicit_clear_removes_only_that_dimension(self):
        cases = [("그럼 담당자 조건은 빼고 몇 건이야?", [1, 2, 3]),
                 ("그럼 플랫폼 조건도 빼고 몇 건이야?", [1, 2, 4]),
                 ("그럼 고객사 제한 없이 몇 건이야?", [1, 2, 5]),
                 ("그럼 기간 제한 없이 몇 건이야?", [1, 2, 6])]
        for question, expected in cases:
            with self.subTest(question=question):
                context, _ = self.follow(question)
                self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, context.query_constraints)], expected)
                self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), [])

    def test_cleared_customer_does_not_return_on_next_turn(self):
        question = "그럼 고객사 제한 없이 몇 건이야?"
        _, plan = self.follow(question)
        context, _ = self.follow("그중 최근 3건 보여줘", plan, question)
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, context.query_constraints)], [1, 2, 5])

    def test_yearless_month_replaces_year_range_using_previous_year(self):
        question = "그럼 6월은 몇 건이야?"
        context, plan = self.follow(question)
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, context.query_constraints)], [1])
        context, _ = self.follow("그럼 7월은 몇 건이야?", plan, question)
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, context.query_constraints)], [2])

    def test_yearless_registration_month_uses_same_year_in_plan_and_display(self):
        question = "그럼 6월에 등록된 이슈는 몇 건이야?"
        context, plan = self.follow(question)
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, context.query_constraints)], [1])
        self.assertTrue(validate_query_plan(context.retrieval_text, plan)["valid"])
        # 응답 검증에는 재작성한 검색문이 아니라 실제 사용자의 질문이 전달된다.
        self.assertTrue(validate_query_plan(question, plan)["valid"])
        plan["filters"]["created_at"] = {"from": "2024-06-01", "to": "2024-06-30"}
        self.assertFalse(validate_query_plan(question, plan)["valid"])

    def test_month_validation_does_not_override_explicit_year_or_trust_unaudited_plan(self):
        question = "그럼 6월에 등록된 이슈는 몇 건이야?"
        _, plan = self.follow(question)
        self.assertFalse(validate_query_plan("2024년 6월에 등록된 이슈는 몇 건이야?", plan)["valid"])
        plan["resolved_constraints"].pop("inherited_from_audit_id", None)
        self.assertFalse(validate_query_plan(question, plan)["valid"])

    def test_fully_specified_new_question_does_not_inherit_old_scope(self):
        context, _ = self.follow("2024년 NH증권 MTS 이영희 담당 이슈 몇 건이야?")
        self.assertEqual(context.query_constraints["customer_key"], "NH증권")
        self.assertEqual(context.query_constraints["people"]["names"], ["이영희"])
        self.assertEqual(context.query_constraints["platform_key"], "mts")
        self.assertEqual(context.query_constraints["created_at"]["from"], "2024-01-01")
        self.assertNotIn("inherited_from_audit_id", context.query_constraints)

    def test_negated_clear_does_not_remove_scope(self):
        context, _ = self.follow("그럼 고객사 제한은 해제하지 말고 그대로 몇 건이야?")
        self.assertEqual(context.query_constraints["customer_key"], "SK증권")

    def test_quoted_issue_text_is_not_a_scope_change(self):
        context, _ = self.follow('그중 제목에 "고객사 제한 없이" 포함된 이슈 보여줘')
        self.assertEqual(context.query_constraints["customer_key"], "SK증권")

    def test_described_customer_change_request_is_not_a_conversation_edit(self):
        self.assertFalse(is_follow_up_question("고객사 코드를 바꿔달라는 요청 이슈 있었어?"))

    def test_clearing_scope_validates_absence_instead_of_demanding_exclusion(self):
        question = "그럼 담당자 조건은 빼고 몇 건이야?"
        context, plan = self.follow(question)
        self.assertTrue(validate_query_plan(context.retrieval_text, plan)["valid"])
        plan["filter_clauses"].append({"field": "owner", "operator": "equals", "value": "우동헌"})
        self.assertFalse(validate_query_plan(context.retrieval_text, plan)["valid"])

    def test_clearing_scope_does_not_hide_a_separate_negative_filter(self):
        question = "그럼 고객사 제한 없이 MTS는 제외하고 몇 건이야?"
        context, plan = self.follow(question)
        self.assertTrue(validate_query_plan(context.retrieval_text, plan)["valid"])
        plan["filter_clauses"] = [c for c in plan["filter_clauses"] if c["field"] != "platform"]
        self.assertFalse(validate_query_plan(context.retrieval_text, plan)["valid"])

    def test_all_filters_cleared_still_returns_full_count(self):
        question = "멘티스 그럼 기간 제한 없이 몇 건이야?"
        constraints = extract_query_constraints(question)
        plan = self.plan(question, constraints)
        batch = IssueBatch(self.rows, {"search_complete": True, "snapshot_complete": True,
                                      "fetchedAt": "2026-10-12T00:00:00+00:00"})
        with patch("backend.sources.mantis_store.read_complete_store", return_value=batch), \
                patch("backend.sources.mantis_store.stream_complete_store", return_value=nullcontext(batch), create=True):
            result = build_mantis_count_report(question, plan)
        self.assertIsNotNone(result)
        self.assertEqual(result["retrieval"]["stages"][0]["meta"]["total_count"], 9)
        display = build_answer_data_scope(result["retrieval"])
        self.assertEqual(display[0]["coverage"], "선택 범위 조회 완료")
        self.assertFalse(display[0]["warning"])


if __name__ == "__main__":
    unittest.main()
