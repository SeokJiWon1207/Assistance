"""차트는 조회한 실제 범위의 등록 건수를 사용한다."""
import unittest
from datetime import date
from unittest.mock import patch

from backend import workspace
from backend.reports import dashboard


class ChartDataTests(unittest.TestCase):
    def test_daily_registration_includes_closed_and_zero_days_not_updates(self):
        from backend.chart_data import daily_registrations
        rows = [
            {'created_at': '2026-10-24T23:10:00', 'status': 'closed'},
            {'created_at': '2026-10-26', 'status': 'assigned'},
            {'created_at': '2026-10-20', 'updated_at': '2026-10-25'},
            {'created_at': '2026-10-27'}, {'created_at': 'invalid'},
        ]
        self.assertEqual(daily_registrations(rows, date(2026, 10, 24), date(2026, 10, 26)), [
            {'date': '2026-10-24', 'count': 1},
            {'date': '2026-10-25', 'count': 0},
            {'date': '2026-10-26', 'count': 1},
        ])

    def test_dashboard_trend_matches_scoped_new_total_in_partial_source(self):
        rows = [
            {'id': 1, 'handler': '석지원', 'created_at': '2026-10-26', 'status': 'closed'},
            {'id': 2, 'handler': '석지원', 'created_at': '2026-10-25', 'status': 'assigned'},
            {'id': 3, 'handler': '외부 담당자', 'created_at': '2026-10-26', 'status': 'closed'},
        ]
        with patch.object(dashboard, '_load_issues', return_value=(rows, {'status': 'snapshot', 'search_complete': False})):
            result = dashboard.build_operations_dashboard({'role': 'member'}, days=7, today=date(2026, 10, 26))
        self.assertEqual(sum(row['count'] for row in result['intakeTrend']), result['summary']['newIssues'])
        self.assertEqual(sum(row['count'] for row in result['intakeTrend']), 2)
        self.assertEqual(len(result['intakeTrend']), 7)
        self.assertFalse(result['meta']['search_complete'])

    def test_personal_trend_uses_all_assigned_not_only_open(self):
        today = date.today().isoformat()
        rows = [{'id': 1, 'handler': '석지원', 'created_at': today, 'status': 'closed'},
                {'id': 2, 'handler': '석지원', 'created_at': today, 'status': 'assigned'}]
        with patch.object(workspace, '_my_mantis_issues', return_value=(
            '석지원', rows, {'status': 'snapshot', 'search_complete': True}
        )), patch.object(workspace, 'init_workspace_db'), patch.object(
            workspace, '_notifications', return_value=[]
        ), patch('backend.workspace_notifications.collect_workspace_events', return_value=([], {})):
            result = workspace.build_my_workspace({'uid': 'chart-test'})
        self.assertEqual(len(result['mantis']['issues']), 1)
        self.assertEqual(sum(row['count'] for row in result['weeklyBrief']['intakeTrend']), 2)
        self.assertEqual(result['weeklyBrief']['intakeTrend'][-1]['date'], today)
        self.assertEqual(result['weeklyBrief']['newIssues'], 2)
