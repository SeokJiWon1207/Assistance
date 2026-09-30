"""팀 채팅과 대시보드가 같은 담당자 명단을 사용해야 한다."""

import unittest
from unittest.mock import patch

from backend.intent.constraints import extract_query_constraints, has_constraint, merge_query_plan_constraints
from backend.intent.context import build_contextual_retrieval_text
from backend.intent.matching import chroma_text_matches_constraints
from backend.intent.search_terms import build_keyword_terms
from backend.reports.aggregate import format_issue_list_criteria
from backend.schemas import ChatMessage, ChatRequest
from backend.services.chat_service import prepare_chat_pipeline, should_scope_to_authenticated_mantis_user
from backend.sources.mantis import issue_matches_constraints
from backend.team_scope import TEAM_MEMBERS, TEAM_SCOPE_LABEL


def messages(*texts):
    return [ChatMessage(role="user", content=text) for text in texts]


class TeamChatScopeTest(unittest.TestCase):
    def test_team_aliases_use_shared_roster_without_customer_or_keywords(self):
        for prefix in ("우리 팀", "우리팀", "저희 팀", "내 팀", "컨텐츠서비스팀", "콘텐츠서비스팀", "CS팀", "우리 컨텐츠서비스팀"):
            with self.subTest(prefix=prefix):
                text = f"{prefix} 미종료 이슈 보여줘"
                constraints = extract_query_constraints(text)
                self.assertEqual(constraints["mantis_team_members"], list(TEAM_MEMBERS))
                self.assertFalse(constraints["customer_key"])
                self.assertFalse(constraints["people"]["names"])
                self.assertEqual(build_keyword_terms(text, constraints), [])
                self.assertTrue(has_constraint(constraints))

    def test_personal_general_and_notion_queries_remain_unscoped(self):
        for text in ("내 미종료 이슈", "전체 미종료 이슈", "우리선물 이슈", "석지원 담당 이슈", "노션 이번 달 문서", "우리 팀 노션 문서", "작업주체 CS팀 이슈", "프로젝트 컨텐츠서비스팀 이슈"):
            with self.subTest(text=text):
                self.assertNotIn("mantis_team_members", extract_query_constraints(text))

    def test_team_weekly_report_is_not_personal(self):
        self.assertFalse(should_scope_to_authenticated_mantis_user("우리 팀 주간업무보고 작성해줘"))
        self.assertTrue(should_scope_to_authenticated_mantis_user("내 미종료 이슈"))

    def test_live_and_snapshot_match_exact_handler_not_reporter_or_body(self):
        constraints = extract_query_constraints("우리 팀 미종료 이슈")
        for handler in (*TEAM_MEMBERS, "타팀담당", "석지원_보조", "공용", ""):
            with self.subTest(handler=handler):
                issue = {"id": 12345, "handler": {"real_name": handler}, "reporter": {"real_name": "석지원"},
                         "summary": "석지원 요청", "status": {"name": "assigned"}}
                document = f"- 이슈 ID: #12345\n- 담당자: {handler}\n- 현재 상태: assigned\n- 요청자: 석지원\n[제목]\n석지원 요청"
                self.assertEqual(issue_matches_constraints(issue, constraints), handler in TEAM_MEMBERS)
                self.assertEqual(chroma_text_matches_constraints(document, constraints), handler in TEAM_MEMBERS)

    def test_closed_and_resolved_are_excluded(self):
        constraints = extract_query_constraints("우리 팀 미종료 이슈")
        for status in ("closed", "resolved"):
            self.assertFalse(issue_matches_constraints({"id": 12345, "handler": {"real_name": "석지원"}, "status": {"name": status}}, constraints))

    def test_follow_up_chain_keeps_roster_and_status(self):
        text, rewrite = build_contextual_retrieval_text(messages("우리 팀 미종료 이슈 몇 건이야", "그중 담당자별로 몇 건이야", "그중 하영훈 담당만 보여줘"))
        constraints = extract_query_constraints(text)
        self.assertFalse(rewrite)
        self.assertEqual(constraints["mantis_team_members"], list(TEAM_MEMBERS))
        self.assertEqual(constraints["statuses"], ["open"])
        self.assertEqual(constraints["people"]["names"], ["하영훈"])
        self.assertEqual(build_keyword_terms(text, constraints), [])

    def test_scope_does_not_cross_new_question_or_explicit_reset(self):
        for texts in (("우리 팀 이슈", "전체 이슈 보여줘"), ("우리 팀 이슈", "그중 전사 기준으로 보여줘"),
                      ("우리 팀 이슈", "노션 문서 보여줘", "그중 최근 것만"),
                      ("우리 팀 이슈", "신한 이슈 보여줘", "그중 미종료만")):
            with self.subTest(texts=texts):
                text, _ = build_contextual_retrieval_text(messages(*texts))
                self.assertNotIn("mantis_team_members", extract_query_constraints(text))

    def test_planner_cannot_remove_team_constraint(self):
        constraints = merge_query_plan_constraints(extract_query_constraints("우리 팀 미종료 이슈"), {"filters": {"statuses": ["open"]}})
        self.assertEqual(constraints["mantis_team_members"], list(TEAM_MEMBERS))
        self.assertIn(TEAM_SCOPE_LABEL, format_issue_list_criteria(constraints, []))

    def test_resolved_reference_keeps_team_constraint(self):
        request = ChatRequest(modelId="test", messages=messages("우리 팀 미종료 이슈 보여줘", "그중 첫 번째 자세히"))
        with patch("backend.services.chat_service.get_private_model", return_value={"status": "active"}), \
             patch("backend.services.chat_service.get_model_api_key", return_value="test"), \
             patch("backend.services.chat_service.resolve_reference_from_context", return_value="0012345 자세히"):
            context = prepare_chat_pipeline(request, request.messages[-1].content, {"mantisUsername": "석지원"})
        self.assertEqual(context.query_constraints["mantis_team_members"], list(TEAM_MEMBERS))
        self.assertEqual(context.query_constraints["issue_ids"], [12345])
        self.assertEqual(context.authenticated_owner, "")

    def test_old_notion_turn_does_not_replace_team_follow_up(self):
        request = ChatRequest(modelId="test", messages=messages(
            "노션 DB03_작업 문서 보여줘", "우리 팀 미종료 이슈 보여줘", "그중 하영훈 담당만 보여줘",
        ))
        with patch("backend.services.chat_service.get_private_model", return_value={"status": "active"}), \
             patch("backend.services.chat_service.get_model_api_key", return_value="test"):
            context = prepare_chat_pipeline(request, request.messages[-1].content, None)
        self.assertEqual(context.query_constraints["mantis_team_members"], list(TEAM_MEMBERS))
        self.assertNotIn("노션", context.retrieval_text)
        self.assertEqual(context.query_constraints["people"]["names"], ["하영훈"])
