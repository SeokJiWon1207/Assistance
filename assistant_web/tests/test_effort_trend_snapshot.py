"""월별 차트가 최신 원문과 검증 가능한 집계 근거를 사용한다."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import date
from unittest import TestCase
from unittest.mock import patch

from backend.evaluation.verification import build_verification_report
from backend.reports.effort_trend import run_effort_trend_report
from backend.sources.mantis_collection import IssueBatch
from test_effort_delivery import issue


class MonthlySnapshotTests(TestCase):
    def report(self, rows, constraints=None):
        batch = IssueBatch(rows, {'search_complete': True, 'status': 'snapshot',
                                 'fetchedAt': '2026-10-27T00:00:00+00:00'})
        with patch('backend.sources.mantis_store.stream_complete_store', return_value=nullcontext(batch)):
            return run_effort_trend_report('최근 2개월 월별 공수 추이', months=2,
                                          today=date(2026, 10, 27), constraints=constraints)

    def rows(self):
        rows = [issue(1, '2026-09-03', 1.25, created='2026-08-01', handler='석지원'),
                issue(2, '', 3, created='2026-10-02', handler='석지원'),
                issue(3, '2026-10-09', 2, created='2026-09-05', handler='외부인')]
        for row in rows:
            row['severity'] = {'id': 50, 'name': 'minor', 'label': '개선'}
        return rows

    def test_fresh_store_replaces_index_and_keeps_two_date_bases_and_team_scope(self):
        c = {'mantis_team_members': ['석지원'], 'mantis_team_label': '우리 팀',
             'filter_clauses': [{'field': 'delivered_at', 'operator': 'between',
                                 'value': ['2026-09-01', '2026-10-31']}]}
        result = self.report(self.rows(), c)
        chart = result['effortTrend']
        self.assertIsNotNone(chart)
        series = next(s for s in chart['series'] if s['label'] == '개선')
        self.assertEqual(series['counts'], [0, 1])
        self.assertEqual(series['efforts'], [10.25, 0])
        self.assertEqual(chart['scopeLabel'], '우리 팀')
        self.assertTrue(result['retrieval']['stages'][0]['meta']['search_complete'])
        self.assertEqual(c['filter_clauses'][0]['field'], 'delivered_at')

    def test_no_fresh_store_does_not_show_stale_index_as_zero(self):
        with patch('backend.sources.mantis_store.stream_complete_store', return_value=nullcontext(None)):
            result = run_effort_trend_report('월별 공수 추이', months=2)
        self.assertIsNone(result['effortTrend'])
        self.assertIn('최신 전체 저장본', result['content'])
        verification = build_verification_report('월별 공수 추이', result['content'], [], result['retrieval'], result['raw']['mode'])
        self.assertNotEqual(verification['status'], 'pass')

    def test_recalculation_detects_tampered_monthly_cells_and_display(self):
        result = self.report(self.rows())
        verification = build_verification_report('월별 공수 추이', result['content'], result['sources'], result['retrieval'], result['raw']['mode'])
        self.assertEqual(verification['status'], 'pass', verification['checks'])
        self.assertEqual(len([d for d in verification['dimensions'] if d['id'] == 'calculation']), 1)
        corrupted = deepcopy(result['retrieval'])
        corrupted['stages'][0]['meta']['trend']['series'][1]['efforts'][0] += 1
        failed = build_verification_report('월별 공수 추이', result['content'], result['sources'], corrupted, result['raw']['mode'])
        self.assertEqual(failed['status'], 'fail')
        failed = build_verification_report('월별 공수 추이', result['content'].replace('등록 **2건**', '등록 **99건**'), result['sources'], result['retrieval'], result['raw']['mode'])
        self.assertEqual(failed['status'], 'fail')

    def test_complete_empty_snapshot_is_verified_without_inventing_sources(self):
        result = self.report([])
        verification = build_verification_report('월별 공수 추이', result['content'], [], result['retrieval'], result['raw']['mode'])
        self.assertEqual(verification['status'], 'pass', verification['checks'])

    def test_excluded_type_is_distinct_from_missing_type(self):
        rows = self.rows()[:1]
        rows[0]['severity'] = {'id': 10, 'name': 'feature', 'label': '제외/중복'}
        result = self.report(rows)
        self.assertIn('집계 대상 유형에 해당하는 이슈가 없습니다', result['content'])
        self.assertNotIn('동기화 상태', result['content'])
        rows[0]['severity'] = {}
        result = self.report(rows)
        self.assertIn('확인하지 못한 1건', result['content'])
        v = build_verification_report('월별 공수 추이', result['content'], [], result['retrieval'], result['raw']['mode'])
        self.assertNotEqual(v['status'], 'pass')
