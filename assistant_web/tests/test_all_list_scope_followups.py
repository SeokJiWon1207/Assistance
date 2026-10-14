"""전체 목록 뒤 매체·월 변경도 원문 표와 전체 표시를 유지한다."""
import unittest
from unittest.mock import patch

from backend.intent.constraints import extract_query_constraints
from backend.intent.context import is_follow_up_question
from backend.intent.search_terms import build_keyword_terms
from backend.intent.scope_changes import has_scope_edit
from backend.schemas import ChatMessage, ChatRequest
from backend.services import chat_service as chat
from backend.sources.mantis import select_mantis_rows


BASE = "2026년 10월에 등록된 MTS 이슈 전체 목록을 보여줘. 이슈 번호, 제목, 담당자, 등록일을 모두 표시해줘."


class AllListScopeFollowupTests(unittest.TestCase):
    def test_retry_after_unanswered_question_inherits_last_completed_answer_scope(self):
        previous = "담당자 조건 해제해줘"
        question = "기간 조건 없이 보여줘"
        scope = {**extract_query_constraints("2026년 10월 전사 MTS 이슈 전체 목록 보여줘"), "list_show_all": True}
        plan = {"source": "mantis", "grounding_text": previous, "terms": [], "resolved_constraints": scope}
        completed = [ChatMessage(role="user", content=previous), ChatMessage(role="assistant", content="원문 목록")]
        older = [ChatMessage(role="user", content="NH증권 MTS 담당자 이영희 이슈 보여줘"),
                 ChatMessage(role="assistant", content="이전 원문 목록")]
        retry = ChatMessage(role="user", content=question)
        for history in (completed + [retry],
                        completed + [retry, ChatMessage(role="assistant", content="오류: 요청 중지")],
                        older + [ChatMessage(role="user", content=previous), retry]):
            with self.subTest(history=[message.role for message in history]):
                request = ChatRequest(modelId="fixture", contextAuditId="a" * 32, messages=[*history, retry])
                with (
                    patch.object(chat, "get_private_model", return_value={"id":"fixture", "status":"active"}),
                    patch.object(chat, "get_model_api_key", return_value="fixture-key"),
                    patch.object(chat, "load_audit_query_plan", side_effect=lambda aid, uid, expected: plan if expected == previous else {}) as load,
                    patch.object(chat, "resolve_reference_from_context", return_value=None),
                    patch.object(chat, "rewrite_follow_up_query_with_model", return_value=question),
                ):
                    context = chat.prepare_chat_pipeline(request, question, {"uid":"fixture-user"})
                self.assertEqual(load.call_args.args, ("a" * 32, "fixture-user", previous))
                self.assertEqual(context.query_constraints["platform_key"], "mts")
                self.assertIsNone(context.query_constraints.get("created_at"))
                self.assertFalse(context.query_constraints.get("customer_key"))
                self.assertFalse((context.query_constraints.get("people") or {}).get("names"))
                self.assertTrue(context.query_constraints["list_show_all"])
                self.assertEqual(context.query_constraints["inherited_from_audit_id"], "a" * 32)

    def test_clearing_three_filters_keeps_other_scope_and_full_list_without_keywords(self):
        previous = "2026년 10월 전사 NH증권 MTS 담당자 이영희 이슈 전체 목록 보여줘"
        plan = {"source": "mantis", "grounding_text": previous, "terms": [],
                "resolved_constraints": {**extract_query_constraints(previous), "list_show_all": True}}
        rows = [{"id": i, "summary": "차트 조회", "project": {"name": customer},
                 "handler": {"real_name": owner}, "created_at": date,
                 "custom_fields": [{"field": {"name": "매체구분"}, "value": platform}]}
                for i, customer, owner, date, platform in (
                    (1, "120NH(NH증권)", "이영희", "2026-10-08", "MTS"),
                    (2, "112KBStar(KB증권)", "이영희", "2026-10-08", "MTS"),
                    (3, "120NH(NH증권)", "우동헌", "2026-10-08", "MTS"),
                    (4, "120NH(NH증권)", "이영희", "2026-09-08", "MTS"),
                    (5, "120NH(NH증권)", "이영희", "2026-10-08", "HTS"))]
        for question, expected in (("고객사 조건 빼줘", [1, 2]),
                                   ("담당자 조건 해제해줘", [1, 2, 3]),
                                   ("기간 조건 없이 보여줘", [1, 2, 3, 4])):
            with self.subTest(question=question):
                request, context = self.prepare(previous, plan, question)
                constraints = context.query_constraints
                self.assertEqual([row["id"] for row in select_mantis_rows(rows, constraints)], expected)
                self.assertEqual(constraints["platform_key"], "mts")
                self.assertEqual(constraints.get("mantis_team_scope"), "all")
                self.assertTrue(constraints.get("list_show_all"))
                self.assertEqual(chat.retrieval_limit(request, context)[0], 200)
                self.assertEqual(build_keyword_terms(context.retrieval_text, constraints), [])
                self.assertFalse(any(c["field"] == "keyword" for c in constraints.get("filter_clauses", [])))
                plan = {"source": "mantis", "grounding_text": question, "terms": [],
                        "resolved_constraints": constraints}
                previous = question

    def test_clear_instruction_preserves_a_confirmed_issue_topic(self):
        previous = "2026년 10월 전사 NH증권 MTS MACD 이슈 전체 목록 보여줘"
        constraints = {**extract_query_constraints(previous), "list_show_all": True}
        plan = {"source": "mantis", "grounding_text": previous, "terms": ["MACD"],
                "resolved_constraints": constraints}
        _, context = self.prepare(previous, plan, "고객사 조건 빼줘")
        self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), ["MACD"])

    def test_quoted_clear_instruction_is_still_an_issue_search_term(self):
        question = '제목에 "고객사 조건 빼줘" 포함된 이슈 보여줘'
        constraints = extract_query_constraints(question)
        self.assertIn("빼줘", build_keyword_terms(question, constraints))
        self.assertTrue(any(c["field"] == "title" and c["value"] == "고객사 조건 빼줘"
                            for c in constraints.get("filter_clauses", [])))

    def test_plain_team_scope_changes_keep_the_confirmed_period_and_entire_list(self):
        plan = {"source": "mantis", "grounding_text": BASE, "terms": [],
                "resolved_constraints": extract_query_constraints(BASE)}
        for question in ("팀 범위 해제하고 전사 전체로 보여줘", "팀 범위 해제해줘",
                         "팀 범위 제거해줘", "팀 제한 제거해주세요",
                         "팀 범위 해제해 주세요", "팀 범위 해제해 줘",
                         "전사 기준으로 보여줘", "전사 전체로 보여줘", "전체 팀으로 보여줘"):
            with self.subTest(question=question):
                self.assertTrue(is_follow_up_question(question))
                request, context = self.prepare(BASE, plan, question)
                self.assertEqual(context.query_constraints["platform_key"], "mts")
                self.assertEqual(context.query_constraints["created_at"]["from"], "2026-10-01")
                self.assertEqual(context.query_constraints.get("mantis_team_scope"), "all")
                self.assertTrue(context.query_constraints.get("list_show_all"))
                self.assertEqual(chat.retrieval_limit(request, context)[0], 200)
                self.assertEqual(build_keyword_terms(context.retrieval_text, context.query_constraints), [])
                next_plan = {"source": "mantis", "grounding_text": question, "terms": [],
                             "resolved_constraints": context.query_constraints}
                _, next_context = self.prepare(question, next_plan, "HTS로 바꿔줘")
                self.assertEqual(next_context.query_constraints["platform_key"], "hts")
                self.assertEqual(next_context.query_constraints["created_at"]["from"], "2026-10-01")
                self.assertTrue(next_context.query_constraints.get("list_show_all"))

    def test_team_scope_topics_are_not_scope_edits(self):
        for question in ('제목에 "팀 범위 해제하고 전사 전체로 보여줘" 포함된 이슈',
                         '제목에 "팀 범위 제거해줘" 포함된 이슈',
                         "팀 범위 해제 요청이 들어온 이슈 있었어?"):
            with self.subTest(question=question):
                self.assertFalse(has_scope_edit(question))

    def test_unquoted_team_removal_topic_keeps_the_default_team(self):
        question = "팀 범위 제거 요청이 들어온 이슈 보여줘"
        self.assertFalse(has_scope_edit(question))
        self.assertEqual(extract_query_constraints(question).get("mantis_team_scope"), "default")

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

    def test_entire_list_and_original_table_survive_four_scope_changes(self):
        plan = {"source":"mantis","grounding_text":BASE,"terms":[],
                "resolved_constraints":extract_query_constraints(BASE)}
        previous = BASE
        for question,month in (("HTS로 바꿔줘","10"),("9월로 바꿔줘","09"),
                               ("NH증권만 보여줘","09"),("담당자는 우동헌으로 바꿔줘","09")):
            with self.subTest(question=question):
                request, context = self.prepare(previous,plan,question)
                self.assertEqual(context.query_constraints["platform_key"],"hts")
                self.assertEqual(context.query_constraints["created_at"]["from"],f"2026-{month}-01")
                if question in ("NH증권만 보여줘","담당자는 우동헌으로 바꿔줘"):
                    self.assertEqual(context.query_constraints["customer_key"],"NH증권")
                if question == "담당자는 우동헌으로 바꿔줘":
                    self.assertEqual(context.query_constraints["people"]["names"],["우동헌"])
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
                         "handler":"우동헌","media_type":"HTS","created_at":f"2026-{month}-02"} for i in range(74)]
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

    def test_team_and_global_scope_changes_keep_the_entire_list(self):
        plan = {"source": "mantis", "grounding_text": BASE, "terms": [],
                "resolved_constraints": extract_query_constraints(BASE)}
        previous = BASE
        for question in ("그중 우리 팀 이슈 팀 범위 해제", "그중 전사 기준으로 보여줘", "그중 우리 팀만 보여줘"):
            with self.subTest(question=question):
                request, context = self.prepare(previous, plan, question)
                self.assertTrue(context.query_constraints.get("list_show_all"))
                self.assertEqual(chat.retrieval_limit(request, context)[0], 200)
                self.assertEqual(context.query_constraints["platform_key"], "mts")
                self.assertEqual(context.query_constraints["created_at"]["from"], "2026-10-01")
                plan = {"source": "mantis", "grounding_text": question, "terms": [],
                        "resolved_constraints": context.query_constraints}
                previous = question
        request, context = self.prepare(previous, plan, "그중 전사 기준으로 최근 3건만 보여줘")
        self.assertFalse(context.query_constraints.get("list_show_all"))
        self.assertEqual(chat.retrieval_limit(request, context)[0], 3)
