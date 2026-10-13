"""전체 목록 뒤 매체·월 변경도 원문 표와 전체 표시를 유지한다."""
import unittest
from unittest.mock import patch

from backend.intent.constraints import extract_query_constraints
from backend.intent.scope_changes import has_scope_edit
from backend.schemas import ChatMessage, ChatRequest
from backend.services import chat_service as chat


BASE = "2026년 10월에 등록된 MTS 이슈 전체 목록을 보여줘. 이슈 번호, 제목, 담당자, 등록일을 모두 표시해줘."


class AllListScopeFollowupTests(unittest.TestCase):
    def test_month_only_change_is_a_scope_edit(self):
        self.assertTrue(has_scope_edit("9월로 바꿔줘"))
        self.assertTrue(has_scope_edit("2025년 9월로 변경해줘"))
        self.assertFalse(has_scope_edit("9월로 바꿔달라는 이슈 있었어?"))
        self.assertFalse(has_scope_edit('제목에 "9월로 바꿔줘" 포함된 이슈'))

    def prepare(self, previous_question, plan, question):
        request = ChatRequest(modelId="fixture", contextAuditId="a" * 32, searchLimit=5, messages=[
            ChatMessage(role="user",content=previous_question),
            ChatMessage(role="assistant",content="원문 목록"),
            ChatMessage(role="user",content=question),
        ])
        with (
            patch.object(chat,"get_private_model",return_value={"id":"fixture","status":"active"}),
            patch.object(chat,"get_model_api_key",return_value="fixture-key"),
            patch.object(chat,"load_audit_query_plan",return_value=plan),
            patch.object(chat,"resolve_reference_from_context",return_value=None),
            patch.object(chat,"rewrite_follow_up_query_with_model",side_effect=AssertionError("모델 재작성 금지")),
        ):
            return request, chat.prepare_chat_pipeline(request,question,{"uid":"fixture-user"})

    def test_entire_list_and_original_table_survive_two_scope_changes(self):
        plan = {"source":"mantis","grounding_text":BASE,"terms":[],
                "resolved_constraints":extract_query_constraints(BASE)}
        previous = BASE
        for question,month in (("HTS로 바꿔줘","10"),("9월로 바꿔줘","09")):
            with self.subTest(question=question):
                request, context = self.prepare(previous,plan,question)
                self.assertEqual(context.query_constraints["platform_key"],"hts")
                self.assertEqual(context.query_constraints["created_at"]["from"],f"2026-{month}-01")
                self.assertTrue(context.query_constraints.get("list_show_all"))
                with (
                    patch("backend.reports.work_queries.build_work_query_report",side_effect=AssertionError("업무 요약으로 우회 금지")),
                    patch.object(chat,"resolve_status_intent",return_value={"status_intent":None}),
                    patch.object(chat,"_plan_chat_query",return_value=None),
                ):
                    self.assertIsNone(chat.plan_chat_query(request,context,None).response)
                self.assertTrue(chat.can_skip_general_planner(context.retrieval_text, context.query_constraints, {"source":"mantis"}))
                limit,_ = chat.retrieval_limit(request,context)
                self.assertGreaterEqual(limit,74)
                rows = [{"id":32000+i,"summary":f"[3600] 원문 제목 {i}","project":"120NH(NH증권)",
                         "handler":"이영희","media_type":"HTS","created_at":f"2026-{month}-02"} for i in range(74)]
                context.retrieval_result={"query":context.retrieval_text,"constraints":context.query_constraints,
                                          "stages":[{"source":"mantis_page_scan","status":"ok","issues_list":rows,
                                                     "meta":{"found_count":74,"search_complete":True}}]}
                response=chat.deterministic_retrieval_response(request,context,limit,None)
                self.assertIsNotNone(response)
                self.assertEqual(response["raw"]["mode"],"deterministic_issue_list")
                self.assertEqual(len(response["sourcesForFollowUps"]),74)
                self.assertIn("등록일",response["content"])
                self.assertIn("원문 제목 73",response["content"])
                plan={"source":"mantis","grounding_text":question,"terms":[],"resolved_constraints":context.query_constraints}
                previous=question

    def test_explicit_count_overrides_inherited_entire_list(self):
        plan={"source":"mantis","grounding_text":BASE,"terms":[],"resolved_constraints":extract_query_constraints(BASE)}
        request,context=self.prepare(BASE,plan,"HTS로 바꿔줘. 최근 3건만 보여줘")
        self.assertFalse(context.query_constraints.get("list_show_all"))
        self.assertEqual(chat.retrieval_limit(request,context)[0],3)

    def test_default_count_is_kept_when_the_previous_query_was_not_entire(self):
        previous="2026년 10월에 등록된 MTS 이슈 최근 5건 보여줘"
        plan={"source":"mantis","grounding_text":previous,"terms":[],"resolved_constraints":extract_query_constraints(previous)}
        request,context=self.prepare(previous,plan,"HTS로 바꿔줘")
        self.assertFalse(context.query_constraints.get("list_show_all"))
        self.assertEqual(chat.retrieval_limit(request,context)[0],5)
