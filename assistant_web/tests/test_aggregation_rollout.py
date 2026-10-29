import os
import unittest
from unittest.mock import patch

from test_aggregation import raw_issue, complete_store
from test_aggregation_spec import proposal, specification
from backend.intent.unified_validation import validate_plan


class AggregationRolloutTests(unittest.TestCase):
    def plan(self, **kwargs):
        return validate_plan('전사 2026년 8월부터 9월까지 고객사별 건수', proposal(
            aggregation=specification(group_by=['customer'], metrics=['issue_count'], comparison=None, explain=False, **kwargs)), {}, {})

    def test_shadow_returns_legacy_answer_and_compares_same_generation(self):
        from backend.reports.aggregation_rollout import run_aggregation_report
        plan = self.plan()
        with patch.dict(os.environ, {'MANTIS_AGGREGATION_MODE': 'shadow'}), patch(
                'backend.sources.mantis_store.stream_complete_store', side_effect=lambda: complete_store([raw_issue(1)])), patch(
                'backend.sources.mantis.stream_complete_store', side_effect=lambda: complete_store([raw_issue(1)])):
            result = run_aggregation_report('전사 고객사별 건수', plan, {}, '')
        self.assertEqual(result['raw']['mode'], 'deterministic_mantis_count')
        self.assertIn('총 1건', result['content'])
        audit = result['retrieval']['aggregation_shadow']
        self.assertEqual(audit['status'], 'matched')
        self.assertEqual(audit['legacy_totals'], {'issue_count': 1})
        self.assertEqual(result['retrieval']['query_plan']['aggregation'], plan['aggregation'])

    def test_mismatch_does_not_replace_legacy_answer(self):
        from backend.reports.aggregation_rollout import run_aggregation_report
        legacy = {'content': '기존 2건', 'sources': [], 'raw': {'mode': 'deterministic_mantis_count'},
                  'retrieval': {'stages': [{'source': 'mantis_count', 'status': 'ok', 'meta': {
                      'total_count': 2, 'group_counts': {'신한': 2}, 'search_complete': True,
                      'fetchedAt': '2026-10-01T00:00:00Z'}}]}}
        with patch.dict(os.environ, {'MANTIS_AGGREGATION_MODE': 'shadow'}), patch(
                'backend.sources.mantis_store.stream_complete_store', side_effect=lambda: complete_store([raw_issue(1)])), patch(
                'backend.reports.aggregate.build_mantis_count_report', return_value=legacy):
            result = run_aggregation_report('건수', self.plan(), {}, '')
        self.assertEqual(result['content'], '기존 2건')
        self.assertEqual(result['retrieval']['aggregation_shadow']['status'], 'mismatch')

    def test_different_generation_is_not_a_numeric_regression(self):
        from backend.reports.aggregation_rollout import compare_legacy
        new = {'totals': {'issue_count': 1}, 'source': {'fetchedAt': 'new'}, 'rows': []}
        legacy = {'retrieval': {'stages': [{'source': 'mantis_count', 'status': 'ok', 'meta': {
            'fetchedAt': 'old', 'total_count': 2, 'search_complete': True}}]}}
        self.assertEqual(compare_legacy(legacy, new, self.plan()['aggregation'])['status'], 'not_comparable')

    def test_new_shape_is_not_silently_reduced_in_shadow_and_works_in_primary(self):
        from backend.reports.aggregation_rollout import run_aggregation_report
        plan = validate_plan('2026년 8월부터 9월까지 고객사별 월별 건수', proposal(
            aggregation=specification(metrics=['issue_count'], explain=False)), {}, {})
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: complete_store([raw_issue(1)])):
            with patch.dict(os.environ, {'MANTIS_AGGREGATION_MODE': 'shadow'}):
                shadow = run_aggregation_report('고객사별 월별 건수', plan, {}, '')
            with patch.dict(os.environ, {'MANTIS_AGGREGATION_MODE': 'primary'}):
                primary = run_aggregation_report('고객사별 월별 건수', plan, {}, '')
        self.assertEqual(shadow['retrieval']['aggregation_shadow']['status'], 'not_comparable')
        self.assertNotIn('총 1건', shadow['content'])
        self.assertEqual(primary['aggregation']['totals'], {'issue_count': 1})
        self.assertIn('2026-09', primary['content'])

    def test_off_does_not_scan_new_executor_and_unavailable_is_not_zero(self):
        from backend.reports.aggregation_rollout import run_aggregation_report
        with patch.dict(os.environ, {'MANTIS_AGGREGATION_MODE': 'off'}), patch(
                'backend.reports.aggregation_rollout.execute_aggregation', side_effect=AssertionError('must not scan')), patch(
                'backend.reports.aggregate.build_mantis_count_report', return_value={'content': '기존', 'sources': [], 'retrieval': {}, 'raw': {}}):
            self.assertEqual(run_aggregation_report('건수', self.plan(), {}, '')['content'], '기존')
        with patch.dict(os.environ, {'MANTIS_AGGREGATION_MODE': 'primary'}), patch(
                'backend.reports.aggregation_rollout.execute_aggregation', side_effect=LookupError('저장본 없음')):
            result = run_aggregation_report('건수', self.plan(), {}, '')
        self.assertNotIn('총 0', result['content'])
        self.assertEqual(result['retrieval']['aggregation_shadow']['status'], 'unavailable')
