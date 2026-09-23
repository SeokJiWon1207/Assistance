"""실제 템플릿 출력과 템플릿 밖 자연어 질의의 경계 계약."""

import unittest
from datetime import date
from unittest.mock import patch

import requests

from backend.answers import format_deterministic_issue_answer, sanitize_customer_notice_answer, should_return_deterministic_issue_list
from backend.evaluation.verification import build_verification_report
from backend.intent.source_router import rule_based_search_intent
from backend.query_planning import build_query_plan
from backend.reports import incident_triage, sla
from backend.retrieval import build_model_retrieval_context, collect_retrieved_customer_groups, run_retrieval_pipeline
from backend.sources import mantis
from backend.understanding import build_keyword_terms, extract_query_constraints


class TemplateAnswerRegressionTests(unittest.TestCase):
    def _retrieval(self, size=320, displayed=None):
        rows = [
            {"id": index, "summary": f"이슈 {index}", "project": "고객A" if index > 100 else "고객B",
             "status": "assigned", "handler": "담당", "created_at": "2026-09-18"}
            for index in range(size, 0, -1)
        ]
        return {"stages": [{"source": "mantis_page_scan", "status": "ok", "issues_list": rows[:displayed],
                             "meta": {"found_count": size, "matched_issue_ids": [r["id"] for r in rows],
                                      "matched_customer_groups": {r["id"]: r["project"] for r in rows}}}]}

    def test_daily_summary_counts_all_matches_before_display_limit(self):
        question = "오늘 등록된 미종료 이슈를 고객사별로 요약해줘"
        for displayed in (None, 12):
            with self.subTest(displayed=displayed):
                answer = format_deterministic_issue_answer(question, extract_query_constraints(question),
                                                           self._retrieval(displayed=displayed), 12)
                self.assertIn("총 320건을 2개 고객사", answer)
                self.assertIn("| 고객A | 220건 |", answer)
                self.assertIn("| 고객B | 100건 |", answer)
                self.assertNotIn("총 12건", answer)
                self.assertIn("\n\n**고객A · 220건**\n", answer)

    def test_daily_summary_does_not_invent_counts_for_missing_group_metadata(self):
        retrieval = self._retrieval(displayed=12)
        del retrieval["stages"][0]["meta"]["matched_customer_groups"]
        answer = format_deterministic_issue_answer("고객사별로 요약해줘", {}, retrieval, 12)
        self.assertIn("조회된 총 320건 중 12건의 고객사별 건수", answer)
        self.assertNotIn("총 12건을", answer)

    def test_group_headers_are_separated_from_numbered_items(self):
        answer = format_deterministic_issue_answer("고객사별로 요약해줘", {}, self._retrieval(), 12)
        self.assertIn("\n\n**고객B · 100건**\n", answer)

    def test_customer_counts_merge_overlapping_stages_and_live_removals(self):
        retrieval = self._retrieval(size=3)
        retrieval["stages"].append({"source": "mantis_page_scan", "status": "ok", "issues_list": [],
                                    "meta": {"scanned_issue_ids": [2, 3], "matched_issue_ids": [3, 4],
                                             "matched_customer_groups": {3: "고객C", 4: "고객D"}}})
        self.assertEqual(collect_retrieved_customer_groups(retrieval), {1: "고객B", 3: "고객C", 4: "고객D"})

    def test_live_revalidation_updates_full_group_counts(self):
        stage = self._retrieval(size=3)["stages"][0]
        stage["source"] = "mantis_snapshot"
        live = {
            1: {"id": 1, "project": {"name": "고객B"}, "status": {"name": "closed"}},
            2: {"id": 2, "project": {"name": "고객C"}, "status": {"name": "assigned"}},
            3: {"id": 3, "project": {"name": "고객B"}, "status": {"name": "assigned"}},
        }
        with patch.object(mantis, "fetch_mantis_issue_by_id", side_effect=live.get):
            refreshed = mantis.revalidate_mantis_snapshot_stage(stage, {"statuses": ["open"]})
        self.assertEqual(collect_retrieved_customer_groups({"stages": [refreshed]}), {2: "고객C", 3: "고객B"})

    def test_model_receives_full_counts_without_per_issue_group_map(self):
        context = build_model_retrieval_context(self._retrieval(displayed=12), 5)
        self.assertEqual(context["customer_counts"], {"고객A": 220, "고객B": 100})
        self.assertTrue(context["customer_counts_complete"])
        self.assertEqual(len(context["issues"]), 5)
        self.assertNotIn("matched_customer_groups", context["stages"][0]["meta"])

    def test_snapshot_and_page_scan_keep_groups_beyond_displayed_rows(self):
        snapshot = {"available": True, "scanned_count": 320, "matched_count": 320, "issues": [],
                    "matched_ids": set(range(1, 321)), "matched_groups": {i: "고객A" for i in range(1, 321)}}
        with patch.object(mantis, "scan_mantis_documents", return_value=snapshot) as scan:
            stage = mantis.execute_mantis_snapshot_search(12, {})
        self.assertEqual(len(stage["meta"]["matched_customer_groups"]), 320)
        self.assertEqual(scan.call_args.kwargs["group_key"]({"project": "고객B"}), "고객B")
        raw = [{"id": i, "project": {"name": "고객A"}, "summary": "차트"} for i in range(1, 21)]
        mantis.clear_mantis_recent_cache()
        self.addCleanup(mantis.clear_mantis_recent_cache)
        with patch.object(mantis, "fetch_mantis_issues", return_value=raw), patch.object(
            mantis, "get_mantis_settings", return_value={"url": "https://template.test", "token": "test"},
        ):
            stage = mantis.execute_mantis_page_scan("차트", 2, {})
        self.assertEqual(len(stage["issues_list"]), 2)
        self.assertEqual(len(stage["meta"]["matched_customer_groups"]), 20)

    def test_notice_keeps_business_identifiers_and_removes_only_issue_references(self):
        question = "#31411 이슈를 바탕으로 내부 식별자를 제외하고 고객 안내 메일을 작성해줘"
        content = ("이슈 ID: 31411, 이슈 번호 31412, Mantis #31413, #31414, ID: 31411\n"
                   "화면번호 9580, 주문번호 1234567, 버전 번호 20260917, 화면 #9580, 주문 ID: 1234567")
        sanitized = sanitize_customer_notice_answer(question, content)
        for value in ("31411", "31412", "31413", "31414"):
            self.assertNotIn(value, sanitized)
        for value in ("화면번호 9580", "주문번호 1234567", "버전 번호 20260917", "화면 #9580", "주문 ID: 1234567"):
            self.assertIn(value, sanitized)
        self.assertEqual(sanitize_customer_notice_answer("인수인계해줘", content), content)

    def test_explicit_status_is_retained_but_notice_instructions_are_not_statuses(self):
        for phrase, status in (("확인 상태인", "confirmed"), ("해결 상태인", "resolved"), ("검토 상태인", "review")):
            q = f"{phrase} 이슈 보여줘"
            c = extract_query_constraints(q)
            self.assertEqual(c["statuses"], [status])
            self.assertEqual(build_keyword_terms(q, c), [])
        self.assertEqual(extract_query_constraints("현재 확인된 사실과 해결 내용을 안내해줘")["statuses"], [])

    def test_ordinary_search_keeps_keywords_used_as_triage_instructions(self):
        for word in ("긴급", "높음", "일반", "영향", "범위", "순서", "전달"):
            q = f"{word} 이슈 보여줘"
            self.assertIn(word, build_keyword_terms(q, extract_query_constraints(q)))
        q = "고객사별 이슈 추이를 요약해줘"
        self.assertFalse(should_return_deterministic_issue_list(q, extract_query_constraints(q)))
        self.assertEqual(rule_based_search_intent(q, extract_query_constraints(q))["source"], "mantis")

    def test_triage_is_specific_to_symptom_and_preserves_all_priorities(self):
        for urgency in ("긴급", "높음", "일반"):
            for symptom in ("차트 미표시", "주문 접수 누락", "로그인 실패", "파일 다운로드 오류"):
                q = f"{urgency} 우선순위로 '{symptom}' 이슈의 영향 범위, 초동 확인 순서와 담당자 전달 내용을 정리해줘"
                with self.subTest(urgency=urgency, symptom=symptom), patch.object(
                    incident_triage, "execute_chroma_search", return_value={"status": "ok", "documents": []},
                ) as search:
                    report = incident_triage.build_incident_triage_report(q)
                self.assertEqual(search.call_args.args[0], symptom)
                self.assertIn(f"우선순위: {urgency}", report["content"])
                if "주문" not in symptom:
                    self.assertNotIn("주문", report["content"])
                if "로그인" not in symptom:
                    self.assertNotIn("로그인", report["content"])
                if "차트" in symptom:
                    self.assertIn("렌더링", report["content"])

    def test_triage_search_failure_is_not_reported_as_no_matches(self):
        q = "높음 우선순위로 '차트 미표시' 영향 범위와 초동 확인 순서 정리해줘"
        with patch.object(incident_triage, "execute_chroma_search", return_value={"status": "error", "reason": "연결 실패"}):
            report = incident_triage.build_incident_triage_report(q)
        self.assertIn("연결 실패", report["content"])
        self.assertNotIn("찾지 못했습니다", report["content"])
        self.assertEqual(report["retrieval"]["stages"][0]["status"], "error")
        with patch.object(incident_triage, "execute_chroma_search") as search:
            report = incident_triage.build_incident_triage_report(q, search_similar=False)
        search.assert_not_called()
        self.assertIn("초동 확인 순서", report["content"])

    def test_mantis_dependency_failure_is_not_reported_as_zero_matches(self):
        question = "이번주 이슈 보여줘"
        constraints = extract_query_constraints(question)
        query_plan = build_query_plan(question, {"source": "mantis"}, constraints)
        retrieval = {
            "query": question,
            "constraints": constraints,
            "query_plan": query_plan,
            "stages": [
                {
                    "source": "mantis_snapshot",
                    "status": "ok",
                    "issues_list": [],
                    "meta": {"found_count": 0, "displayed_count": 0},
                },
                {
                    "source": "mantis_page_scan",
                    "status": "error",
                    "error_kind": "dependency",
                    "reason": "Mantis API failed: 500",
                    "issues_list": [],
                    "meta": {"displayed_count": 0},
                },
            ],
        }
        answer = format_deterministic_issue_answer(question, constraints, retrieval, 5)
        self.assertIn("Mantis 연결 오류로 실시간 조회를 완료하지 못했어요", answer)
        self.assertIn("0건이라고 확정할 수 없습니다", answer)
        self.assertNotIn("조건에 맞는 이슈를 찾지 못했어요", answer)
        report = build_verification_report(question, answer, [], retrieval, "deterministic_issue_list")
        evidence = next(check for check in report["checks"] if check["id"] == "evidence")
        self.assertEqual(evidence["status"], "warning")
        self.assertIn("원문 확인 미완료", evidence["detail"])
        self.assertEqual(report["status"], "warning")

    def test_completed_empty_search_is_valid_evidence(self):
        retrieval = {
            "stages": [{
                "source": "mantis_page_scan",
                "status": "ok",
                "issues_list": [],
                "meta": {"displayed_count": 0},
            }],
        }
        answer = "조건에 맞는 이슈가 없습니다."
        report = build_verification_report("이번주 이슈 보여줘", answer, [], retrieval, "deterministic_issue_list")
        evidence = next(check for check in report["checks"] if check["id"] == "evidence")
        self.assertEqual(evidence["status"], "pass")
        self.assertIn("검색 완료", evidence["detail"])

    def test_mantis_filter_connection_failure_becomes_dependency_stage(self):
        question = "이번주 이슈 보여줘"
        constraints = extract_query_constraints(question)
        plan = build_query_plan(question, {"source": "mantis"}, constraints)
        snapshot = {
            "source": "mantis_snapshot",
            "status": "ok",
            "issues_list": [],
            "meta": {"snapshot_as_of": "2026-08-19", "found_count": 0},
        }
        with patch("backend.retrieval.execute_mantis_snapshot_search", return_value=snapshot), patch(
            "backend.retrieval.execute_mantis_filter_search",
            side_effect=requests.RequestException("invalid response"),
        ), patch(
            "backend.retrieval.execute_mantis_page_scan",
            return_value={"source": "mantis_page_scan", "status": "ok", "issues_list": []},
        ):
            result = run_retrieval_pipeline(
                question,
                5,
                {"mantis": True, "notion": False, "vectorDb": False},
                intent_plan=plan,
            )
        filter_stage = next(stage for stage in result["stages"] if stage["source"] == "mantis_filter")
        self.assertEqual(filter_stage["status"], "error")
        self.assertEqual(filter_stage["error_kind"], "dependency")

    def test_aging_intersects_registration_month_and_age(self):
        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 9, 18)
        raw = [
            {"id": i, "summary": title, "project": {"name": "고객A"}, "status": {"name": "assigned"},
             "created_at": created, "updated_at": created}
            for i, title, created in ((101, "7월 제외", "2026-07-01"), (102, "8월 포함", "2026-08-01"),
                                      (103, "8월 경과일 미달", "2026-08-25"), (104, "9월 제외", "2026-09-01"))
        ]
        for q, expected in (("2026년 8월에 등록된 이슈 중 30일 넘게 미종료", [102]),
                            ("이번 달 등록된 이슈 중 30일 넘게 미종료", [])):
            with self.subTest(q=q), patch.object(sla, "date", FixedDate), patch(
                "backend.query_planning.date", FixedDate,
            ), patch.object(sla, "fetch_mantis_recent_issues", return_value=raw):
                report = sla.build_sla_report(q)
            rows = report["retrieval"]["stages"][0].get("issues_list", [])
            self.assertEqual([row["id"] for row in rows], expected)
            self.assertTrue(report["retrieval"]["constraints"]["created_at"]["from"])
            self.assertNotIn("적체 분석에 적용하지 않았습니다", report["content"])

    def test_effort_is_not_lost_when_no_issue_has_closed(self):
        raw = [{"id": 221, "summary": "진행 중", "status": {"name": "assigned"}, "project": {"name": "고객A"},
                "created_at": "2026-08-01", "updated_at": "2026-08-05",
                "custom_fields": [{"field": {"name": "3PC공수"}, "value": "김담당(2.5)"}]}]
        for q in ("2026년 8월 고객사별 공수와 평균 처리시간을 표로 보여줘", "2026년 8월 공수와 평균 처리시간을 표로 보여줘"):
            with self.subTest(q=q), patch.object(sla, "fetch_mantis_recent_issues", return_value=raw):
                report = sla.build_sla_report(q)
            self.assertIn("2.50", report["content"])
            self.assertIn("계산 불가", report["content"])
            self.assertIn("|", report["content"])
