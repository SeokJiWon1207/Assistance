"""원문 버전과 실제 모델에 전달한 구간을 혼동하지 않는 근거 평가."""
import hashlib
import json
import unittest
from types import SimpleNamespace

from scripts.accuracy import benchmark_outcomes as outcomes
from scripts.accuracy.benchmark_observation import Observation


BODY = '개요\n기능 설정은 메뉴를 열고 저장을 선택한다.\n다른 기능 설명'
DIGEST = hashlib.sha256(BODY.encode()).hexdigest()
START, END = BODY.index('기능 설정'), BODY.index('\n다른')


def expectation(level='direct'):
    return {'type': 'evidence_relevance', 'topic': '기능 설정', 'level': 'direct', 'judgments': [{
        'page_id': 'page-1', 'body_sha256': DIGEST, 'body_scope': 'page', 'body_chars': len(BODY),
        'supporting_ranges': [{'start': START, 'end': END}] if level != 'irrelevant' else [],
        'level': level, 'reason': '설정·저장 절차를 원문에서 확인'}]}


def response(start=START, end=END):
    return {'sources': [{'page_id': 'page-1'}], 'evidenceReads': [{
        'page_id': 'page-1', 'body_checked': True, 'content_complete': True,
        'body_sha256': hashlib.sha256(BODY[start:end].encode()).hexdigest(),
        'page_body_sha256': DIGEST, 'page_body_chars': len(BODY), 'page_body_verified': True,
        'content_offset': start, 'returned_chars': end-start, 'truncated': True}]}


class EvidenceSpanTests(unittest.TestCase):
    def test_only_the_final_serialized_tool_body_counts_as_delivered_evidence(self):
        from backend.tools.registry import fit_result
        body = '앞부분 설명 ' * 800 + '직접 근거 구간'
        context = SimpleNamespace(ledger=SimpleNamespace(page_id_for_ref=lambda ref: 'page-1',
            notion_page=lambda pid: {'content': body, '_verified_body': True}))
        result = {'ok': True, 'tool': 'get_notion_page_content', 'results': [{
            'ref': 'N1', 'content': body, 'content_offset': 0, 'next_offset': None,
            'body_checked': True, 'content_complete': True}]}
        measured = Observation({}, {})
        measured.record_tool('get_notion_page_content', result, context)
        self.assertEqual(measured.report()['evidence_reads'], [])
        encoded = fit_result({**result, 'remaining_budget': {'tool_calls': 3}}, 900)
        measured.record_delivery(result, encoded)
        self.assertEqual(measured.report()['evidence_reads'], [])
        measured.record_model_delivery({'messages': [{'role': 'tool', 'content': encoded}]})
        sent = json.loads(encoded)['results'][0]
        read = measured.report()['evidence_reads'][0]
        self.assertLess(read['returned_chars'], len(body))
        self.assertEqual(read['returned_chars'], len(sent['content']))
        self.assertEqual(read['body_sha256'], hashlib.sha256(sent['content'].encode()).hexdigest())
        self.assertTrue(read['page_body_verified'])
        self.assertTrue(read['delivery_verified'])
        expected = expectation()
        expected['judgments'][0].update(body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_chars=len(body),
            supporting_ranges=[{'start': len(body)-8, 'end': len(body)}])
        self.assertIn('근거 구간', outcomes.check(expected, {'sources': [{'page_id': 'page-1'}], 'evidenceReads': [read]}))

    def test_body_removed_at_final_serialization_is_not_observed_as_read(self):
        from backend.tools.registry import fit_result
        context = SimpleNamespace(ledger=SimpleNamespace(page_id_for_ref=lambda ref: 'page-1'))
        result = {'ok': True, 'tool': 'get_notion_page_content', 'metadata': 'x' * 2000,
                  'results': [{'ref': 'N1', 'content': '짧은 원문', 'body_checked': True, 'content_complete': True}]}
        measured = Observation({}, {})
        measured.record_tool('get_notion_page_content', result, context)
        measured.record_delivery(result, fit_result(result, 500))
        self.assertEqual(measured.report()['evidence_reads'], [])

    def test_diagnostics_distinguish_unassessed_unread_and_irrelevant(self):
        from scripts.accuracy.benchmark_relevance import assess_relevance
        cases = [
            (expectation(), response(), 'supported'),
            (expectation(), response(0, START), 'support_not_read'),
            (expectation('irrelevant'), response(), 'insufficient_relevance'),
            ({**expectation(), 'judgments': []}, response(), 'unassessed_version'),
            (expectation(), {}, 'body_not_read'),
        ]
        items = []
        for expected, answer, code in cases:
            diagnostic = assess_relevance(expected, answer)
            self.assertEqual(diagnostic['code'], code)
            items.append({'id': 'direct', 'label': '직접 근거', 'status': 'pass' if diagnostic['passed'] else 'fail',
                          'evidence_diagnostics': [diagnostic]})
        summary = outcomes.summarize_requirements([{'requirements': items, 'outcome': 'partial', 'passed': False}])
        self.assertEqual(summary['by_id']['direct']['evidence_diagnostics'], {c[2]: 1 for c in cases})

    def test_same_current_page_is_assessed_when_the_read_slice_changes(self):
        for start, end in ((0, len(BODY)), (START, END), (START-1, END+1)):
            with self.subTest(start=start, end=end):
                self.assertIsNone(outcomes.check(expectation(), response(start, end)))

    def test_unread_support_cannot_pass_from_the_page_hash_alone(self):
        result = outcomes.check(expectation(), response(0, START))
        self.assertIsNotNone(result)
        self.assertIn('근거 구간', result)

    def test_multiple_reads_cover_support_but_gaps_do_not(self):
        middle = (START+END)//2
        answer = response(START, middle)
        answer['evidenceReads'] += response(middle, END)['evidenceReads']
        self.assertIsNone(outcomes.check(expectation(), answer))
        answer['evidenceReads'][1]['content_offset'] += 1
        answer['evidenceReads'][1]['returned_chars'] -= 1
        self.assertIn('근거 구간', outcomes.check(expectation(), answer))

    def test_changed_unverified_or_unretained_pages_do_not_inherit_a_judgment(self):
        for field, value in (('page_body_sha256', 'f'*64), ('page_body_verified', False),
                             ('page_body_chars', len(BODY)+1), ('content_complete', False)):
            answer = response()
            answer['evidenceReads'][0][field] = value
            self.assertIsNotNone(outcomes.check(expectation(), answer))
        answer = response(); answer['sources'] = []
        self.assertIsNotNone(outcomes.check(expectation(), answer))

    def test_related_and_irrelevant_pages_do_not_become_direct_evidence(self):
        for level in ('related', 'irrelevant'):
            self.assertIn('미충족', outcomes.check(expectation(level), response()))

    def test_page_judgments_need_reviewed_support_and_valid_ranges(self):
        outcomes.validate_expectation(expectation())
        for ranges in ([], [{'start': -1, 'end': END}], [{'start': START, 'end': len(BODY)+1}],
                       [{'start': True, 'end': END}], [{'start': END, 'end': START}]):
            item = expectation(); item['judgments'][0]['supporting_ranges'] = ranges
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                outcomes.validate_expectation(item)

    def test_observation_records_stable_page_version_and_verifies_the_delivered_span(self):
        page = {'content': BODY, '_verified_body': True, 'content_incomplete': False}
        context = SimpleNamespace(ledger=SimpleNamespace(page_id_for_ref=lambda ref: 'page-1', notion_page=lambda pid: page))
        measured = Observation({}, {})
        for start, end in ((0, END), (START, len(BODY))):
            result = {'ok': True, 'tool': 'get_notion_page_content', 'truncated': True, 'results': [{
                'ref': 'N1', 'content': BODY[start:end], 'content_offset': start,
                'body_checked': True, 'content_complete': True}]}
            measured.record_tool('get_notion_page_content', result, context)
            encoded = json.dumps(result)
            measured.record_delivery(result, encoded)
            measured.record_model_delivery({'messages': [{'role': 'tool', 'content': encoded}]})
        reads = measured.report()['evidence_reads']
        self.assertEqual([r.get('page_body_sha256') for r in reads], [DIGEST, DIGEST])
        self.assertNotEqual(reads[0]['body_sha256'], reads[1]['body_sha256'])
        self.assertTrue(all(r['page_body_verified'] for r in reads))
        self.assertTrue(all('content' not in r for r in reads))

    def test_empty_current_body_is_observed_but_cannot_satisfy_body_reading(self):
        context = SimpleNamespace(ledger=SimpleNamespace(page_id_for_ref=lambda ref: 'page-1',
            notion_page=lambda pid: {'content': '', '_verified_body': True, 'content_incomplete': False}))
        measured = Observation({}, {})
        result = {'ok': True, 'tool': 'get_notion_page_content', 'results': [{
            'ref': 'N1', 'content': '', 'body_checked': True, 'content_complete': True}]}
        measured.record_tool('get_notion_page_content', result, context)
        encoded = json.dumps(result)
        measured.record_delivery(result, encoded)
        measured.record_model_delivery({'messages': [{'role': 'tool', 'content': encoded}]})
        reads = measured.report()['evidence_reads']
        self.assertEqual(len(reads), 1)
        self.assertEqual(reads[0]['returned_chars'], 0)
        self.assertEqual(outcomes.completed_reads({'evidenceReads': reads}), [])


if __name__ == '__main__':
    unittest.main()
