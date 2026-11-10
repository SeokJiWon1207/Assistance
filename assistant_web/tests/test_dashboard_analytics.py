"""차트의 날짜 기준과 동일 고객사 범위를 검증한다."""
import unittest
from datetime import date

from backend.reports import dashboard


class DashboardAnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.issues = [
            {"id": 1, "project": "LS", "handler": "석지원", "screen_number": "1892", "status": "assigned",
             "created_at": "2026-09-01", "updated_at": "2026-11-09",
             "date_fields": {"고객사전달일": "2026-11-03", "완료예정일": "2026-11-01"},
             "effort_fields": {"3PC공수": "석지원(1.5)"}},
            {"id": 2, "project": "LS", "handler": "석지원", "screen_number": "1892", "status": "closed",
             "created_at": "2026-11-08", "updated_at": "2026-11-09"},
            {"id": 3, "project": "NH", "handler": "권한별", "screen_number": "3605", "status": "developed",
             "created_at": "2026-10-20", "date_fields": {"고객사전달일": "2026-11-05"},
             "effort_fields": {"2서버공수": "권한별(0.5)", "4QC공수": "석지원(1.0)"}},
            {"id": 4, "project": "NH", "status": "unrecognized", "created_at": "invalid"},
            {"id": 5, "project": "LS", "handler": "석지원", "status": "resolved",
             "created_at": "2026-09-20", "date_fields": {"고객사전달일": "2026-09-22"}},
        ]

    def build(self, days=30, complete=True):
        builder = getattr(dashboard, "build_dashboard_analytics", None)
        self.assertTrue(callable(builder), "대시보드에 분석 집계를 연결해야 합니다")
        return builder(self.issues, today=date(2026, 11, 10), days=days, complete=complete)

    def test_delivery_uses_explicit_delivery_date_not_last_modified(self):
        data = self.build()
        self.assertEqual(data["delivery"]["count"], 2)
        self.assertEqual(data["delivery"]["previousCount"], 1)
        self.assertEqual(sum(p["count"] for p in data["delivery"]["points"]), 2)
        self.assertEqual(data["delivery"]["dateBasis"], "고객사전달일")
        self.assertEqual(data["effort"]["total"], 3)
        self.assertEqual(sum(p["count"] for p in data["effort"]["points"]), 3)

    def test_current_open_is_independent_of_period_and_customer_scope_is_consistent(self):
        data = self.build()
        week = self.build(days=7)
        self.assertEqual(data["summary"]["open"], week["summary"]["open"])
        self.assertEqual(data["summary"]["open"], 2)
        self.assertEqual(data["summary"]["newIssues"], 2)
        self.assertEqual(week["summary"]["newIssues"], 1)
        ls = data["scopes"]["LS"]
        self.assertEqual(ls["summary"]["open"], 1)
        self.assertEqual(ls["summary"]["newIssues"], 1)
        self.assertEqual(ls["summary"]["overdue"], 1)
        self.assertEqual(ls["delivery"]["count"], 1)
        self.assertEqual(ls["effort"]["total"], 1.5)
        self.assertEqual(sum(p["count"] for p in ls["intake"]), 1)
        self.assertEqual([r["name"] for r in ls["comparisons"]["customer"]], ["LS"])

    def test_current_state_matrix_preserves_unknown_without_inventing_history(self):
        data = self.build()
        self.assertEqual(data["states"]["unknown"], 1)
        self.assertEqual(data["states"]["validation"], 1)
        self.assertEqual(data["states"]["done"], 2)
        self.assertEqual(sum(r["total"] for r in data["cohorts"]), 2)
        self.assertEqual(sum(sum(r["states"].values()) for r in data["cohorts"]), 2)
        self.assertEqual(data["historyAvailable"], False)
        self.assertEqual(data["missingCreated"], 1)

    def test_partial_comparison_does_not_report_a_percentage(self):
        data = self.build(complete=False)
        self.assertIsNone(data["summary"]["changeRate"])
        self.assertTrue(all(row["changeRate"] is None for row in data["comparisons"]["customer"]))
        self.assertTrue(all(scope["summary"]["changeRate"] is None for scope in data["scopes"].values()))

    def test_zero_dates_and_all_zero_series_remain_finite(self):
        self.issues = [{"id": 6, "project": "LS", "status": "new", "created_at": "2026-99-99",
                        "date_fields": {"고객사전달일": "2026-99-99"}}]
        data = self.build()
        self.assertEqual(len(data["intake"]), 30)
        self.assertEqual(sum(p["count"] for p in data["intake"]), 0)
        self.assertEqual(data["delivery"]["count"], 0)
        self.assertIsNone(data["summary"]["changeRate"])

    def test_overdue_uses_the_same_fallback_due_fields_as_issue_lists(self):
        self.issues = [{"id": 1, "project": "LS", "status": "assigned", "created_at": "2026-09-01",
                        "date_fields": {"수정예정일": "2026-11-01"}}]
        data = self.build()
        self.assertEqual(data["summary"]["overdue"], 1)
        self.assertEqual(data["scopes"]["LS"]["summary"]["overdue"], 1)
