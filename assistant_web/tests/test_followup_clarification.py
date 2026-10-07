"""확인 질문의 미확정 표현과 확정된 검색 조건을 구분하는 회귀."""

import unittest
from copy import deepcopy
from unittest.mock import patch

from backend.intent.confirmed_context import CONFIRMED_TERMS_VERSION, restore_confirmed_constraints
from backend.intent.constraints import extract_query_constraints
from backend.intent.search_terms import build_keyword_terms
from backend.schemas import ChatMessage, ChatRequest
from backend.services import chat_service as chat
from backend.sources.mantis import select_mantis_rows


def issue(identifier, status, *, title="차트 표시", owner="이영희", media="MTS", customer="120NH(NH증권)"):
    return {
        "id": identifier,
        "summary": title,
        "status": {"name": status},
        "handler": {"real_name": owner},
        "project": {"name": customer},
        "custom_fields": [{"field": {"name": "매체구분"}, "value": media}],
    }


class FollowUpClarificationTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            issue(1, "developed"), issue(2, "closed"), issue(3, "assigned"),
            issue(4, "developed", owner="다른담당"),
            issue(5, "developed", media="HTS"),
            issue(6, "developed", customer="112KBStar(KB증권)"),
        ]

    def plan(self, question, constraints, *, clarification=False):
        context = chat.ChatPipelineContext(
            question, {}, "", [], question, {"mantis": True}, [], constraints, 10,
            status_resolution={
                "needs_clarification": clarification,
                "origin": "unavailable" if clarification else "rule",
                "reason": "planner_unavailable" if clarification else "explicit_scope",
                "grounding_text": question,
            },
        )
        return chat.attach_status_resolution({"content": "테스트 답변"}, context)["retrieval"]["query_plan"]

    def followup(self, previous_question, plan, question):
        request = ChatRequest(
            modelId="fixture-model", contextAuditId="a" * 32,
            messages=[
                ChatMessage(role="user", content=previous_question),
                ChatMessage(role="assistant", content="이전 답변"),
                ChatMessage(role="user", content=question),
            ],
        )
        with (
            patch.object(chat, "get_private_model", return_value={"id": "fixture-model", "status": "active"}),
            patch.object(chat, "get_model_api_key", return_value="fixture-key"),
            patch.object(chat, "load_audit_query_plan", return_value=plan),
            patch.object(chat, "resolve_reference_from_context", return_value=None),
            patch.object(chat, "rewrite_follow_up_query_with_model", side_effect=AssertionError("모델 호출 금지")),
        ):
            return chat.prepare_chat_pipeline(request, question, {"uid": "fixture-user"})

    def test_failed_status_plans_do_not_narrow_followup_counts_with_raw_terms(self):
        question = "NH MTS 아직 손봐야 할 이슈 몇 건이야?"
        plan = self.plan(question, extract_query_constraints(question), clarification=True)
        self.assertEqual(plan["terms"], ["아직", "손봐야", "할"])

        owner_question = "그중 이영희 담당만 몇 건이야?"
        owner = self.followup(question, plan, owner_question)
        self.assertEqual(owner.query_constraints["customer_key"], "NH")
        self.assertEqual(owner.query_constraints["platform_key"], "mts")
        self.assertEqual(owner.query_constraints["people"]["names"], ["이영희"])
        self.assertEqual(owner.query_constraints["inherited_terms"], [])
        self.assertFalse(any(c["field"] == "keyword" for c in owner.query_constraints["filter_clauses"]))

        owner_plan = self.plan(owner.retrieval_text, owner.query_constraints, clarification=True)
        developed_question = "그중 개발완료만 몇 건이야?"
        developed = self.followup(owner_question, owner_plan, developed_question)
        self.assertEqual(developed.query_constraints["statuses"], ["dev_done"])
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, developed.query_constraints)], [1])

        developed_plan = self.plan(developed.retrieval_text, developed.query_constraints)
        all_statuses = self.followup(developed_question, developed_plan, "그중 전체 상태로 몇 건이야?")
        self.assertEqual(all_statuses.query_constraints["statuses"], [])
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, all_statuses.query_constraints)], [1, 2, 3])
        self.assertNotIn("손봐야", all_statuses.retrieval_text)

    def test_successful_search_terms_remain_grounded_in_followup_text(self):
        question = "NH MTS 담당자 이영희의 차트 이슈 몇 건이야?"
        plan = self.plan(question, extract_query_constraints(question))
        self.assertEqual(plan["terms"], ["차트"])
        context = self.followup(question, plan, "그중 개발완료만 몇 건이야?")
        rows = [*self.rows, issue(7, "developed", title="주문 표시")]
        self.assertEqual([r["id"] for r in select_mantis_rows(rows, context.query_constraints)], [1])
        self.assertIn("차트", context.retrieval_text)
        self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), ["차트"])

    def test_clarification_keeps_prior_keywords_and_explicit_exclusions(self):
        question = "NH MTS 담당자 이영희의 차트에서 아직 손봐야 할 이슈 몇 건이야?"
        constraints = extract_query_constraints("NH MTS 담당자 이영희 이슈")
        keyword = {"field": "keyword", "operator": "contains", "value": ["차트"]}
        excluded_title = {"field": "title", "operator": "not_contains", "value": "오류"}
        constraints["inherited_terms"] = ["차트"]
        constraints["confirmed_terms_version"] = CONFIRMED_TERMS_VERSION
        constraints["filter_clauses"].extend([keyword, excluded_title])
        plan = {"source": "mantis", "resolved_constraints": constraints,
                "terms": ["아직", "손봐야", "할"], "status_resolution": {"needs_clarification": True}}
        original = deepcopy(plan)
        context = self.followup(question, plan, "그중 전체 상태로 몇 건이야?")
        self.assertEqual(plan, original)
        self.assertEqual(context.query_constraints["inherited_terms"], ["차트"])
        self.assertEqual(context.query_constraints["filter_clauses"].count(keyword), 1)
        self.assertEqual(context.query_constraints["filter_clauses"].count(excluded_title), 1)
        self.assertIn("키워드 '차트' 포함", context.retrieval_text)
        self.assertIn("제목 '오류' 제외", context.retrieval_text)
        self.assertNotIn("손봐야", context.retrieval_text)
        self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), ["차트"])
        rows = [*self.rows, issue(7, "developed", title="차트 오류"), issue(8, "developed", title="주문 표시")]
        self.assertEqual([r["id"] for r in select_mantis_rows(rows, context.query_constraints)], [1, 2, 3])

    def test_pending_new_terms_do_not_replace_previously_confirmed_terms(self):
        constraints = {"inherited_terms": ["로그인"], "filter_clauses": [],
                       "confirmed_terms_version": CONFIRMED_TERMS_VERSION}
        plan = {"resolved_constraints": constraints, "terms": ["챙겨야", "할"],
                "status_resolution": {"needs_clarification": True}}
        restored = restore_confirmed_constraints(plan)
        self.assertEqual(restored["inherited_terms"], ["로그인"])
        restored["inherited_terms"].append("새 검색어")
        self.assertEqual(constraints["inherited_terms"], ["로그인"])

    def legacy_plan(self, *, clarification=False):
        constraints = extract_query_constraints("NH MTS 담당자 이영희 개발완료 이슈")
        terms = ["아직", "손봐야", "할"]
        constraints["inherited_terms"] = terms
        constraints["filter_clauses"].append({"field": "keyword", "operator": "contains", "value": terms})
        return {"source": "mantis", "resolved_constraints": constraints, "terms": terms,
                "status_resolution": {"needs_clarification": clarification}}

    def test_legacy_terms_require_confirmation_even_after_a_status_was_selected(self):
        for clarification in (True, False):
            with self.subTest(clarification=clarification):
                plan = self.legacy_plan(clarification=clarification)
                question = "그중 전체 상태로 몇 건이야?"
                context = self.followup("그중 개발완료만 몇 건이야?", plan, question)
                self.assertEqual(context.query_constraints["unconfirmed_search_terms"], ["아직", "손봐야", "할"])
                self.assertNotIn("손봐야", context.retrieval_text)
                with (
                    patch("backend.reports.work_queries.build_work_query_report") as report,
                    patch.object(chat, "resolve_status_intent") as status,
                ):
                    response = chat.plan_chat_query(
                        ChatRequest(modelId="fixture-model", messages=[ChatMessage(role="user", content=question)]),
                        context, None,
                    ).response
                report.assert_not_called()
                status.assert_not_called()
                self.assertEqual(response["raw"]["mode"], "search_terms_clarification")
                self.assertEqual(response["sources"], [])
                self.assertEqual(response["retrieval"]["stages"], [])
                self.assertIn("검색어 조건", response["content"])
                restored = restore_confirmed_constraints(response["retrieval"]["query_plan"])
                self.assertTrue(restored["unconfirmed_search_terms"])

    def test_explicit_keyword_reply_replaces_legacy_terms_and_can_continue(self):
        question = '그중 제목에 "차트" 포함, 전체 상태로 몇 건이야?'
        legacy = self.legacy_plan()
        legacy["resolved_constraints"]["filter_clauses"].append(
            {"field": "title", "operator": "contains", "value": "주문"}
        )
        context = self.followup("그중 개발완료만 몇 건이야?", legacy, question)
        self.assertNotIn("unconfirmed_search_terms", context.query_constraints)
        self.assertEqual(context.query_constraints["inherited_terms"], [])
        self.assertEqual(context.query_constraints["confirmed_terms_version"], CONFIRMED_TERMS_VERSION)
        self.assertFalse(any(c["field"] == "keyword" for c in context.query_constraints["filter_clauses"]))
        self.assertEqual(
            [c["value"] for c in context.query_constraints["filter_clauses"] if c["field"] == "title"],
            ["차트"],
        )
        self.assertNotIn("손봐야", context.retrieval_text)
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, context.query_constraints)], [1, 2, 3])
        plan = self.plan(context.retrieval_text, context.query_constraints)
        continued = self.followup(question, plan, "그중 개발완료만 몇 건이야?")
        self.assertNotIn("unconfirmed_search_terms", continued.query_constraints)
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, continued.query_constraints)], [1])

    def test_independent_question_discards_legacy_search_terms(self):
        question = "NH MTS 담당자 이영희 전체 상태 이슈 몇 건이야?"
        context = self.followup("그중 개발완료만 몇 건이야?", self.legacy_plan(), question)
        self.assertNotIn("unconfirmed_search_terms", context.query_constraints)
        self.assertFalse(context.query_constraints.get("inherited_terms"))
        self.assertEqual([r["id"] for r in select_mantis_rows(self.rows, context.query_constraints)], [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
