"""운영 대시보드 통합 리포트 테스트."""

import unittest
from datetime import date
from unittest.mock import patch

from backend.reports import dashboard


class OperationsDashboardTests(unittest.TestCase):
    def test_partial_source_withholds_comparisons_and_stability(self):
        for issues in ([], self.issues, [self.issues[-1]]):
            with self.subTest(ids=[row['id'] for row in issues]), patch.object(
                dashboard, '_load_issues', return_value=(issues, {'status': 'snapshot', 'search_complete': False})
            ):
                result = dashboard.build_operations_dashboard({'role': 'member'}, today=date(2026, 8, 27))
            self.assertIsNone(result['summary']['changeRate'])
            self.assertEqual(result['customerSpikes'], [])
            self.assertEqual(result['screenSpikes'], [])
            self.assertNotEqual(result['decisionSummary']['riskLevel'], 'stable')
            self.assertNotIn('%', result['decisionSummary']['headline'])
            self.assertIn('확인', result['decisionSummary']['headline'])
            for health in result['customerHealth']:
                self.assertEqual(health['trend'], 'unknown')
                self.assertEqual(health['level'], 'unknown')

    def test_unknown_status_is_neither_closed_nor_open_in_new_cohort(self):
        self.issues += [{**self.issues[2], 'id': 104, 'status': 'unrecognized'}]
        result = self._build({'role': 'member'})
        self.assertEqual(result['summary']['newIssues'], 2)
        self.assertEqual(result['summary']['closedNow'], 1)
        self.assertEqual(result['summary']['newOpen'], 0)
        self.assertEqual(result['summary']['unknownStatus'], 1)

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
            return_value=(self.issues, {"status": "live", "reason": "", "search_complete": True}),
        ):
            return dashboard.build_operations_dashboard(
                user,
                users=users or [],
                days=30,
                today=date(2026, 8, 27),
            )

    def test_member_dashboard_uses_team_metrics_and_workload(self):
        users = [
            {
                "uid": "u1",
                "displayName": "석지원",
                "role": "member",
                "mantisUsername": "석지원",
                "approved": True,
            }
        ]
        result = self._build(
            {"uid": "u1", "displayName": "석지원", "role": "member", "mantisUsername": "석지원"},
            users,
        )

        self.assertEqual(result["meta"]["scope"], "team")
        self.assertEqual(result["summary"]["open"], 1)
        self.assertEqual(result["summary"]["overdue"], 1)
        self.assertEqual(result["summary"]["highRisk"], 1)
        self.assertEqual(result["priorities"][0]["id"], 101)
        self.assertIn("최근 30일", result["decisionSummary"]["headline"])
        self.assertNotIn("지난주", result["decisionSummary"]["headline"])
        self.assertEqual(result["customerHealth"][0]["customer"], "신한")
        self.assertEqual(result["customerHealth"][0]["level"], "high")
        self.assertEqual(len(result["members"]), 13)

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
        self.assertEqual(result["summary"]["open"], 1)
        self.assertEqual(len(result["members"]), 13)
        self.assertNotIn("email", result["members"][0])
        self.assertNotIn("uid", result["members"][0])

    def test_period_must_be_one_of_the_supported_options(self):
        with self.assertRaisesRegex(ValueError, "7일"):
            dashboard.build_operations_dashboard({"role": "member"}, days=15, today=date(2026, 8, 27))

    def test_all_dashboard_sections_exclude_other_teams_and_include_extra_members(self):
        template = self.issues[0]
        self.issues += [
            {**template, "id": 201, "handler": "하영훈", "project": "추가담당", "work_subject": "고객사"},
            {**template, "id": 202, "handler": "이상신", "project": "추가담당"},
        ]
        for index, name in enumerate(["강민성", "cs이슈", "DRFN_NEXT", "석지원외부"]):
            self.issues.append({**template, "id": 300 + index, "handler": name, "project": "외부고객",
                                "screen_number": "9999", "created_at": "2026-08-20", "work_subject": "CS팀"})
        users = [
            {"displayName": "관리자", "mantisUsername": "석지원", "approved": True, "role": "admin"},
            {"displayName": "석지원", "mantisUsername": "석지원", "approved": True, "role": "member"},
            {"displayName": "강민성", "approved": True, "role": "member"},
        ]
        result = self._build({"role": "member", "displayName": "석지원"}, users)
        self.assertEqual(result["summary"]["open"], 3)
        self.assertEqual(result["summary"]["newIssues"], 1)
        self.assertEqual(result["summary"]["previousNewIssues"], 1)
        self.assertEqual(result["meta"]["loadedIssues"], 5)
        self.assertEqual(sum(row["open"] for row in result["members"]), 3)
        members = {row["mantisUsername"]: row for row in result["members"]}
        self.assertEqual(len(members), 13)
        self.assertEqual(members["하영훈"]["open"], 1)
        self.assertEqual(members["이상신"]["open"], 1)
        self.assertEqual(members["김진성"]["open"], 0)
        self.assertEqual(members["석지원"]["role"], "member")
        self.assertNotIn("강민성", members)
        self.assertEqual({row["id"] for row in result["priorities"]}, {101, 201, 202})
        self.assertEqual({row["id"] for row in result["todos"]}, {101, 201, 202})
        self.assertNotIn("외부고객", {row["customer"] for row in result["customerHealth"]})
        self.assertFalse(result["customerSpikes"])
        self.assertFalse(result["screenSpikes"])

    def test_member_aging_only_excludes_overdue_rows_for_stacked_bars(self):
        self.issues.append({**self.issues[1], "id": 104, "handler": "석지원",
                            "created_at": "2026-07-01", "updated_at": "2026-07-01", "date_fields": {}})
        users = [{"displayName": "석지원", "mantisUsername": "석지원", "approved": True, "role": "member"}]
        result = self._build({"role": "member", "displayName": "석지원"}, users)
        member = {row["mantisUsername"]: row for row in result["members"]}["석지원"]
        # #101은 지연이면서 장기라서 aging에는 들어가도 agingOnly에서는 빠진다.
        self.assertEqual((member["open"], member["overdue"], member["aging"], member["agingOnly"]), (2, 1, 2, 1))

class DashboardWholePopulationTests(unittest.TestCase):
    setUp = OperationsDashboardTests.setUp
    _build = OperationsDashboardTests._build
    def test_counts_include_rows_beyond_display_limits(self):
        self.issues = [{**self.issues[0], "id": i + 1, "project": f"고객사{i}"} for i in range(40)]
        result = self._build({"role": "manager", "displayName": "팀장"})
        self.assertEqual(result['summary']['highRisk'], 40)
        self.assertEqual(len(result['priorities']), dashboard.DASHBOARD_PRIORITY_LIMIT)
        self.assertEqual(len(result['openIssues']), 40)
        self.assertEqual(sum(row['overdue'] for row in result['openIssues']), result['summary']['overdue'])
        self.assertEqual(sum(row['ageDays'] >= 30 for row in result['openIssues']), result['summary']['aging'])
        self.assertEqual(sum(row['riskLevel'] == 'high' for row in result['openIssues']), result['summary']['highRisk'])
        self.assertEqual(sum(result['customerHealthSummary'].values()), 40)
        self.assertEqual(len(result['customerHealth']), dashboard.DASHBOARD_CUSTOMER_LIMIT)

    def test_all_open_list_includes_recent_non_priority_issues_once(self):
        self.issues.append({**self.issues[0], 'id': 999, 'status': 'assigned', 'priority': 'normal',
                            'created_at': '2026-08-26', 'updated_at': '2026-08-26', 'date_fields': {}})
        result = self._build({'role': 'member', 'displayName': '석지원'})
        self.assertEqual([row['id'] for row in result['openIssues']], [101, 999])
        self.assertEqual(len(result['openIssues']), result['summary']['open'])
