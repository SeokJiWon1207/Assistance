"""실제 웹 QA 32문항과 의미별 변형의 재발 방지."""

import unittest
from copy import deepcopy
from datetime import date
from unittest.mock import patch

from backend.intent.constraints import extract_query_constraints, merge_query_plan_constraints
from backend.intent.confirmed_context import merge_confirmed_constraints
from backend.intent.context import is_follow_up_question
from backend.reports.work_queries import build_work_query_report, verify_work_report, work_operation
from backend.sources.mantis_collection import IssueBatch
from backend.services import chat_service as chat
from backend.schemas import ChatMessage, ChatRequest
from backend.retrieval import build_model_retrieval_context


QUESTIONS = [
    "나 오늘 뭐부터 해야 해?", "우리 아직 못 끝낸 거 얼마나 돼?", "급한 것만 좀 추려줘.",
    "오래 묵혀둔 거 있어?", "이번 주 안에 끝내야 하는 거 뭐야?", "NH 쪽 요즘 어때?",
    "NH 모바일 쪽 아직 남은 거 있어?", "KB에서 요청한 건 다 끝났나?",
    "요즘 어디서 이슈가 제일 많이 들어와?", "차트 쪽 계속 문제 생기는 데가 어디야?",
    "영훈님 지금 뭐 하고 있어?", "우리 팀에서 누가 제일 바빠?", "담당자 안 정해진 것도 있어?",
    "내 건 말고 상신님 건만 보여줘.", "한 사람한테 일이 너무 몰려 있지는 않아?",
    "전에 NH에서 차트 이상하다고 했던 거 있잖아.", "아이폰에서 죽는다는 건 어떻게 됐어?",
    "VWAP 추가해달라는 건 끝났나?", "21371 이거 지금 어디까지 됐어?",
    "이거 예전에도 비슷한 게 있었던 것 같은데?", "지난주보다 좀 줄었어?",
    "이번 주에 뭐 처리했지?", "개발 끝났는데 아직 안 닫힌 거 있어?",
    "고객한테 답 기다리는 거 뭐 있어?", "팀장님께 공유하게 중요한 것만 정리해줘.",
    "그중 우리 팀 것만.", "모바일만 보면?", "끝난 건 빼고.", "왜 아직 남아 있어?",
    "그거 담당자가 누구야?", "아니, NH 말고 KB.", "그럼 지난달은?",
]


def issue(i, owner="이영희", status="assigned", customer="120NH(NH증권)", title="MTS 차트 문의", created="2026-09-15", due=""):
    return {"id": i, "handler": {"real_name": owner}, "project": {"name": customer},
            "status": {"name": status}, "summary": title, "created_at": created,
            "custom_fields": [{"field": {"name": "개발완료 목표일"}, "value": due}] if due else []}


class NaturalWorkQueryTests(unittest.TestCase):
    def setUp(self):
        self.rows = [issue(21371, owner="홍재우"), issue(2), issue(3, status="resolved"),
                     issue(4, owner="하영훈"), issue(5, owner="이상신", status="developed"),
                     issue(6, customer="112KBStar(KB증권)"), issue(7, owner=""),
                     issue(8, title="VWAP 추가 요청", status="confirmed"),
                     issue(9, title="VWAP 추가 요청", status="closed"),
                     issue(10, owner="석지원", created="2020-01-01", due="2026-10-02")]
        self.meta = {"search_complete": True, "snapshot_complete": True, "fetchedAt": "2026-10-01T04:00:00+00:00"}
        self.store = patch("backend.reports.work_queries.read_complete_store", return_value=IssueBatch(self.rows, self.meta))
        self.store.start()
        self.addCleanup(self.store.stop)

    def report(self, q, c=None):
        return build_work_query_report(q, c or extract_query_constraints(q), today=date(2026, 10, 1))

    def ids(self, r):
        return set(r["retrieval"]["stages"][0]["meta"]["matched_ids"])

    def test_all_32_questions_use_grounded_or_explicit_limitation_routes(self):
        parents = {26: 6, 27: 26, 28: 27, 31: 28, 32: 31, 29: 19, 30: 29, 20: 30}
        saved = {}
        for i in [*range(1, 20), 29, 30, 20, *range(21, 29), 31, 32]:
            q = QUESTIONS[i-1]
            c = extract_query_constraints(q)
            if i in parents:
                self.assertTrue(is_follow_up_question(q), q)
                c = merge_confirmed_constraints(saved[parents[i]], c, q)
            r = self.report(q, c)
            self.assertIsNotNone(r, q)
            saved[i] = r["retrieval"]["constraints"]
            v = verify_work_report(q, r["content"], r["sources"], r["retrieval"])
            self.assertNotEqual(v["status"], "fail", (q, v))
        self.assertEqual(saved[27]["customer_key"], "NH")
        self.assertEqual(saved[27]["platform_key"], "mts")
        self.assertTrue(saved[27]["mantis_team_members"])
        self.assertEqual(saved[31]["customer_key"], "kb")
        self.assertEqual(saved[32]["platform_key"], "mts")
        self.assertFalse(saved[32].get("inherited_terms"))

    def test_team_alias_and_short_names_do_not_leak_other_owners(self):
        self.assertNotIn(21371, self.ids(self.report(QUESTIONS[1])))
        self.assertEqual(self.ids(self.report(QUESTIONS[10])), {4})
        self.assertEqual(self.ids(self.report(QUESTIONS[13])), {5})
        self.assertFalse(chat.should_scope_to_authenticated_mantis_user(QUESTIONS[13]))
        self.assertTrue(chat.should_scope_to_authenticated_mantis_user(QUESTIONS[0]))
        self.assertEqual(extract_query_constraints("우리금융 이슈")["mantis_team_members"] if "mantis_team_members" in extract_query_constraints("우리금융 이슈") else None, None)

    def test_not_closed_and_completion_question_do_not_select_closed(self):
        self.assertEqual(self.ids(self.report(QUESTIONS[22])), {5})
        self.assertEqual(self.ids(self.report(QUESTIONS[7])), {6})
        r = self.report(QUESTIONS[17])
        self.assertEqual(self.ids(r), {8, 9})
        self.assertIn("단정하지", r["content"])

    def test_due_dates_are_not_creation_dates(self):
        r = self.report(QUESTIONS[4])
        self.assertEqual(self.ids(r), {10})
        self.assertNotIn("created_at", r["retrieval"]["constraints"])
        self.assertIn("기한 미입력", r["content"])

    def test_duplicate_platform_alias_is_not_an_additional_and_filter(self):
        c = extract_query_constraints(QUESTIONS[6])
        merged = merge_query_plan_constraints(c, {"grounding_text": QUESTIONS[6],
            "filter_clauses": [{"field": "platform", "operator": "contains", "value": "모바일"}]})
        self.assertEqual(merged["filter_clauses"], c["filter_clauses"])

    def test_missing_evidence_and_staleness_never_get_verified_as_current(self):
        with patch("backend.reports.work_queries.read_complete_store", return_value=None):
            r = self.report(QUESTIONS[6])
            self.assertNotIn("0건", r["content"])
        with patch("backend.reports.work_queries.read_complete_store", return_value=IssueBatch(self.rows, {**self.meta, "search_complete": False})):
            r = self.report(QUESTIONS[6])
            self.assertIn("현재 건수로 확정할 수 없습니다", r["content"])
            self.assertEqual(verify_work_report(QUESTIONS[6], r["content"], r["sources"], r["retrieval"])["status"], "warning")

    def test_verification_rejects_forged_count_id_and_owner(self):
        r = self.report(QUESTIONS[10])
        for content, sources in [(r["content"].replace("1건", "1386건"), r["sources"]),
                                 (r["content"] + " #999999", r["sources"]),
                                 (r["content"], [{**s, "handler": "석지원"} for s in r["sources"]])]:
            self.assertEqual(verify_work_report(QUESTIONS[10], content, sources, r["retrieval"])["status"], "fail")

    def test_notion_and_general_chat_are_not_work_queries(self):
        for q in ("노션 마감 일정", "안녕하세요", "파이썬 사용법 알려줘", "NH 남은 공수 알려줘", "마감이라는 단어가 뭐야"):
            self.assertIsNone(work_operation(q, extract_query_constraints(q)))

    def test_ambiguous_short_name_requests_disambiguation(self):
        with patch("backend.intent.entities.load_known_people", return_value=(("김영훈", "하영훈"), ())):
            r = self.report("영훈님 지금 뭐 하고 있어?")
        self.assertIn("후보가 여러 명", r["content"])
        self.assertIn("김영훈", r["content"])
        self.assertFalse(r["sources"])

    def test_empty_retrieval_does_not_send_scan_ids_or_logs_to_model(self):
        retrieval = {"stages": [{"source": "mantis_page_scan", "status": "ok", "reason": "private diagnostic",
                                "meta": {"scanned_count": 40000, "scanned_ids": [1, 2], "matched_ids": [],
                                         "matched_count": 0, "search_complete": False}}]}
        context = build_model_retrieval_context(retrieval, 10)
        self.assertNotIn("scanned_count", context["stages"][0]["meta"])
        self.assertNotIn("scanned_ids", context["stages"][0]["meta"])
        self.assertNotIn("reason", context["stages"][0])

    def test_empty_retrieval_returns_limitation_instead_of_generation(self):
        q = "NH 차트 문제"
        context = chat.ChatPipelineContext(q, {"id": "test"}, "key", [], q, {"mantis": True}, [], extract_query_constraints(q), 10)
        request = ChatRequest(modelId="test", messages=[ChatMessage(role="user", content=q)])
        with patch.object(chat, "should_retrieve_chat_evidence", return_value=True), patch.object(chat, "retrieval_limit", return_value=(10, None)), patch.object(chat, "run_retrieval_pipeline", return_value={"stages": []}), patch.object(chat, "deterministic_retrieval_response", return_value=None):
            response = chat.retrieve_chat_evidence(request, context).response
        self.assertEqual(response["raw"]["mode"], "empty_evidence")
        self.assertIn("0건이거나 모두 완료됐다는 뜻은 아닙니다", response["content"])

    def test_actual_context_restores_customer_team_platform_and_replaces_customer(self):
        q = QUESTIONS[30]
        previous = self.report("NH 모바일 쪽 아직 남은 거 있어?", extract_query_constraints("우리 팀 NH MTS 미종료 이슈"))
        plan = deepcopy(previous["retrieval"]["query_plan"])
        req = ChatRequest(modelId="test", contextAuditId="a"*32, messages=[ChatMessage(role="user", content="끝난 건 빼고."), ChatMessage(role="assistant", content=previous["content"]), ChatMessage(role="user", content=q)])
        with patch.object(chat, "get_private_model", return_value={"id": "test", "status": "active"}), patch.object(chat, "get_model_api_key", return_value="test"), patch.object(chat, "load_audit_query_plan", return_value=plan), patch.object(chat, "rewrite_follow_up_query_with_model", side_effect=AssertionError("unnecessary model rewrite")):
            context = chat.prepare_chat_pipeline(req, q, {"uid": "user", "displayName": "석지원"})
        self.assertEqual(context.query_constraints["customer_key"], "kb")
        self.assertEqual(context.query_constraints["platform_key"], "mts")
        self.assertTrue(context.query_constraints["mantis_team_members"])
