from copy import deepcopy
import unittest
from unittest.mock import patch
import requests

from test_aggregation import raw_issue, rows
from test_aggregation_spec import specification


class AggregationNarrativeTests(unittest.TestCase):
    def daily_result(self):
        from backend.reports.aggregation import aggregate_rows
        return aggregate_rows(rows([
            raw_issue(1, '고객사A', '2026-11-02'), raw_issue(2, '고객사B', '2026-11-02'),
            raw_issue(3, '고객사B', '2026-11-02'), raw_issue(4, '고객사A', '2026-11-01')]),
            specification(group_by=['customer'], metrics=['issue_count'], comparison=None,
                          period={'from': '2026-11-02', 'to': '2026-11-02', 'evidence': '오늘'}), {})

    def test_daily_customer_summary_has_each_groups_calculated_facts(self):
        from backend.reports.aggregation_narrative import build_facts
        facts = build_facts(self.daily_result())
        groups = {tuple(f['group']): f for f in facts if f['group']}
        self.assertEqual({group: fact['value'] for group, fact in groups.items()},
                         {('고객사A',): 1, ('고객사B',): 2})
        self.assertTrue(all(f['period'] == '2026-11-02' for f in facts))
        self.assertFalse(any(f['kind'] in {'change', 'contribution'} for f in facts))

    def test_daily_customer_explanation_is_reviewed_and_incorrect_facts_still_rejected(self):
        from backend.reports.aggregation_narrative import build_facts, summarize_aggregation, validate_claim
        result = self.daily_result()
        facts = build_facts(result)
        fact = next((f for f in facts if f['group'] == ['고객사B']), None)
        self.assertIsNotNone(fact, '고객사별 집계 사실이 모델에 전달되어야 한다')
        claim = {'fact_ids': [fact['id']], 'text': '2026년 11월 2일 고객사B 등록 건수는 2건입니다.'}
        with patch('backend.reports.aggregation_narrative._chat', side_effect=[
                {'claims': [claim]}, {'supported': [True]}]) as chat:
            text, meta = summarize_aggregation('오늘 등록된 미종료 이슈를 고객사별로 요약해줘',
                                               result, {'id': 'test'}, 'key')
        self.assertEqual(meta['status'], 'accepted')
        self.assertEqual(chat.call_count, 2)
        self.assertIn('2026-11-02', text)
        catalog = {f['id']: f for f in facts}
        for invalid in (fact['text'].replace('고객사B', '고객사A'),
                        fact['text'].replace('2026-11-02', '2026-11-03'),
                        fact['text'].replace('2026-11-02', '2026-11'),
                        fact['text'].replace('2입니다', '9입니다'), fact['text'] + ' 100% 증가했습니다.'):
            with self.subTest(invalid=invalid):
                self.assertFalse(validate_claim({'fact_ids': [fact['id']], 'text': invalid}, catalog))

    def test_new_group_facts_preserve_months_other_and_input_limit(self):
        from backend.reports.aggregation import aggregate_rows
        from backend.reports.aggregation_narrative import MAX_FACTS, build_facts
        result = aggregate_rows(rows([
            raw_issue(1, 'A', '2026-08-02'), raw_issue(2, 'A', '2026-08-03'),
            raw_issue(3, 'B', '2026-09-02'), raw_issue(4, 'C', '2026-09-03')]),
            specification(metrics=['issue_count'], comparison=None, top_n=1), {})
        values = [f for f in build_facts(result) if f['kind'] == 'value']
        self.assertEqual({(tuple(f['group']), f['period']): f['value'] for f in values},
                         {(('A',), '2026-08'): 2, (('A',), '2026-09'): 0,
                          (('기타',), '2026-08'): 0, (('기타',), '2026-09'): 2})
        expanded = deepcopy(result)
        expanded['rows'] *= MAX_FACTS
        self.assertEqual(len(build_facts(expanded)), MAX_FACTS)

    def test_partial_month_value_fact_does_not_claim_the_whole_month(self):
        from backend.reports.aggregation import aggregate_rows
        from backend.reports.aggregation_narrative import build_facts, validate_claim
        result = aggregate_rows(rows([raw_issue(1, 'A', '2026-08-20')]),
                                specification(metrics=['issue_count'], comparison=None, period={
                                    'from': '2026-08-15', 'to': '2026-09-05', 'evidence': '기간'}), {})
        facts = build_facts(result)
        fact = next(f for f in facts if f['kind'] == 'value' and f['value'] == 1)
        self.assertEqual(fact['period'], '2026-08-15~2026-08-31')
        catalog = {f['id']: f for f in facts}
        self.assertFalse(validate_claim({'fact_ids': [fact['id']], 'text': fact['text'].replace(
            '2026-08-15~2026-08-31', '2026-08')}, catalog))

    def test_final_verification_rechecks_new_group_facts_and_rejects_tampering(self):
        from backend.evaluation.aggregation import verify_aggregation
        from backend.reports.aggregation_narrative import build_facts
        result = self.daily_result()
        result['source'] = {'search_complete': True}
        fact = next((f for f in build_facts(result) if f['group'] == ['고객사B']), None)
        self.assertIsNotNone(fact)
        claim = {'fact_ids': [fact['id']], 'text': fact['text']}
        retrieval = {'aggregation_result': result, 'aggregation_narrative': {
            'status': 'accepted', 'claims': [claim]}}
        for text, status in ((fact['text'], 'pass'), (fact['text'].replace('2입니다', '9입니다'), 'fail')):
            with self.subTest(status=status):
                claim['text'] = text
                checks = verify_aggregation('고객사별 요약', text, retrieval, 'validated_aggregation')['checks']
                self.assertEqual(next(c['status'] for c in checks if c['id'] == 'aggregation_narrative'), status)

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
