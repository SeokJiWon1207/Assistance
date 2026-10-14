"""해석 안전성, 빠른 경로, 조회 일치 및 확정 조건 상속 회귀."""

import unittest
from unittest.mock import patch

from backend.intent.constraints import extract_query_constraints
from backend.intent.context import build_contextual_retrieval_text
from backend.intent.confirmed_context import confirmed_scope_text
from backend.intent.status_semantics import valid_open_evidence
from backend.schemas import ChatMessage
from backend.services.chat_service import can_skip_general_planner
from backend.sources.mantis import select_mantis_rows
from backend.sources.mantis_collection import IssueBatch
from backend.sources.mantis_store import render_document
from backend.intent.matching import chroma_text_matches_constraints
from backend.common import normalize_chroma_issue_document
from backend.config import CHROMA_DB_PATH
from backend.tools.mantis_snapshot import scan_mantis_documents
from backend.timing import TIMINGS, measured, request_timing


class AccuracyPerformanceTests(unittest.TestCase):
    def test_notion_planner_skip_requires_an_explicit_simple_title_lookup(self):
        for question, expected in (
            ('노션에서 "배포 계획" 항목을 찾아서 알려줘', True),
            ('노션에서 "배포 계획" 찾아줘', True),
            ('노션에서 "배포 계획" 내용 요약해줘', False),
            ('노션에서 "배포 계획" 찾아줘. 보관됨 상태는 빼고', False),
            ('노션에서 배포 계획 찾아줘', False),
            ('노션에서 "배포 계획"과 "다른 계획" 비교해줘', False),
        ):
            self.assertEqual(can_skip_general_planner(question, {}, {"source": "notion"}), expected, question)

    def test_evidence_requires_more_than_entity_or_generic_noun(self):
        for q, e in (("NH 이슈 몇 건", "NH"), ("NH 이슈 몇 건", "이슈"),
                     ("NH 이슈 몇 건", "NH 이슈"), ("삼성증권 이슈", "삼성증권"),
                     ("아직 손봐야 할 이슈는 아니야", "아직 손봐야 할 이슈"),
                     ("NH 완료된 이슈", "완료된 이슈"), ("'마무리 못 한' 제목 검색", "마무리 못 한"),
                     ("남은 공수", "남은 공수")):
            self.assertFalse(valid_open_evidence(q, e), (q, e))
        for e in ("아직 손봐야 할 이슈", "마무리 못 한 것", "안 끝난 건"):
            self.assertTrue(valid_open_evidence("NH " + e, e), e)

    def test_fast_path_only_for_unambiguous_queries(self):
        for question, expected in (("이슈 0021371 알려줘", True),
                                   ("0021371과 비슷한 이슈", False),
                                   ("0021371 원인 분석해줘", False),
                                   ("NH MTS 미종료 이슈 몇 건이야", True),
                                   ("NH MTS 전체 상태로 몇 건이야", True),
                                   ("NH 로그인 장애 몇 건이야", False),
                                   ("NH 담당자별 건수", False)):
            self.assertEqual(can_skip_general_planner(question, extract_query_constraints(question),
                {"source": "mantis"}), expected, question)
        q = "NH mts 그중 전체 상태로 몇 건이야?"
        self.assertTrue(can_skip_general_planner(q, extract_query_constraints(q), {"source": "general"}))
        self.assertFalse(can_skip_general_planner(q, extract_query_constraints(q), {"source": "notion"}))

    def test_raw_count_and_list_share_exact_ids_and_keep_callback_contract(self):
        rows = [{"id": i, "project": {"name": "120NH(NH증권)"}, "summary": "MTS 차트 문의",
                 "handler": {"real_name": "석지원"}, "status": {"name": s}, "custom_fields": [{"field": {"name": "매체구분"}, "value": media}]}
                for i, (s, media) in enumerate((("assigned", ""), ("closed", "MTS"),
                                               ("developed", "MTS"), ("new", "HTS")), 1)]
        for q, expected in (("NH MTS 미종료 이슈", {1, 3}), ("NH 종료 이슈", {2}),
                            ("NH HTS 이슈", {4}), ("NH MTS 제외 이슈", {4})):
            c = extract_query_constraints(q)
            self.assertEqual({r["id"] for r in select_mantis_rows(rows, c)}, expected)
            legacy = {r["id"] for r in rows if chroma_text_matches_constraints(render_document(r), c)}
            self.assertEqual(expected, legacy, q)
            with patch("backend.sources.mantis_store.read_complete_store", return_value=IssueBatch(rows, {})):
                result = scan_mantis_documents(CHROMA_DB_PATH, lambda _: False, normalize_chroma_issue_document,
                    constraints=c, use_raw_constraints=True)
                original = scan_mantis_documents(CHROMA_DB_PATH, lambda _: False, normalize_chroma_issue_document, constraints=c)
            self.assertEqual(result["matched_ids"], expected)
            self.assertEqual(result["scanned_count"], 4)
            self.assertEqual(original["matched_count"], 0)

    def test_confirmed_conditions_survive_multiple_followups(self):
        constraints = extract_query_constraints("NH MTS 미종료 담당자 이영희 2025년 3월 이슈")
        for latest, customer in (("그중 KB만 보여줘", "kb"), ("그중 전체 상태 보여줘", "kb")):
            text, _ = build_contextual_retrieval_text([
                ChatMessage(role="user", content=confirmed_scope_text(constraints)),
                ChatMessage(role="user", content=latest)])
            constraints = extract_query_constraints(text)
            self.assertEqual(constraints["customer_key"], customer)
            self.assertEqual(constraints["platform_key"], "mts")
            self.assertEqual(constraints["people"]["names"], ["이영희"])
            self.assertEqual(constraints["created_at"]["from"], "2025-03-01")
        self.assertEqual(constraints["statuses"], [])

    def test_independent_customer_count_does_not_inherit_previous_issue_id(self):
        text, _ = build_contextual_retrieval_text([
            ChatMessage(role="user", content="이슈 0021371 알려줘"),
            ChatMessage(role="user", content="NH MTS 미종료 이슈 몇 건이야?")])
        self.assertEqual(extract_query_constraints(text)["issue_ids"], [])

    def test_saved_scope_keeps_exclusions_and_unassigned_but_replaces_screen(self):
        prior = extract_query_constraints("우리 팀 NH 제외 MTS 제외 담당자 미지정 화면번호 0090 이슈")
        text, _ = build_contextual_retrieval_text([
            ChatMessage(role="user", content=confirmed_scope_text(prior)),
            ChatMessage(role="user", content="그중 화면번호 0080만 보여줘")])
        current = extract_query_constraints(text)
        self.assertEqual(current["screen_numbers"], ["0080"])
        self.assertEqual(current["excluded_customer_keys"], prior["excluded_customer_keys"])
        self.assertEqual(current["excluded_platform_keys"], prior["excluded_platform_keys"])
        self.assertTrue(current["unassigned_people"])
        self.assertEqual(current["mantis_team_members"], prior["mantis_team_members"])

    def test_timings_are_isolated_and_record_failures(self):
        @measured("work")
        def work():
            raise ValueError("test")

        @request_timing
        def request():
            self.assertEqual(TIMINGS.get(), {})
            with self.assertRaises(ValueError):
                work()
            return dict(TIMINGS.get())
        for _ in range(2):
            self.assertEqual(request()["work"]["calls"], 1)
            self.assertIsNone(TIMINGS.get())
