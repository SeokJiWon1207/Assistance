"""운영 웹에서 재현한 고객사 축소와 담당자 변경의 조건 보존 회귀."""
import unittest
from unittest.mock import patch

from backend.intent.constraints import extract_query_constraints
from backend.intent.context import build_contextual_retrieval_text, is_follow_up_question
from backend.intent.entities import extract_people_constraints
from backend.intent.people import grounded_model_people
from backend.intent.search_terms import build_keyword_terms
from backend.intent.scope_changes import has_scope_edit
from backend.reports.work_queries import work_operation, build_work_query_report
from backend.sources.mantis_collection import IssueBatch
from backend.schemas import ChatMessage, ChatRequest
from backend.services import chat_service as chat


BASE = "2026년 10월 MTS 접수된 이슈 최근 5건 보여줘"


class FollowupWebRefinementTests(unittest.TestCase):
    def test_customer_only_refinement_preserves_the_period_and_platform(self):
        for question in ("NH증권만 보여줘", "고객사는 NH증권만", "NH투자증권 이슈만 알려줘"):
            with self.subTest(question=question):
                text, rewrite = build_contextual_retrieval_text([
                    ChatMessage(role="user", content=BASE), ChatMessage(role="user", content=question),
                ])
                actual = extract_query_constraints(text)
                self.assertFalse(rewrite)
                self.assertEqual(actual["platform_key"], "mts")
                self.assertEqual(actual["created_at"]["from"], "2026-10-01")
                self.assertEqual(actual["created_at"]["to"], "2026-10-31")
                self.assertIn("NH", actual["customer_aliases"])

    def test_complete_new_query_does_not_inherit_the_previous_scope(self):
        for text in ("NH MTS 이슈 몇 건이야", "NH 차트 이슈만 보여줘", "#31572만 보여줘"):
            with self.subTest(text=text):
                self.assertFalse(is_follow_up_question(text))

    def test_person_direction_particle_is_not_an_additional_name(self):
        with patch("backend.intent.entities.load_known_people", return_value=(("이영희", "이지은", "이한로"), ())):
            for text, expected in (("담당자는 이영희로 바꿔줘", "이영희"),
                                   ("담당자를 이지은으로 변경해줘", "이지은"),
                                   ("담당자는 이한로 바꿔줘", "이한로"),
                                   ("담당자는 김도현으로 바꿔줘", "김도현")):
                with self.subTest(text=text):
                    self.assertEqual(extract_people_constraints(text)["names"], [expected])

    def test_model_cannot_reintroduce_a_name_with_the_direction_particle(self):
        people = {"names": ["이영희"], "role": "handler", "name_origins": {"이영희": "known"}, "candidates": []}
        actual = grounded_model_people("담당자는 이영희로 바꿔줘", people, ["이영희로"], "handler")
        self.assertEqual(actual["names"], ["이영희"])

    def test_bare_platform_change_is_a_filter_edit_and_keeps_a_confirmed_list(self):
        question = "HTS로 바꿔줘"
        self.assertTrue(has_scope_edit(question))
        constraints = extract_query_constraints(question)
        constraints["inherited_from_audit_id"] = "a" * 32
        self.assertEqual(work_operation(question, constraints), "list")
        self.assertFalse(has_scope_edit("HTS에서 VBScript를 JavaScript로 바꿔달라는 이슈 있었어?"))

    def test_owner_change_in_a_confirmed_search_returns_a_list_without_edit_terms(self):
        previous = "2026년 10월 NH증권 MTS 이슈 최근 5건 보여줘"
        question = "담당자는 이영희로 바꿔줘"
        plan = {"source": "mantis", "terms": [], "resolved_constraints": extract_query_constraints(previous)}
        request = ChatRequest(modelId="fixture", contextAuditId="a" * 32, messages=[
            ChatMessage(role="user", content=previous), ChatMessage(role="assistant", content="검색 결과"),
            ChatMessage(role="user", content=question),
        ])
        with (
            patch.object(chat, "get_private_model", return_value={"id": "fixture", "status": "active"}),
            patch.object(chat, "get_model_api_key", return_value="fixture-key"),
            patch.object(chat, "load_audit_query_plan", return_value=plan),
            patch.object(chat, "resolve_reference_from_context", return_value=None),
            patch.object(chat, "rewrite_follow_up_query_with_model", side_effect=AssertionError("모델 재작성 금지")),
        ):
            context = chat.prepare_chat_pipeline(request, question, {"uid": "fixture-user"})
        constraints = context.query_constraints
        self.assertEqual(constraints["platform_key"], "mts")
        self.assertEqual(constraints["created_at"]["from"], "2026-10-01")
        self.assertEqual(constraints["people"]["names"], ["이영희"])
        self.assertEqual(build_keyword_terms(context.retrieval_text, constraints), [])
        self.assertEqual(work_operation(question, constraints), "list")
        self.assertIsNone(work_operation(question, extract_query_constraints(question)))

    def test_confirmed_filter_edit_does_not_add_an_open_status_filter(self):
        constraints = extract_query_constraints("2025년 NH증권 MTS 담당자 이영희 이슈")
        constraints["inherited_from_audit_id"] = "a" * 32
        rows = IssueBatch([{
            "id": 901, "summary": "[MTS] 종료된 차트 이슈", "project": {"name": "120NH(NH증권)"},
            "handler": {"real_name": "이영희"}, "status": {"name": "closed"},
            "created_at": "2025-12-18T01:00:00Z",
            "custom_fields": [{"field": {"name": "매체구분"}, "value": "MTS"}],
        }], {"search_complete": True, "snapshot_complete": True})
        with patch("backend.reports.work_queries.read_complete_store", return_value=rows):
            report = build_work_query_report("담당자는 이영희로 바꿔줘", constraints)
        self.assertEqual(report["retrieval"]["constraints"]["statuses"], [])
        self.assertEqual([source["issue_id"] for source in report["sources"]], [901])
        self.assertNotIn("미종료 업무", report["content"])
