"""평가가 전달·완수·현재 본문·관련성을 혼동하지 않는지 확인한다."""
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from scripts.accuracy import benchmark

TODAY = date(2026, 11, 9)


def case(expect, requirements=None):
    turn = {'question': '한투 미해결 건수와 가이드', 'expect': expect}
    if requirements is not None:
        turn['requirements'] = requirements
    return {'id': 'C1', 'split': 'dev', 'category': 'explain', 'turns': [turn]}


def score(row, answer):
    return benchmark.score_cases([row], [{'case_id': 'C1', 'turn': 0, 'latency_ms': 10,
        'response': answer}], TODAY)


class OutcomeTests(unittest.TestCase):
    def test_agent_clarification_is_not_delivered_and_is_counted(self):
        answer = {'mode': 'agent_plan_clarification', 'content': '조건을 확인해 주세요.'}
        self.assertIsNotNone(benchmark.check_expectation({'type': 'delivered'}, answer, TODAY))
        self.assertIsNone(benchmark.check_expectation({'type': 'clarify'}, answer, TODAY))
        result = score(case([{'type': 'clarify'}]), answer)
        self.assertEqual(result['summary']['dev']['clarify_rate'], 1.0)
        self.assertTrue(result['results'][0]['passed'])

    def test_delivery_rejects_policy_denial_cancellation_and_no_evidence(self):
        for answer in [
            {'mode': 'agent_no_evidence'}, {'mode': 'model', 'verification': {'delivery': 'blocked'}},
            {'mode': 'agent_plan_clarification', 'agent': {'stopReason': 'policy_blocked'}},
            {'mode': 'agent_model', 'agent': {'stopReason': 'cancelled'}},
        ]:
            self.assertIsNotNone(benchmark.check_expectation({'type': 'delivered'}, answer, TODAY))

    def test_empty_answer_and_policy_denial_are_not_successful_delivery_or_clarification(self):
        self.assertIsNotNone(benchmark.check_expectation({'type': 'delivered'},
            {'mode': 'agent_model', 'content': '  '}, TODAY))
        self.assertIsNotNone(benchmark.check_expectation({'type': 'clarify'},
            {'mode': 'agent_plan_clarification', 'agent': {'stopReason': 'policy_blocked'}}, TODAY))

    def test_displayed_list_and_followup_order_are_not_source_card_order(self):
        raw = {'content': '1. #9 제목\n2. #2 제목\n\n관련 대상은 #9입니다.',
               'sources': [{'issue_id': 2}, {'issue_id': 9}],
               'sourcesForFollowUps': [{'type': 'mantis', 'issue_id': 9}, {'type': 'mantis', 'issue_id': 2}]}
        answer = benchmark.compact_response(raw)
        for kind in ('displayed_issue_ids', 'followup_issue_ids'):
            self.assertIsNone(benchmark.check_expectation({'type': kind, 'ids': [9, 2]}, answer, TODAY))
            self.assertIsNotNone(benchmark.check_expectation({'type': kind, 'ids': [2, 9]}, answer, TODAY))
        missing = benchmark.compact_response({k: v for k, v in raw.items() if k != 'sourcesForFollowUps'})
        self.assertIsNotNone(benchmark.check_expectation({'type': 'followup_issue_ids', 'ids': [9, 2]}, missing, TODAY))

    def test_body_success_does_not_prove_direct_relevance_or_survive_revision_change(self):
        digest = hashlib.sha256(b'chart save/load').hexdigest()
        answer = {'mode': 'agent_query_results', 'evidenceReads': [
            {'page_id': 'page-1', 'body_checked': True, 'content_complete': True, 'body_sha256': digest}],
                  'sources': [{'type': 'notion', 'page_id': 'page-1'}]}
        self.assertIsNone(benchmark.check_expectation({'type': 'current_body'}, answer, TODAY))
        requirement = {'type': 'evidence_relevance', 'level': 'direct', 'topic': '화살표 메모',
                       'judgments': [{'page_id': 'page-1', 'body_sha256': digest, 'level': 'related', 'reason': '저장/불러오기 참고'}]}
        self.assertIsNotNone(benchmark.check_expectation(requirement, answer, TODAY))
        requirement['judgments'][0]['level'] = 'direct'
        self.assertIsNone(benchmark.check_expectation(requirement, answer, TODAY))
        answer['sources'] = []
        self.assertIsNotNone(benchmark.check_expectation(requirement, answer, TODAY))
        answer['sources'] = [{'type': 'notion', 'page_id': 'page-1'}]
        answer['evidenceReads'][0]['body_sha256'] = 'changed'
        self.assertIsNotNone(benchmark.check_expectation(requirement, answer, TODAY))
        answer['evidenceReads'][0]['body_checked'] = False
        self.assertIsNotNone(benchmark.check_expectation({'type': 'current_body'}, answer, TODAY))

    def test_requirements_report_partial_completion_without_hiding_it_in_delivery(self):
        row = case([{'type': 'delivered'}], [
            {'id': 'count', 'label': '건수', 'expect': [{'type': 'contains', 'all': ['5건']}]},
            {'id': 'guide', 'label': '직접 가이드', 'expect': [{'type': 'current_body'}]},
        ])
        report = score(row, {'mode': 'agent_query_results', 'content': '5건입니다.'})
        self.assertFalse(report['results'][0]['passed'])
        summary = report['summary']['dev']['requirements']
        self.assertEqual((summary['passed'], summary['total']), (1, 2))
        self.assertEqual(summary['complete_turns'], 0)
        self.assertEqual(report['results'][0]['turns'][0]['outcome'], 'partial')

    def test_matching_items_cannot_hide_a_failed_condition_check(self):
        row = case([{'type': 'delivered'}, {'type': 'plan', 'customer': '한투'}], [
            {'id': 'count', 'label': '건수', 'expect': [{'type': 'contains', 'all': ['5건']}]}])
        report = score(row, {'mode': 'agent_query_results', 'content': '5건입니다.',
                           'queryPlan': {'resolved_constraints': {'customer_key': 'KB'}}})
        self.assertFalse(report['results'][0]['passed'])
        self.assertEqual(report['summary']['dev']['requirements']['complete_turns'], 0)
        self.assertEqual(report['results'][0]['turns'][0]['outcome'], 'partial')

    def test_unexecuted_followup_remains_in_completion_denominator(self):
        row = case([{'type': 'delivered'}], [{'id': 'first', 'label': '첫 조회', 'expect': [{'type': 'current_body'}]}])
        row['turns'].append({'question': '두 번째', 'expect': [{'type': 'delivered'}], 'requirements': [
            {'id': 'followup', 'label': '후속 조회', 'expect': [{'type': 'contains', 'all': ['제목']}]}]})
        report = benchmark.score_cases([row], [{'case_id': 'C1', 'turn': 0, 'latency_ms': 180000, 'error': 'Timeout'}], TODAY)
        self.assertEqual(report['summary']['dev']['requirements']['total'], 2)
        self.assertEqual(report['summary']['dev']['outcomes'], {'error': 1, 'not_run': 1})

    def test_requirements_validate_identifiers_and_independent_relevance_judgment(self):
        row = case([{'type': 'delivered'}], [{'id': 'x', 'label': '본문', 'expect': [{'type': 'current_body'}]}])
        self.assertEqual(benchmark.validate_case(row, 1), row)
        row['turns'][0]['requirements'].append(row['turns'][0]['requirements'][0])
        with self.assertRaises(benchmark.CaseError):
            benchmark.validate_case(row, 1)
        with self.assertRaises(benchmark.CaseError):
            benchmark.validate_expectation({'type': 'evidence_relevance', 'level': 'direct', 'topic': '가이드',
                'judgments': [{'page_id': 'p', 'level': 'direct'}]}, 'x')

    def test_aggregation_requires_both_server_groups_and_printed_values(self):
        expected = {'type': 'groups', 'values': {'정보 부족': 3, '할당': 2}}
        raw = {'content': '| 그룹 | 건수 |\n| 정보 부족 | 3 |\n| 할당 | 2 |',
               'retrieval': {'stages': [{'source': 'mantis_aggregation', 'status': 'ok', 'meta': {'search_complete': True}}]},
               'agentReview': {'aggregationResults': [{'groups': expected['values'], 'total_count': 5}]}}
        answer = benchmark.compact_response(raw)
        self.assertIsNone(benchmark.check_expectation(expected, answer, TODAY))
        answer['content'] = answer['content'].replace('| 3 |', '| 9 |')
        self.assertIsNotNone(benchmark.check_expectation(expected, answer, TODAY))
        answer['content'] = raw['content']
        answer['stages'][0]['meta']['search_complete'] = False
        self.assertIsNotNone(benchmark.check_expectation(expected, answer, TODAY))

    def test_runner_keeps_audit_order_observation_and_failed_latency(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        @contextmanager
        def observe():
            yield SimpleNamespace(report=lambda: {'model_attempts': 2, 'source_http_requests': 1,
                'unreported_usage_calls': 1, 'unpriced_calls': 2, 'prompt_tokens': 50, 'completion_tokens': 4,
                'cost': {'total': None, 'known_subtotal': None, 'currency': None}, 'evidence_reads': []})
        row = case([{'type': 'delivered'}])
        records = benchmark.run_cases([row], lambda r: {'content': '#9 제목', 'auditId': 'audit'}, 'test',
            request_factory=lambda *a: None, observation_factory=observe,
            audit_reader=lambda audit_id: [{'issue_id': 9}])
        self.assertEqual(records[0]['response']['followupIssueIds'], [9])
        report = benchmark.score_cases([row], records, TODAY)
        self.assertEqual(report['summary']['dev']['measurement']['model_attempts'], 2)
        self.assertIsNone(report['summary']['dev']['measurement']['cost_total'])
        self.assertEqual(report['summary']['dev']['latency_by_category']['explain']['samples'], 1)

    def test_manifest_detects_uncommitted_code_and_config_changes_without_secret_values(self):
        from scripts.accuracy.benchmark_provenance import code_version
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'backend').mkdir()
            (root / 'config').mkdir()
            source = root / 'backend/a.py'
            config = root / 'config/models.json'
            source.write_text('x = 1\n')
            config.write_text('{"private": "secret-value"}')
            before = code_version(root)
            self.assertNotIn('secret-value', str(before))
            source.write_text('x = 2\n')
            self.assertNotEqual(before, code_version(root))
            source.write_text('x = 1\n')
            config.write_text('{"private": "changed"}')
            self.assertNotEqual(before, code_version(root))

    def test_score_rejects_changed_cases_and_invalid_measurement(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / 'cases.jsonl'
            cases.write_text(json.dumps(case([{'type': 'delivered'}])) + '\n')
            args = SimpleNamespace(cases=str(cases), run_dir=str(root), fail_under=None)
            for manifest in [
                {'cases_sha256': 'changed'},
                {'cases_sha256': benchmark.file_sha256(cases), 'validity': 'invalid'},
                {'cases_sha256': benchmark.file_sha256(cases), 'validity': 'running'},
            ]:
                (root / 'manifest.json').write_text(json.dumps(manifest))
                with self.assertRaises(benchmark.CaseError):
                    benchmark.command_score(args)
