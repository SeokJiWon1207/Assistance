"""실제 전송 경계에서 모든 시도·사용량을 세고 미확인 비용을 보존한다."""
import json
import unittest
from unittest.mock import patch

import requests


def reply(code, data):
    response = requests.Response()
    response.status_code = code
    response._content = json.dumps(data).encode()
    return response


class ObservationTests(unittest.TestCase):
    def test_only_a_successful_request_with_the_final_tool_message_proves_delivery(self):
        from types import SimpleNamespace
        from scripts.accuracy.benchmark_observation import Observation
        measured = Observation({'https://model.test/chat': {'luna': 'luna'}}, {})
        result = {'ok': True, 'tool': 'get_notion_page_content', 'results': [{
            'ref': 'N1', 'content': 'private-body', 'body_checked': True, 'content_complete': True}]}
        context = SimpleNamespace(ledger=SimpleNamespace(page_id_for_ref=lambda ref: 'page-1'))
        measured.record_tool('get_notion_page_content', result, context)
        encoded = json.dumps(result)
        measured.record_delivery(result, encoded)
        for code, content in ((503, encoded), (200, '{}')):
            request = requests.Request('POST', 'https://model.test/chat', json={'model': 'luna',
                'messages': [{'role': 'tool', 'content': content}]}).prepare()
            measured.send(lambda *a, status=code, **kw: reply(status, {}), None, request)
            self.assertEqual(measured.report()['evidence_reads'], [])
        request = requests.Request('POST', 'https://model.test/chat', json={'model': 'luna',
            'messages': [{'role': 'tool', 'content': encoded}]}).prepare()
        measured.send(lambda *a, **kw: reply(200, {}), None, request)
        self.assertEqual(len(measured.report()['evidence_reads']), 1)
        self.assertNotIn('private-body', json.dumps(measured.report()))

    def test_attempts_fallback_and_missing_usage_are_counted_without_prompt_leak(self):
        from scripts.accuracy.benchmark_observation import observe_requests
        responses = [requests.Timeout('secret-url'), reply(503, {}),
                     reply(200, {'usage': {'prompt_tokens': 1000, 'completion_tokens': 100,
                                          'prompt_tokens_details': {'cached_tokens': 200}}}),
                     reply(200, {'usage': {'prompt_tokens': 50, 'completion_tokens': 20}}), reply(200, {})]
        endpoints = {'https://model.test/chat': 'luna', 'https://backup.test/chat': 'gemini'}
        with patch.object(requests.Session, 'send', side_effect=responses), observe_requests(endpoints) as measured:
            session = requests.Session()
            for url in ['https://model.test/chat'] * 3 + ['https://backup.test/chat', 'https://api.notion.com/v1/pages/p']:
                request = requests.Request('POST', url, json={'model': 'luna', 'messages': 'secret-body'},
                                           headers={'Authorization': 'secret-token'}).prepare()
                try:
                    session.send(request)
                except requests.Timeout:
                    pass
        report = measured.report()
        self.assertEqual(report['model_attempts'], 4)
        self.assertEqual(report['unreported_usage_calls'], 2)
        self.assertEqual((report['prompt_tokens'], report['completion_tokens'], report['cached_tokens']), (1050, 120, 200))
        self.assertEqual(report['source_http_requests'], 1)
        self.assertEqual(set(report['models']), {'luna', 'gemini'})
        self.assertIsNone(report['cost']['total'])
        self.assertNotIn('secret', json.dumps(report))

    def test_cost_uses_versioned_rates_and_preserves_unknown_total(self):
        from scripts.accuracy.benchmark_observation import observe_requests, validate_prices
        rates = {'currency': 'USD', 'as_of': '2026-11-09', 'source': 'test contract', 'models': {
            'luna': {'input_per_million': 2, 'cached_input_per_million': .5, 'output_per_million': 8}}}
        self.assertEqual(validate_prices(rates), rates)
        payload = {'usage': {'prompt_tokens': 1000, 'completion_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 200}}}
        with patch.object(requests.Session, 'send', return_value=reply(200, payload)), \
             observe_requests({'https://model.test/chat': 'luna'}, rates) as measured:
            requests.Session().send(requests.Request('POST', 'https://model.test/chat', json={}).prepare())
        self.assertAlmostEqual(measured.report()['cost']['total'], .0025)
        rates['models']['luna']['input_per_million'] = -1
        with self.assertRaises(ValueError):
            validate_prices(rates)

    def test_native_usage_and_unpriced_models_are_not_free(self):
        from scripts.accuracy.benchmark_observation import observe_requests
        with patch.object(requests.Session, 'send', return_value=reply(200, {'prompt_eval_count': 10, 'eval_count': 3})), \
             observe_requests({'http://localhost/api/chat': 'company'}) as measured:
            requests.Session().send(requests.Request('POST', 'http://localhost/api/chat').prepare())
        report = measured.report()
        self.assertEqual(report['unreported_usage_calls'], 0)
        self.assertEqual(report['prompt_tokens'], 10)
        self.assertEqual(report['unpriced_calls'], 1)
        self.assertIsNone(report['cost']['total'])

    def test_evidence_capture_requires_successful_current_body_read(self):
        from scripts.accuracy.benchmark_observation import Observation
        from types import SimpleNamespace
        measured = Observation({}, {})
        context = SimpleNamespace(ledger=SimpleNamespace(page_id_for_ref=lambda ref: 'page-1'))
        body = {'ref': 'N1', 'body_checked': True, 'content_complete': True, 'content': '본문'}
        measured.record_tool('search_notion_pages', {'ok': True, 'results': [body]}, context)
        self.assertEqual(measured.report()['evidence_reads'], [])
        result = {'ok': True, 'tool': 'get_notion_page_content', 'results': [body]}
        measured.record_tool('get_notion_page_content', result, context)
        encoded = json.dumps(result)
        measured.record_delivery(result, encoded)
        measured.record_model_delivery({'messages': [{'role': 'tool', 'content': encoded}]})
        read = measured.report()['evidence_reads'][0]
        self.assertEqual(read['page_id'], 'page-1')
        self.assertEqual(len(read['body_sha256']), 64)
        self.assertNotIn('content', read)

    def test_cache_writes_response_tier_and_request_cost_distribution(self):
        from scripts.accuracy.benchmark_observation import observe_requests, summarize_measurements
        rates = {'currency': 'USD', 'as_of': '2026-11-09', 'source': 'test contract', 'models': {'luna': {
            'input_per_million': 1, 'cached_input_per_million': .1, 'output_per_million': 5,
            'cache_write_input_per_million': 1.25, 'cache_write_12h_input_per_million': 2.5,
            'service_tiers': ['default'], 'endpoint_origin': 'https://model.test'}}}
        payload = {'service_tier': 'default', 'usage': {'prompt_tokens': 1000, 'completion_tokens': 10,
                   'prompt_tokens_details': {'cached_tokens': 300, 'cache_write_tokens': 200}}}
        with patch.object(requests.Session, 'send', return_value=reply(200, payload)), \
             observe_requests({'https://model.test/chat': {'luna': 'luna'}}, rates) as measured:
            requests.Session().send(requests.Request('POST', 'https://model.test/chat',
                json={'model': 'luna', 'messages': 'secret', 'service_tier': 'auto'}).prepare())
        report = measured.report()
        self.assertAlmostEqual(report['cost']['total'], .00083)
        self.assertEqual(report['cache_write_tokens'], 200)
        self.assertEqual(report['unreported_cache_write_calls'], 0)
        self.assertNotIn('secret', json.dumps(report))
        summary = summarize_measurements([{'observation': report}, {'observation': report}])
        self.assertAlmostEqual(summary['cost_per_request'], .00083)
        self.assertAlmostEqual(summary['cost_p95'], .00083)
        missing = summarize_measurements([{'observation': report}, {}])
        self.assertIsNone(missing['cost_total'])
        self.assertIsNone(missing['cost_p95'])
