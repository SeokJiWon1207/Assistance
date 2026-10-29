from contextlib import contextmanager
from datetime import date
import unittest
from unittest.mock import patch

from backend.sources.mantis import normalize_issue
from backend.sources.mantis_collection import IssueBatch
from backend.sources.mantis_store import render_document
from test_aggregation_spec import specification


def raw_issue(i, customer='신한', created='2026-09-10', delivered='2026-09-20', effort='석지원(1.5)',
              handler='석지원', platform='HTS', title='일반 문의'):
    return {'id': i, 'project': {'name': customer}, 'summary': title, 'created_at': created,
            'status': {'name': 'assigned'}, 'severity': {'name': 'minor', 'label': '문의'},
            'handler': {'real_name': handler}, 'reporter': {'real_name': '요청자'},
            'custom_fields': [{'field': {'name': name}, 'value': value} for name, value in (
                ('고객사전달일', delivered), ('1문의공수', effort), ('매체구분', platform))]}


def rows(raw):
    for item in raw:
        normalized = normalize_issue(item)
        yield normalized['created_at'], normalized['severity_label'], render_document(item), normalized


@contextmanager
def complete_store(raw, **metadata):
    yield IssueBatch(raw, {'search_complete': True, 'fetchedAt': '2026-10-01T00:00:00Z', **metadata})


class AggregationTests(unittest.TestCase):
    def aggregate(self, raw, spec=None, constraints=None):
        from backend.reports.aggregation import aggregate_rows
        return aggregate_rows(rows(raw), spec or specification(), constraints or {}, today=date(2026, 10, 29))

    def test_compound_groups_use_independent_metric_dates_and_deduplicate(self):
        a = raw_issue(1, created='2026-08-01', delivered='2026-09-02', effort='석지원(2)')
        b = raw_issue(2, created='2026-09-01', delivered='2026-08-02', effort='석지원(3)')
        result = self.aggregate([a, a, b])
        self.assertEqual(result['totals'], {'issue_count': 2, 'effort': 5})
        self.assertEqual([r['values'] for r in result['rows']], [
            {'issue_count': 1, 'effort': 3}, {'issue_count': 1, 'effort': 2}])
        self.assertEqual(result['duplicates'], 1)
        self.assertEqual(result['date_basis'], {'issue_count': 'created_at', 'effort': 'delivered_at'})

    def test_fixed_top_n_and_other_preserve_each_month_and_totals(self):
        raw = [raw_issue(1, 'A', '2026-08-02'), raw_issue(2, 'A', '2026-08-03'),
               raw_issue(3, 'B', '2026-09-02'), raw_issue(4, 'C', '2026-09-03')]
        result = self.aggregate(raw, specification(metrics=['issue_count'], top_n=1))
        self.assertEqual(result['totals']['issue_count'], 4)
        self.assertEqual([(r['group'], r['period'], r['values']['issue_count'], r['is_other']) for r in result['rows']],
                         [(['A'], '2026-08', 2, False), (['A'], '2026-09', 0, False),
                          (['기타'], '2026-08', 0, True), (['기타'], '2026-09', 2, True)])
        self.assertEqual(result['rows'][1]['comparisons']['issue_count']['percentage'], -100)
        self.assertEqual(result['rows'][3]['comparisons']['issue_count']['state'], 'new')

    def test_previous_month_outside_display_window_is_computed(self):
        spec = specification(metrics=['issue_count'], period={
            'from': '2026-09-01', 'to': '2026-09-30', 'evidence': '9월'})
        result = self.aggregate([raw_issue(1, created='2026-08-02'), raw_issue(2)], spec)
        self.assertEqual(result['totals']['issue_count'], 1)
        self.assertEqual(result['rows'][0]['comparisons']['issue_count'],
                         {'previous': 1, 'delta': 0, 'percentage': 0.0, 'state': 'unchanged'})

    def test_partial_month_has_no_misleading_percentage(self):
        spec = specification(metrics=['issue_count'], period={
            'from': '2026-09-01', 'to': '2026-09-15', 'evidence': '9월 15일까지'})
        result = self.aggregate([raw_issue(1, created='2026-08-02'), raw_issue(2)], spec)
        self.assertEqual(result['rows'][0]['comparisons']['issue_count']['state'], 'partial_period')
        self.assertIsNone(result['rows'][0]['comparisons']['issue_count']['percentage'])

    def test_effort_counts_only_selected_contributor_not_handler(self):
        result = self.aggregate([raw_issue(1, effort='석지원(0.1), 김개발(0.2)', handler='김개발'),
                                 raw_issue(2, effort='석지원(0.2)', handler='석지원')],
                                specification(metrics=['effort']), {'filter_clauses': [
                                    {'field': 'effort_person', 'operator': 'equals', 'value': '석지원'}]})
        self.assertEqual(result['totals']['effort'], 0.3)

    def test_missing_delivery_is_not_registration_and_empty_month_is_zero(self):
        result = self.aggregate([raw_issue(1, delivered='')])
        self.assertEqual(result['totals'], {'issue_count': 1, 'effort': 0})
        self.assertEqual(result['missing_dates']['effort'], 1)
        self.assertEqual(result['rows'][0]['values']['issue_count'], 0)

    def test_execution_applies_raw_filters_and_refuses_incomplete_store(self):
        from backend.reports.aggregation import execute_aggregation
        raw = [raw_issue(1), raw_issue(2, platform='MTS'), raw_issue(3, title='점검')]
        plan = {'aggregation': specification(), 'resolved_constraints': {'filter_clauses': [
            {'field': 'platform', 'operator': 'equals', 'value': 'hts'},
            {'field': 'title', 'operator': 'not_contains', 'value': '점검'}]}}
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: complete_store(raw)):
            result = execute_aggregation(plan)
        self.assertEqual(result['totals']['issue_count'], 1)
        self.assertTrue(result['source']['search_complete'])
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: complete_store(raw, search_complete=False)), self.assertRaises(LookupError):
            execute_aggregation(plan)
