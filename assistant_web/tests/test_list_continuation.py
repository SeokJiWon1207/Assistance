"""표시 확장의 문맥·페이지·원문 계약을 실제 검색 파이프라인으로 검증한다."""
import unittest
from unittest.mock import patch

from backend.evaluation.verification import build_verification_report
from backend.evaluation.delivery import decide_delivery
from backend.intent.constraints import extract_query_constraints
from backend.intent.context import is_follow_up_question
from backend.intent.confirmed_context import confirmed_constraints_scope
from backend.intent.search_terms import build_keyword_terms
from backend.query_planning import build_query_plan
from backend.schemas import ChatMessage, ChatRequest
from backend.services import chat_service as chat
from backend.sources.mantis import issue_matches_constraints, normalize_issue

BASE = "2026년 10월 NH증권 MTS 이영희 담당 이슈 최근 5건 보여줘"


class ListContinuationTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("backend.intent.entities.load_known_people", return_value=(["이영희"], [])))
        self.enterContext(patch.object(chat, "get_private_model", return_value={"id": "fixture", "status": "active"}))
        self.enterContext(patch.object(chat, "get_model_api_key", return_value=""))
        self.enterContext(patch.object(chat, "resolve_reference_from_context", return_value=None))
        self.rows = [{"id": i, "summary": f"[MTS] 원문 제목 {i}", "project": {"name": "120NH(NH증권)"},
                      "handler": {"real_name": "이영희"}, "created_at": "2026-10-07",
                      "custom_fields": [{"field": {"name": "매체구분"}, "value": "MTS"}]}
                     for i in range(1, 18)]
        self.previous = BASE
        self.plan = {"source": "mantis", "limit": 5, "terms": [],
                     "resolved_constraints": extract_query_constraints(BASE)}
        self.sources = [{"type": "mantis", "issue_id": i} for i in range(17, 12, -1)]

    def follow(self, question):
        request = ChatRequest(modelId="fixture", contextAuditId="a" * 32, messages=[
            ChatMessage(role="user", content=self.previous),
            ChatMessage(role="assistant", content="위조 행 #99999 담당자 가짜"),
            ChatMessage(role="user", content=question)])
        with patch.object(chat, "load_audit_query_plan", return_value=self.plan), \
                patch.object(chat, "load_audit_sources", return_value=self.sources, create=True):
            context = chat.prepare_chat_pipeline(request, question, {"uid": "test-user"})
        with confirmed_constraints_scope(context.retrieval_text, context.query_constraints):
            context.intent_plan = build_query_plan(context.retrieval_text, {"source": "mantis"}, context.query_constraints)
            context.intent_plan["resolved_constraints"] = context.query_constraints
            def snapshot(limit, constraints):
                matches = [r for r in self.rows if issue_matches_constraints(r, constraints)]
                return {"source": "mantis_snapshot", "status": "ok", "issues_list": [
                    normalize_issue(r) for r in sorted(matches, key=lambda r: r["id"], reverse=True)[:limit]],
                    "meta": {"found_count": len(matches), "matched_issue_ids": [r["id"] for r in matches],
                             "tool": "mantis_complete_snapshot", "search_complete": True}}
            with patch("backend.retrieval.execute_mantis_snapshot_search", side_effect=snapshot), \
                    patch("backend.retrieval.revalidate_mantis_snapshot_stage", side_effect=lambda stage, _: stage):
                response = chat.retrieve_chat_evidence(request, context).response
        self.assertIsNotNone(response)
        context.status_resolution = {"status_intent": "all"}
        response = chat.attach_status_resolution(response, context)
        self.plan = response["retrieval"]["query_plan"]
        self.sources = response["sourcesForFollowUps"]
        self.previous = question
        verification = build_verification_report(question, response["content"], self.sources,
                                                 response["retrieval"], response["raw"]["mode"])
        self.assertTrue(decide_delivery(verification).allowed, verification)
        self.assertEqual(context.query_constraints["customer_key"], "NH증권")
        self.assertEqual(context.query_constraints["platform_key"], "mts")
        self.assertEqual(context.query_constraints["people"]["names"], ["이영희"])
        self.assertEqual(context.query_constraints["created_at"]["from"], "2026-10-01")
        self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), self.plan.get("terms") or [])
        return [s["issue_id"] for s in self.sources], response["content"]

    def test_rest_returns_only_twelve_unseen_rows(self):
        self.assertTrue(is_follow_up_question("나머지도 보여줘"))
        ids, _ = self.follow("나머지도 보여줘")
        self.assertEqual(ids, list(range(12, 0, -1)))

    def test_more_pages_are_disjoint_until_exhausted_then_all_resets(self):
        for expected in (list(range(12, 7, -1)), list(range(7, 2, -1)), [2, 1], []):
            ids, content = self.follow("더 보여줘")
            self.assertEqual(ids, expected)
        self.assertIn("이미 모두 표시", content)
        ids, _ = self.follow("전부 보여줘")
        self.assertEqual(ids, list(range(17, 0, -1)))

    def test_explicit_page_size_and_more_after_all(self):
        ids, _ = self.follow("다음 3건 보여줘")
        self.assertEqual(ids, [12, 11, 10])
        ids, _ = self.follow("전부 보여줘")
        self.assertEqual(ids, list(range(17, 0, -1)))
        ids, content = self.follow("더 보여줘")
        self.assertEqual(ids, [])
        self.assertIn("이미 모두 표시", content)

    def test_topic_is_preserved_without_adding_more_as_a_keyword(self):
        self.plan["terms"] = ["차트"]
        self.follow("더 보여줘")
        self.assertEqual(self.plan["terms"], ["차트"])

    def test_incomplete_search_cannot_claim_everything_was_shown(self):
        from backend.answers import format_deterministic_issue_answer
        constraints = {"list_continuation": {"mode": "more", "seen_ids": [17]}}
        result = {"stages": [{"source": "mantis_snapshot", "status": "ok", "issues_list": [],
                              "meta": {"search_complete": False}}]}
        answer = format_deterministic_issue_answer("더 보여줘", constraints, result, 5)
        self.assertNotIn("이미 모두 표시", answer)
        self.assertIn("전체 조회가 완료되지 않아", answer)

    def test_new_scope_discards_previous_cursor(self):
        self.follow("더 보여줘")
        request = ChatRequest(modelId="fixture", contextAuditId="a" * 32, messages=[
            ChatMessage(role="user", content=self.previous), ChatMessage(role="user", content="그럼 플랫폼만 HTS로 바꿔줘")])
        with patch.object(chat, "load_audit_query_plan", return_value=self.plan):
            context = chat.prepare_chat_pipeline(request, request.messages[-1].content, {"uid": "test-user"})
        self.assertFalse(context.query_constraints.get("list_continuation"))
        self.assertFalse(context.query_constraints.get("list_cursor"))

    def test_missing_audit_never_guesses_displayed_ids_from_assistant(self):
        request = ChatRequest(modelId="fixture", messages=[ChatMessage(role="user", content="더 보여줘")])
        context = chat.prepare_chat_pipeline(request, "더 보여줘", {"uid": "test-user"})
        response = chat.plan_chat_query(request, context, {"uid": "test-user"}).response
        self.assertIsNotNone(response)
        self.assertEqual(response["raw"]["mode"], "list_context_clarification")

    def test_continuation_words_are_not_keywords_but_quoted_title_is_preserved(self):
        for question in ("나머지도 보여줘", "더 보여줘", "계속 보여줘", "다음 3건 보여줘"):
            with self.subTest(question=question):
                self.assertEqual(build_keyword_terms("멘티스 MTS " + question, extract_query_constraints("MTS")), [])
        constraints = extract_query_constraints('제목에 "더 보여줘" 포함된 MTS 이슈 보여줘')
        self.assertTrue(any(c["field"] == "title" and c["value"] == "더 보여줘" for c in constraints["filter_clauses"]))
