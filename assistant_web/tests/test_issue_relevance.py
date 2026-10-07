import unittest
from datetime import date
from unittest.mock import patch

from backend.reports.issue_relevance import rank_similar, positive_crash, crash_issue, incident_kind, recurrence_candidates
from backend.reports.work_queries import build_work_query_report, verify_work_report, work_operation
from backend.intent.constraints import extract_query_constraints
from backend.intent.confirmed_context import merge_confirmed_constraints
from backend.sources.mantis_collection import IssueBatch
from backend.sources.mantis import normalize_issue
from backend.services import chat_service as chat
from backend.schemas import ChatRequest, ChatMessage


def row(i, title, created="2020-01-01", description="", customer="120NH(NH증권)"):
    return {"id": i, "summary": title, "created_at": created, "description": description,
            "status": {"name": "assigned"}, "project": {"name": customer}, "handler": {"real_name": "이영희"}}


class IssueRelevanceTests(unittest.TestCase):
    def report(self, q, rows, c=None):
        with patch("backend.reports.work_queries.read_complete_store", return_value=IssueBatch(rows,
                {"snapshot_complete": True, "search_complete": True, "fetchedAt": "2026-10-02"})):
            return build_work_query_report(q, c or extract_query_constraints(q), today=date(2026, 10, 2))

    def test_similarity_preserves_numbers_and_ignores_generic_chart_language(self):
        seed = normalize_issue(row(21371, "[0000] MTS에서 1분대 이하의 차트 구현 가능 문의", "2020-10-22"))
        candidates = [normalize_issue(r) for r in [
            row(3, "MTS 차트 배경색 변경"), row(4, "5분 차트 기능 문의"),
            row(5, "차트 2*1분할 후 동기화 오류"), row(6, "주문 1분마다 조회 변경"),
            row(7, "9시1분에 검색된 신호"),
            row(2, "1분 차트 조회 요청"), row(30000, "1분 차트 조회 요청", "2026-01-01")]]
        ranked = rank_similar(seed, candidates, historical=True)
        self.assertEqual([r["id"] for r in ranked], [2])
        self.assertEqual(ranked[0]["similarity_terms"], ["1분"])

    def test_issue_number_does_not_intercept_unrelated_operations(self):
        self.assertEqual(work_operation("0021371", {"issue_ids": [21371]}), "detail")
        self.assertIsNone(work_operation("21371 소요 공수 합계", {"issue_ids": [21371]}))

    def test_document_requests_about_an_issue_are_written_not_looked_up(self):
        # 템플릿 질문 속 '담당자'·'원인'은 문서 항목 이름이다. 담당자·원인 조회로 답하면
        # 인수인계·사후 분석·고객 안내문이 만들어지지 않는다.
        documents = (
            "#31572 이슈를 다음 담당자에게 인수인계할 수 있도록 현재 상태, 확인한 내용, 시도한 조치, 막힌 점, 다음 행동과 주의사항을 정리해줘",
            "#31399 이슈를 바탕으로 발생 영향, 타임라인, 직접 원인과 근본 원인, 대응 과정, 잘된 점, 개선점, 재발 방지 과제를 포함해 사후 분석서 형식으로 작성해줘",
            "#31399 이슈를 바탕으로 내부 식별자와 불확실한 원인 추정을 제외하고, 현재 확인된 사실, 고객 영향, 진행 상황과 다음 안내 시점을 담아 정중한 말투의 점검 공지 형식으로 작성해줘",
            "#31399 이슈를 바탕으로 발생 영향, 타임라인, 직접 원인과 근본 원인을 포함해 5줄 요약 형식으로 작성해줘",
            # 원인·해결 비교는 유사 해결 리포트가 사례별 조치까지 정리한다.
            "#31399와 비슷한 과거 이슈를 찾아서 원인과 해결 내용을 비교해줘",
        )
        for q in documents:
            with self.subTest(q=q):
                self.assertIsNone(work_operation(q, extract_query_constraints(q)))
        # 짧은 조회 질문은 그대로 업무 조회가 답한다.
        for q, operation in (("21371 왜 아직 남아 있어?", "cause"), ("21371 담당자가 누구야?", "owner"),
                             ("21371과 비슷한 과거 이슈", "similar"), ("21371 이거 지금 어디까지 됐어?", "detail")):
            with self.subTest(q=q):
                self.assertEqual(work_operation(q, extract_query_constraints(q)), operation)

    def test_report_does_not_replace_relevance_order_with_recency(self):
        rows = [row(21371, "VWAP 거래량 누락 오류", "2020-10-22"),
                row(2, "VWAP 거래량 누락 오류", "2019-01-01"),
                row(3, "VWAP 거래량 표시 설정 변경", "2020-01-01")]
        r = self.report("21371과 비슷한 과거 이슈", rows)
        self.assertEqual(r["sources"][0]["issue_id"], 2)
        self.assertIn("일치 개념", r["content"])
        v = verify_work_report("21371과 비슷한 과거 이슈", r["content"], r["sources"], r["retrieval"])
        self.assertEqual(v["status"], "warning")
        self.assertIn("semantic_review", [c["id"] for c in v["checks"]])

    def test_no_strong_similarity_is_not_filled_with_latest_issues(self):
        rows = [row(21371, "1분대 이하 차트 문의"), row(2, "MTS 차트 배경색 수정")]
        r = self.report("21371과 비슷한 과거 이슈", rows)
        self.assertFalse(r["sources"])
        self.assertEqual(r["retrieval"]["stages"][0]["meta"]["matched_count"], 0)

    def test_crash_excludes_normal_exit_download_negation_and_other_primary_symptoms(self):
        for text in ("앱 종료 후 설정 저장", "아이폰 다운로드", "iOS 크래시 없음", "비정상 종료가 아닌 설정 초기화"):
            self.assertFalse(positive_crash(text), text)
        for text in ("아이폰에서 앱이 죽는 현상", "iOS 앱 튕김", "아이폰 강제 종료", "iPhone crash"):
            self.assertTrue(positive_crash(text), text)
        self.assertFalse(crash_issue({"summary": "iOS 배경색 오류", "description": "과거 크래시와 관련 없는 문제"}))
        self.assertTrue(crash_issue({"summary": "iOS 비정상 종료 개선 요청"}))

    def test_crash_query_uses_actual_symptom_and_not_plain_exit(self):
        rows = [row(1, "아이폰 앱 종료 후 설정 해제"), row(2, "iOS 비정상 종료"),
                row(3, "iOS 배경색 오류", description="과거 crash 참고"), row(4, "Android 비정상 종료")]
        for q in ("아이폰에서 죽는다는 건 어떻게 됐어?", "아이폰 앱 튕김 건 알려줘", "iOS crash 어떻게 됐어?"):
            r = self.report(q, rows)
            self.assertEqual([s["issue_id"] for s in r["sources"]], [2], q)

    def test_ambiguous_query_asks_for_discriminating_dimension_and_keeps_scope(self):
        rows = [row(i, "MTS 차트 표시 오류") for i in range(1, 8)]
        r = self.report("전에 NH에서 차트 이상하다고 했던 거 있잖아.", rows)
        self.assertIn("모바일(MTS)", r["content"])
        self.assertFalse(r["sources"])
        c = merge_confirmed_constraints(r["retrieval"]["constraints"], extract_query_constraints("MTS야"), "MTS야")
        r2 = self.report("MTS야", rows, c)
        self.assertIn("화면번호", r2["content"])
        self.assertEqual(r2["retrieval"]["constraints"]["customer_key"], "NH")
        self.assertEqual(r2["retrieval"]["constraints"]["platform_key"], "mts")
        self.assertEqual(sum(x["field"] == "keyword" for x in r2["retrieval"]["constraints"]["filter_clauses"]), 1)

    def test_incidents_separate_features_and_do_not_infer_recurrence_from_count_alone(self):
        self.assertEqual(incident_kind({"summary": "VWAP 지표 추가 요청"}), "enhancement")
        self.assertEqual(incident_kind({"summary": "차트 오류 확인요청"}), "defect")
        self.assertEqual(incident_kind({"summary": "차트 버전 문의", "description": "오래된 크래시 사례"}), "unknown")
        rows = [row(1, "[1234] 차트 비정상 종료", "2026-10-01"),
                row(2, "[1234] 차트 크래시", "2026-10-02"),
                row(3, "차트 지표 추가", "2026-10-01"), row(4, "차트 버전 문의", "2026-10-01")]
        r = self.report("차트 쪽 계속 문제 생기는 데가 어디야?", rows)
        self.assertEqual(r["retrieval"]["stages"][0]["meta"]["matched_count"], 2)
        self.assertIn("화면 1234 · 크래시: 2건", r["content"])
        self.assertIn("기능 추가/개선 요청 1건, 미분류 1건", r["content"])
        self.assertIn("이력을 대조", r["content"])
        self.assertFalse(recurrence_candidates([{"id": 1, "project": "NH", "screen_number": "0000", "summary": "크래시"}]*2))

    def test_short_customer_answer_only_inherits_pending_disambiguation(self):
        q = "NH"
        initial = self.report("아이폰에서 죽는다는 건 어떻게 됐어?", [row(1, "iOS 크래시"), row(2, "iOS 크래시", customer="112KBStar(KB증권)")])
        req = ChatRequest(modelId="test", contextAuditId="a"*32, messages=[ChatMessage(role="user", content="아이폰에서 죽는다는 건 어떻게 됐어?"), ChatMessage(role="assistant", content=initial["content"]), ChatMessage(role="user", content=q)])
        with patch.object(chat, "get_private_model", return_value={"id": "test", "status": "active"}), patch.object(chat, "get_model_api_key", return_value="test"), patch.object(chat, "load_audit_query_plan", return_value=initial["retrieval"]["query_plan"]), patch.object(chat, "rewrite_follow_up_query_with_model", side_effect=AssertionError("unnecessary rewrite")):
            c = chat.prepare_chat_pipeline(req, q, {"uid": "user", "displayName": "석지원"}).query_constraints
        self.assertEqual(c["customer_key"], "NH")
        self.assertTrue(c["work_lookup"]["crash"])
