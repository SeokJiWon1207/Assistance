"""10월 목록 이후 전체 표시 요청이 검색 없이 가짜 행을 만들던 오류 회귀."""

import unittest
from unittest.mock import patch

from backend.answers import format_deterministic_issue_answer
from backend.evaluation.delivery import decide_delivery
from backend.evaluation.verification import build_verification_report
from backend.intent.context import build_contextual_retrieval_text, is_follow_up_question
from backend.intent.constraints import extract_query_constraints
from backend.schemas import ChatMessage, ChatRequest
from backend.services import chat_service as chat
from backend.services.contracts import ChatPipelineContext
from backend.sources.mantis import build_sources_from_issue_rows


class OctoberAllFollowupTests(unittest.TestCase):
    def test_all_display_requests_preserve_previous_month_and_platform(self):
        for followup in ("모두 보여줘", "전부 보여줘", "다 보여줘", "전체 목록 보여줘"):
            with self.subTest(followup=followup):
                self.assertTrue(is_follow_up_question(followup))
                query, rewrite = build_contextual_retrieval_text([
                    ChatMessage(role="user", content="2026년 10월 MTS 접수된거 보여줘"),
                    ChatMessage(role="assistant", content="검색 결과 17건 중 5건을 표시했습니다."),
                    ChatMessage(role="user", content=followup),
                ])
                constraints = extract_query_constraints(query)
                self.assertEqual(constraints["platform_key"], "mts")
                self.assertEqual(constraints["created_at"]["from"], "2026-10-01")
                self.assertFalse(rewrite)
                context = ChatPipelineContext(followup, {"id": "fixture"}, "", [], query,
                                              {"mantis": True}, [], constraints, 5,
                                              intent_plan={"source": "mantis", "limit": 5})
                self.assertTrue(chat.should_retrieve_chat_evidence(context))
                request = ChatRequest(modelId="fixture", messages=[{"role": "user", "content": followup}])
                limit, _ = chat.retrieval_limit(request, context)
                self.assertGreaterEqual(limit, 17)
                rows = [{"id": 32000+i, "summary": f"[MTS] 원문 제목 {i}", "handler": f"원문담당{i}",
                         "project": "KB증권", "media_type": "MTS", "created_at": "2026-10-07"}
                        for i in range(17)]
                result = {"query": query, "constraints": constraints,
                          "stages": [{"source": "mantis_page_scan", "status": "ok", "issues_list": rows,
                                      "meta": {"found_count": 17, "search_complete": True}}]}
                with patch.object(chat, "run_retrieval_pipeline", return_value=result):
                    response = chat.retrieve_chat_evidence(request, context).response
                self.assertIsNotNone(response)
                self.assertEqual(response["raw"]["mode"], "deterministic_issue_list")
                self.assertEqual(len(response["sourcesForFollowUps"]), 17)
                self.assertIn("원문담당16", response["content"])

    def test_new_explicit_customer_all_query_is_not_inherited(self):
        self.assertFalse(is_follow_up_question("KB증권 이슈 모두 보여줘"))

    def test_original_numbered_list_without_sources_is_blocked(self):
        content = ("1. **ID**: 31595 | **프로젝트**: NH투자증권 | **제목**: [MTS] 가짜 제목 | "
                   "**담당자**: 김민수 | **등록일**: 2026-10-06")
        report = build_verification_report("모두 보여줘", content, [], None, "model")
        self.assertIn("issue_ids", decide_delivery(report).blocked_checks)

    def test_known_id_with_false_fields_is_blocked_but_original_is_delivered(self):
        row = {"id": 31585, "project": "신한투자증권", "summary": "[MTS] 전략추가·조건찾기·조건설정 화면 UI 개선",
               "handler": "석지원", "created_at": "2026-10-06", "media_type": "MTS"}
        result = {"query": "10월 MTS 이슈", "constraints": {}, "stages": [
            {"source": "mantis_page_scan", "status": "ok", "issues_list": [row], "meta": {"found_count": 1}}]}
        sources = build_sources_from_issue_rows([row])
        content = ("| 이슈 번호 | 제목 | 담당자 | 등록일 |\n|---|---|---|---|\n"
                   "| 31585 | [MTS] 로그인 유지 시간 초과 시 재인증 오류 | 하영훈 | 2026-10-04 |")
        report = build_verification_report("이슈 조회", content, sources, result, "model")
        self.assertIn("issue_fields", decide_delivery(report).blocked_checks)
        correct = format_deterministic_issue_answer("이슈 보여줘", {}, result, 20, "normal")
        report = build_verification_report("이슈 조회", correct, sources, result, "deterministic_issue_list")
        self.assertTrue(decide_delivery(report).allowed, report)
