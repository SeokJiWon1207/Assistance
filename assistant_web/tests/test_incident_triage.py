"""장애 초동 대응 템플릿 테스트."""

import unittest
from unittest.mock import patch

from backend.reports import incident_triage


class IncidentTriageReportTests(unittest.TestCase):
    def test_unrelated_question_is_not_intercepted(self):
        self.assertIsNone(incident_triage.build_incident_triage_report("미래에셋 MTS 이슈 보여줘"))

    def test_report_keeps_scope_and_returns_actionable_checklist(self):
        question = (
            "긴급 우선순위로 미래에셋증권 MTS에서 발생한 "
            "'로그인 후 주문 화면이 열리지 않음' 이슈의 영향 범위, 초동 확인 순서, "
            "유사 사례와 담당자 전달 내용을 정리해줘"
        )
        search_result = {
            "status": "ok",
            "documents": [{
                "latest_issue": {
                    "id": 101,
                    "summary": "모바일 주문 화면 오류",
                    "project": "다른 고객사",
                    "status": "closed",
                }
            }],
        }
        with (
            patch.object(incident_triage, "execute_chroma_search", return_value=search_result) as search,
            patch.object(
                incident_triage,
                "build_sources_from_issue_rows",
                return_value=[{"issue_id": 101}],
            ),
        ):
            report = incident_triage.build_incident_triage_report(question)

        self.assertEqual(report["raw"]["mode"], "deterministic_incident_triage")
        self.assertIn("미래에셋증권 · MTS", report["content"])
        self.assertIn("영향 범위 확인", report["content"])
        self.assertIn("초동 확인 순서", report["content"])
        self.assertIn("담당자 전달 내용", report["content"])
        self.assertIn("#101 모바일 주문 화면 오류", report["content"])
        passed_constraints = search.call_args.args[2]
        self.assertEqual(passed_constraints["platform_key"], "mts")
        self.assertFalse(any(
            clause.get("field") == "customer"
            for clause in passed_constraints["filter_clauses"]
        ))


if __name__ == "__main__":
    unittest.main()
