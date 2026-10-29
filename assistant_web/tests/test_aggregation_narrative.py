from copy import deepcopy
import unittest
from unittest.mock import patch
import requests

from test_aggregation import raw_issue, rows
from test_aggregation_spec import specification


class AggregationNarrativeTests(unittest.TestCase):
    def result(self, explain=True):
        from backend.reports.aggregation import aggregate_rows
        result = aggregate_rows(rows([
            raw_issue(1, '신한', '2026-08-01'), raw_issue(2, '신한', '2026-09-01'), raw_issue(3, '신한', '2026-09-02'),
            raw_issue(4, 'NH', '2026-08-01'), raw_issue(5, 'NH', '2026-09-01')]),
            specification(metrics=['issue_count'], explain=explain), {})
        result['source'] = {'search_complete': True, 'fetchedAt': '2026-10-01'}
        return result

    def test_contribution_is_delta_not_current_share(self):
        from backend.reports.aggregation_narrative import build_facts
        facts = build_facts(self.result())
        fact = next(f for f in facts if f['kind'] == 'contribution' and f['period'] == '2026-09' and f['group'] == ['신한'])
        self.assertEqual(fact['delta'], 1)
        self.assertEqual(fact['total_delta'], 1)
        self.assertEqual(fact['contribution_pct'], 100)
        self.assertIn('실제 원인', fact['text'])

    def test_writer_and_reviewer_accept_fact_bound_explanation(self):
        from backend.reports.aggregation_narrative import build_facts, summarize_aggregation
        result = self.result()
        fact = next(f for f in build_facts(result) if f['kind'] == 'contribution' and f['group'] == ['신한'] and f['period'] == '2026-09')
        claim = {'fact_ids': [fact['id']], 'text': fact['text']}
        with patch('backend.reports.aggregation_narrative._chat', side_effect=[{'claims': [claim]}, {'supported': [True]}]) as call:
            text, meta = summarize_aggregation('증가 기여도를 분석해줘', result, {'id': 'test'}, 'test-key')
        self.assertIn('신한', text)
        self.assertEqual(meta['status'], 'accepted')
        self.assertEqual(meta['calls'], 2)
        self.assertNotIn('test-key', str(meta))
        self.assertNotIn('일반 문의', str(call.call_args_list))

    def test_rejects_number_sign_period_group_and_unknown_fact_changes(self):
        from backend.reports.aggregation_narrative import build_facts, validate_claim
        facts = build_facts(self.result())
        fact = next(f for f in facts if f['kind'] == 'change' and f['group'] == ['신한'] and f['period'] == '2026-09')
        catalog = {f['id']: f for f in facts}
        self.assertTrue(validate_claim({'fact_ids': [fact['id']], 'text': fact['text']}, catalog))
        for text in [fact['text'].replace('+1', '-1'), fact['text'].replace('신한', '삼성'),
                     fact['text'].replace('2026-09', '2026-10'), fact['text'] + ' 추가 999건',
                     fact['text'].replace('+1 ', '-9 ')]:
            with self.subTest(text=text):
                self.assertFalse(validate_claim({'fact_ids': [fact['id']], 'text': text}, catalog))
        self.assertFalse(validate_claim({'fact_ids': ['unknown'], 'text': fact['text']}, catalog))
        self.assertFalse(validate_claim({'fact_ids': [fact['id']], 'text': fact['text'].replace('변동했습니다', '감소했습니다')}, catalog))

    def test_reviewer_rejection_and_timeout_keep_deterministic_fallback(self):
        from backend.reports.aggregation_narrative import build_facts, summarize_aggregation
        result = self.result()
        fact = build_facts(result)[0]
        draft = {'claims': [{'fact_ids': [fact['id']], 'text': fact['text']}]}
        for replies, status in [([draft, {'supported': [False]}], 'rejected'),
                                ([draft, requests.Timeout()], 'unavailable')]:
            with patch('backend.reports.aggregation_narrative._chat', side_effect=replies):
                text, meta = summarize_aggregation('분석', result, {'id': 'test'}, 'key')
            self.assertEqual(text, '')
            self.assertEqual(meta['status'], status)

    def test_simple_count_does_not_call_model_and_zero_delta_has_no_share(self):
        from backend.reports.aggregation_narrative import build_facts, summarize_aggregation
        result = self.result(explain=False)
        with patch('backend.reports.aggregation_narrative._chat', side_effect=AssertionError('unexpected model')):
            self.assertEqual(summarize_aggregation('건수', result, {'id': 'test'}, 'key')[1]['calls'], 0)
        changed = deepcopy(result)
        for row in changed['rows']:
            row['comparisons']['issue_count']['delta'] = 0
        self.assertFalse(any(f['kind'] == 'contribution' and f.get('contribution_pct') is not None for f in build_facts(changed)))

    def test_valid_paraphrase_gets_explicit_fact_period_and_metric(self):
        from backend.reports.aggregation_narrative import build_facts, summarize_aggregation
        result = self.result()
        fact = next(f for f in build_facts(result) if f['kind'] == 'change' and f['group'] == ['신한'] and f['period'] == '2026-09')
        draft = {'claims': [{'fact_ids': [fact['id']], 'text': '신한은 2건으로 전월 대비 1건(+100.0%) 증가했습니다.'}]}
        with patch('backend.reports.aggregation_narrative._chat', side_effect=[draft, {'supported': [True]}]):
            text, meta = summarize_aggregation('분석해줘', result, {'id': 'test'}, 'key')
        self.assertEqual(meta['status'], 'accepted')
        self.assertIn('2026-09', text)
        self.assertIn('등록 건수', text)

    def test_wrong_explicit_period_is_not_replaced_by_fact_period(self):
        from backend.reports.aggregation_narrative import build_facts, summarize_aggregation
        result = self.result()
        fact = next(f for f in build_facts(result) if f['kind'] == 'change' and f['group'] == ['신한'] and f['period'] == '2026-09')
        draft = {'claims': [{'fact_ids': [fact['id']], 'text': '2026년 10월 신한 등록 건수는 2건, +1 증가했습니다.'}]}
        with patch('backend.reports.aggregation_narrative._chat', return_value=draft):
            text, meta = summarize_aggregation('분석해줘', result, {'id': 'test'}, 'key')
        self.assertEqual(text, '')
        self.assertEqual(meta['status'], 'rejected')
