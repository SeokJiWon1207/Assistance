"""실제 페이지의 20개 질문에서 발견한 실패를 재현한다."""

import unittest
from datetime import date, timedelta
from unittest.mock import patch

from backend.answers import format_deterministic_issue_answer, should_return_deterministic_issue_list
from backend.evaluation.verification import build_verification_report, ungrounded_constraint_labels
from backend.intent.constraints import extract_query_constraints, merge_query_plan_constraints
from backend.intent.context import build_contextual_retrieval_text
from backend.notion.search import extract_notion_constraints, build_notion_terms
from backend.query_planning import build_query_plan, infer_operation
from backend.reports import sla
from backend.schemas import ChatMessage
from backend.sources.mantis import normalize_issue, issue_matches_constraints
from backend.sources.mantis_collection import IssueBatch


class UiAccuracyRegressions(unittest.TestCase):
    def test_customer_exclusion_does_not_reintroduce_short_alias(self):
        text = "우리 팀 미종료 이슈 중 NH증권을 제외하면 몇 건이야?"
        c = extract_query_constraints(text)
        self.assertIsNone(c["customer_key"])
        plan = build_query_plan(text, {"source": "mantis"}, c, {
            "filter_clauses": [{"field": "customer", "operator": "equals", "value": "NH"}],
        })
        c = merge_query_plan_constraints(c, plan)
        self.assertIsNone(c["customer_key"])
        self.assertFalse(any(x["field"] == "customer" and x["operator"] == "equals" for x in c["filter_clauses"]))

    def test_grouped_aggregation_is_count_without_count_word(self):
        for axis in ("상태", "고객사", "담당자", "플랫폼"):
            text = f"우리 팀 미종료 이슈를 {axis}별로 집계해줘"
            plan = build_query_plan(text, {"source": "mantis"}, extract_query_constraints(text), {"operation": "search"})
            self.assertEqual(plan["operation"], "count")

    def test_developed_is_not_closed(self):
        c = extract_query_constraints("우리 팀에서 개발완료 상태인 이슈는 몇 건이야?")
        self.assertEqual(c["statuses"], ["dev_done"])
        raw = {"status": {"name": "developed"}, "handler": {"real_name": "권한별"}}
        self.assertTrue(issue_matches_constraints(raw, c))
        self.assertEqual(set(extract_query_constraints("개발완료 또는 종료된 이슈")["statuses"]), {"dev_done", "closed"})
        plan = build_query_plan("개발완료 상태 이슈 몇 건이야?", {"source": "mantis"}, c, {
            "filter_clauses": [{"field": "status", "operator": "equals", "value": "resolved"}],
        })
        self.assertEqual(plan["filter_clauses"], [{"field": "status", "operator": "in", "value": ["dev_done"]}])

    def test_followup_stops_at_independent_query(self):
        messages = [ChatMessage(role="user", content=q) for q in (
            "NH증권 미종료 이슈 보여줘", "그중 MTS만 보여줘",
            "우리 팀에서 오늘 등록된 미종료 이슈 모두 보여줘", "그럼 어제 등록된 건은 몇 건이야?",
        )]
        text, _ = build_contextual_retrieval_text(messages)
        c = extract_query_constraints(text)
        self.assertIsNone(c["customer_key"])
        self.assertIsNone(c["platform_key"])
        self.assertTrue(c["mantis_team_members"])
        self.assertEqual(c["created_at"]["from"], (date.today() - timedelta(days=1)).isoformat())

    def test_notion_owner_before_role_does_not_become_document(self):
        with patch("backend.notion.search.load_known_people", return_value=(["우동헌"], [])):
            c = extract_notion_constraints("노션 DB03_작업에서 우동헌 담당 문서 모두 보여줘")
        self.assertEqual(c["owner"], "우동헌")

    def test_notion_followup_keeps_database_owner_and_native_status(self):
        texts = ["노션 DB03_작업에서 우동헌 담당 문서 모두 보여줘", "그중 진행 중인 문서만 보여줘"]
        with patch("backend.notion.search.load_known_people", return_value=(["우동헌"], [])):
            text, _ = build_contextual_retrieval_text([ChatMessage(role="user", content=q) for q in texts])
            native = extract_notion_constraints(text)
            self.assertEqual(native["database_label"], "DB03_작업")
            self.assertEqual(native["owner"], "우동헌")
            c = extract_query_constraints(text)
            plan = build_query_plan(text, {"source": "notion"}, c)
            self.assertEqual(plan["filters"]["statuses"], ["진행중"])
            c = merge_query_plan_constraints(c, plan)
            self.assertEqual(c["statuses"], ["진행중"])
            self.assertEqual(ungrounded_constraint_labels(text, c, "notion"), [])
            self.assertNotIn("문서만", build_notion_terms(text, native))

    def test_resolution_custom_fields_survive_normalization_and_answer(self):
        text = "이슈 #31562의 사유 및 원인, 조치 및 개선 내용을 알려줘"
        self.assertEqual(infer_operation(text), "get")
        raw = {"id": 31562, "summary": "설정 복원", "custom_fields": [
            {"field": {"name": "사유 및 원인"}, "value": "기존 플래그의 반대 값을 세팅하지 않음"},
            {"field": {"name": "조치 및 개선"}, "value": "반대 값을 세팅하도록 수정"},
        ]}
        detail = normalize_issue(raw)
        row = {**detail, "detail": detail}
        c = extract_query_constraints(text)
        self.assertTrue(should_return_deterministic_issue_list(text, c))
        answer = format_deterministic_issue_answer(text, c, {"stages": [{"status": "ok", "issues_list": [row]}]}, 5)
        self.assertIn("기존 플래그의 반대 값을 세팅하지 않음", answer)
        self.assertIn("반대 값을 세팅하도록 수정", answer)

    def test_detail_request_in_question_adds_issue_summaries_without_style_setting(self):
        rows = [
            {"id": 31001 + i, "project": "고객사", "summary": f"제목{i}", "status": "confirmed",
             "handler": "담당자", "created_at": "2026-10-01", "description": f"본문 요약{i}"}
            for i in range(2)
        ]
        retrieval = {"stages": [{"status": "ok", "issues_list": rows}]}
        for text, expected in (("최근 이슈 자세히 보여줘", True), ("최근 이슈 보여줘", False)):
            with self.subTest(text=text):
                answer = format_deterministic_issue_answer(text, extract_query_constraints(text), retrieval, 5)
                self.assertEqual("   - 요약: 본문 요약0" in answer, expected)

    def test_aging_uses_complete_data_and_requested_limit(self):
        rows = [{"id": 30000 + i, "summary": str(i), "created_at": (date.today()-timedelta(days=500-i)).isoformat(),
                 "status": {"name": "confirmed"}, "handler": {"real_name": "권한별"}} for i in range(44)]
        rows.append({"id": 40000, "status": {"name": "resolved"}, "created_at": "2020-01-01"})
        with patch.object(sla, "read_complete_store", return_value=IssueBatch(rows, {"search_complete": True})), \
             patch.object(sla, "fetch_mantis_recent_issues", side_effect=AssertionError("일부 조회 금지")):
            result = sla.build_sla_report("우리 팀에서 등록 후 30일 이상 지난 미종료 이슈를 오래된 순으로 5건 보여줘")
        self.assertIn("**44건**", result["content"])
        self.assertEqual([r["id"] for r in result["retrieval"]["stages"][0]["issues_list"]], list(range(30000, 30005)))
        report = build_verification_report(
            "우리 팀에서 등록 후 30일 이상 지난 미종료 이슈를 오래된 순으로 5건 보여줘",
            result["content"], result["sources"], result["retrieval"], "deterministic_mantis_sla",
        )
        self.assertFalse([check for check in report["checks"] if check["status"] == "fail"], report)

    def test_aging_does_not_claim_global_ranking_for_incomplete_data(self):
        with patch.object(sla, "read_complete_store", return_value=None), \
             patch.object(sla, "fetch_mantis_recent_issues", return_value=IssueBatch([], {"search_complete": False})):
            result = sla.build_sla_report("30일 이상 오래된 미종료 이슈 5건 보여줘")
        self.assertIn("확정할 수 없습니다", result["content"])

    def test_timestamp_and_population_are_not_issue_ids(self):
        report = build_verification_report("미종료 몇 건이야?", "저장본 31183건 · 2026-09-30T01:59:24.652325+00:00\n총 83건입니다.", [], None, "test")
        self.assertNotIn("근거 없는 ID", str(report))

    def test_completed_missing_id_lookup_is_not_fabricated_evidence(self):
        report = build_verification_report(
            "멘티스 이슈 #999999의 현재 상태 알려줘", "#999999 이슈를 찾지 못했어요.", [],
            {"stages": [{"source": "mantis_id_lookup", "status": "not_found", "meta": {
                "search_complete": True, "missing_ids": [999999],
            }}]}, "deterministic_issue_list",
        )
        checks = {check["id"]: check for check in report["checks"]}
        self.assertEqual(checks["evidence"]["status"], "pass")
        self.assertEqual(checks["issue_ids"]["status"], "pass")

    def test_empty_notion_search_with_wrong_owner_cannot_pass(self):
        q = "노션 DB03_작업에서 우동헌 담당 문서 모두 보여줘"
        result = {"query": q, "stages": [{"source": "notion", "status": "ok", "meta": {
            "search_complete": True, "constraints": {"owner": "문서"},
        }}]}
        report = build_verification_report(q, "찾지 못했어요", [], result, "test")
        self.assertIn("notion_owner_intent", str(report))
        self.assertIn("실행 담당자 문서", str(report))


if __name__ == "__main__":
    unittest.main()
