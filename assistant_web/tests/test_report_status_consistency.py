"""운영 보고서와 일반 조회가 해결·미종료·미확인 상태를 동일하게 집계한다."""

import unittest
from datetime import date
from unittest.mock import patch

from backend.reports import customer, monthly_customer, weekly_customer
from backend.sources.mantis_collection import IssueBatch


class ReportDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 8, 10)


class ReportStatusConsistencyTests(unittest.TestCase):
    def test_resolved_and_unknown_never_enter_open_backlog(self):
        rows = IssueBatch([
            {"id": index, "status": status, "summary": status, "project": "신한투자증권",
             "handler": "석지원", "created_at": "2026-07-01", "updated_at": "2026-07-04",
             "effort_fields": {}}
            for index, status in enumerate(["resolved", "종료", "developed", "미확인"], 1)
        ], {"status": "snapshot", "fetchedAt": "2026-08-10T00:00:00Z", "search_complete": False})
        for module, build, question, end in (
            (customer, customer.build_customer_profile_report, "신한 고객사 프로필", "2026-07-31"),
            (monthly_customer, monthly_customer.build_monthly_customer_report, "신한 월간 고객사 리포트", "2026-07-31"),
            (weekly_customer, weekly_customer.build_weekly_report, "신한 주간 보고서", "2026-07-07"),
        ):
            constraints = {"customer_exact": "신한투자증권", "created_at": {"from": "2026-07-01", "to": end}}
            with (
                self.subTest(report=module.__name__),
                patch.object(module, "date", ReportDate),
                patch.object(module, "fetch_mantis_recent_issues", return_value=rows),
                patch.object(module, "normalize_issue", side_effect=lambda row: row),
                patch.object(module, "issue_matches_constraints", return_value=True),
                patch.object(module, "extract_query_constraints", return_value=constraints),
                patch.object(module, "build_sources_from_issue_rows", return_value=[]),
            ):
                result = build(question)
            meta = result["retrieval"]["stages"][0]["meta"]
            self.assertEqual(meta["open_count"], 1)
            self.assertEqual(meta["closed_count"], 2)
            self.assertEqual(meta["unknown_status_count"], 1)
            self.assertEqual(meta["aging_count"], 1)
            self.assertEqual(meta["fetchedAt"], rows.meta["fetchedAt"])
            self.assertFalse(meta["search_complete"])
            self.assertIn("상태 미확인: **1건**", result["content"])
            self.assertIn("평균 처리기간: **3.0일**", result["content"])
