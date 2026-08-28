"""운영 대시보드 통합 리포트 테스트."""

import unittest
from datetime import date
from unittest.mock import patch

from backend.reports import dashboard


class OperationsDashboardTests(unittest.TestCase):
    def setUp(self):
        self.issues = [
            {
                "id": 101,
                "summary": "오래된 주문 오류",
                "project": "신한",
                "screen_number": "7600",
                "status": "confirmed",
                "priority": "high",
                "handler": "석지원",
                "created_at": "2026-05-01",
                "updated_at": "2026-08-01",
                "date_fields": {"완료예정일": "2026-08-10"},
            },
            {
                "id": 102,
                "summary": "로그인 오류",
                "project": "신한",
                "screen_number": "7600",
                "status": "assigned",
                "priority": "normal",
                "handler": "담당자 미지정",
                "created_at": "2026-08-20",
                "updated_at": "2026-08-20",
                "date_fields": {},
            },
            {
                "id": 103,
                "summary": "조회 오류",
                "project": "신한",
                "screen_number": "7600",
                "status": "closed",
                "priority": "normal",
                "handler": "석지원",
                "created_at": "2026-08-18",
                "updated_at": "2026-08-22",
                "date_fields": {},
            },
            {
                "id": 90,
                "summary": "직전 기간 이슈",
                "project": "유진",
                "screen_number": "5000",
                "status": "closed",
                "priority": "normal",
                "handler": "우동헌",
                "created_at": "2026-07-20",
                "updated_at": "2026-07-22",
                "date_fields": {},
            },
        ]

    def _build(self, user, users=None):
        with patch.object(
            dashboard,
            "_load_issues",
            return_value=(self.issues, {"status": "live", "reason": ""}),
        ):
            return dashboard.build_operations_dashboard(
                user,
                users=users or [],
                days=30,
                today=date(2026, 8, 27),
            )

    def test_member_dashboard_uses_personal_metrics_and_team_health(self):
        result = self._build(
            {"uid": "u1", "displayName": "석지원", "role": "member", "mantisUsername": "석지원"}
        )

        self.assertEqual(result["meta"]["scope"], "personal")
        self.assertEqual(result["summary"]["open"], 1)
        self.assertEqual(result["summary"]["overdue"], 1)
        self.assertEqual(result["summary"]["highRisk"], 1)
        self.assertEqual(result["priorities"][0]["id"], 101)
        self.assertIn("최근 30일", result["decisionSummary"]["headline"])
        self.assertNotIn("지난주", result["decisionSummary"]["headline"])
        self.assertEqual(result["customerHealth"][0]["customer"], "신한")
        self.assertEqual(result["customerHealth"][0]["level"], "high")
        self.assertEqual(result["members"], [])

    def test_manager_dashboard_includes_team_workload_without_account_details(self):
        users = [
            {
                "uid": "u1",
                "email": "member@example.com",
                "displayName": "석지원",
                "role": "member",
                "mantisUsername": "석지원",
                "approved": True,
            },
            {
                "uid": "u2",
                "email": "member2@example.com",
                "displayName": "우동헌",
                "role": "member",
                "mantisUsername": "우동헌",
                "approved": True,
            },
        ]
        result = self._build(
            {"uid": "manager", "displayName": "팀장", "role": "manager", "mantisUsername": "팀장"},
            users,
        )

        self.assertEqual(result["meta"]["scope"], "team")
        self.assertEqual(result["summary"]["open"], 2)
        self.assertEqual(len(result["members"]), 2)
        self.assertNotIn("email", result["members"][0])
        self.assertNotIn("uid", result["members"][0])

    def test_period_must_be_one_of_the_supported_options(self):
        with self.assertRaisesRegex(ValueError, "7일"):
            dashboard.build_operations_dashboard(
                {"role": "member"}, days=15, today=date(2026, 8, 27)
            )
