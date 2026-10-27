"""일별 공수는 답변 합계와 같은 고객사전달일·기입자 범위를 사용한다."""
from contextlib import nullcontext
from datetime import date
from unittest import TestCase
from unittest.mock import patch

from backend.reports import effort
from test_effort_delivery import issue


class EffortSparklineTests(TestCase):
    def report(self, rows, *, filtered=False, complete=True):
        meta = {'search_complete': complete, 'status': 'snapshot'}
        with patch.object(effort, 'effort_issues', return_value=nullcontext((rows, meta))):
            if filtered:
                return effort.run_filtered_effort_report('2026년 10월 공수', 2026, 10, {})
            return effort.run_person_effort_report('2026년 10월 석지원 공수', '석지원', 2026, 10)

    def test_person_daily_totals_match_sum_and_use_korean_delivery_not_registration(self):
        first = issue(1, '1790780400', .1, created='2026-10-20')
        first['custom_fields'].append({'field': {'name': '4QC공수'}, 'value': '석지원(0.2)'})
        rows = [first, issue(2, '2026-10-01', .3), issue(3, '2026-10-31', .05),
                issue(4, '', 50), issue(5, '2026-11-01', 90)]
        chart = self.report(rows)['effortChart']
        self.assertEqual(chart['periodFrom'], '2026-10-01')
        self.assertEqual(chart['periodTo'], '2026-10-31')
        days = {p['date']: p['amount'] for p in chart['dailyEffort']}
        self.assertEqual(days['2026-10-01'], .6)
        self.assertEqual(days['2026-10-02'], 0)
        self.assertEqual(days['2026-10-20'], 0)
        self.assertEqual(days['2026-10-31'], .05)
        self.assertEqual(round(sum(days.values()), 4), chart['totalEffort'])
        self.assertEqual(chart['issueCount'], 3)

    def test_filtered_report_uses_all_matching_contributors_and_keeps_partial_coverage(self):
        result = self.report([issue(1, '2026-10-01', .75), issue(2, '2026-10-01', 1.25)],
                             filtered=True, complete=False)
        chart = result['effortChart']
        self.assertEqual(chart['dailyEffort'][0], {'date': '2026-10-01', 'amount': 20.0})
        self.assertEqual(sum(p['amount'] for p in chart['dailyEffort']), chart['totalEffort'])
        self.assertFalse(result['retrieval']['stages'][0]['meta']['search_complete'])

    def test_current_month_stops_at_today_but_preserves_recorded_future_delivery(self):
        from backend.reports.effort_series import daily_effort
        row = {'date_fields': {'고객사전달일': '2026-10-01'}, 'effort_items': [{'amount': 2}]}
        points = daily_effort([row], '2026-10-01', '2026-10-31', today=date(2026, 10, 27))
        self.assertEqual(len(points), 27)
        self.assertEqual(points[-1], {'date': '2026-10-27', 'amount': 0})
        row['date_fields']['고객사전달일'] = '2026-10-30'
        points = daily_effort([row], '2026-10-01', '2026-10-31', today=date(2026, 10, 27))
        self.assertEqual(points[-1], {'date': '2026-10-30', 'amount': 2})
        self.assertEqual(len(points), 30)

    def test_long_unbounded_range_is_omitted_and_empty_results_do_not_invent_a_chart(self):
        from backend.reports.effort_series import daily_effort
        row = {'date_fields': {'고객사전달일': '2026-10-01'}, 'effort_items': [{'amount': 2}]}
        self.assertIsNone(daily_effort([row], '0001-01-01', '9999-12-31'))
        self.assertEqual(daily_effort([], '2026-10-01', '2026-10-31'), [])
        self.assertIsNone(self.report([])['effortChart'])

    def test_day_aggregation_keeps_precision_before_display_rounding(self):
        from backend.reports.effort_series import daily_effort
        rows = [{'date_fields': {'고객사전달일': day}, 'effort_items': [{'amount': .00006}]} for day in
                ['2026-10-01', '2026-10-02']]
        points = daily_effort(rows, '2026-10-01', '2026-10-02')
        self.assertEqual(round(sum(p['amount'] for p in points), 4), .0001)

    def test_single_day_and_leap_year_keep_calendar_alignment(self):
        from backend.reports.effort_series import daily_effort
        rows = [{'date_fields': {'고객사전달일': '2024-02-29'}, 'effort_items': [{'amount': 0}]}]
        self.assertEqual(daily_effort(rows, '2024-02-29', '2024-02-29'),
                         [{'date': '2024-02-29', 'amount': 0}])
        self.assertEqual(len(daily_effort(rows, '2024-01-01', '2024-12-31')), 366)
