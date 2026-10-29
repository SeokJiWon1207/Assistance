"""질문 해석의 연결 장애는 활성 로컬 모델로 전환하되 조회 조건은 검증한다."""
import copy
import unittest
from unittest.mock import patch

import requests

from backend import llm
from backend.execution import RequestStopped
from backend.intent import unified
from test_unified_accuracy_failures import NO_STATUS, USER, response
from test_unified_planner import proposal


PRIMARY = {'id': 'company', 'model': 'company-model', 'provider': 'openai-compatible', 'status': 'active'}
LOCAL = {'id': 'local', 'model': 'qwen3.5:9b', 'provider': 'ollama', 'status': 'active'}


class UnifiedModelFallbackTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.object(llm, 'get_model_registry', return_value=[PRIMARY, LOCAL]))
        self.enterContext(patch.object(llm, 'get_model_api_key', side_effect=lambda model: 'ollama'))
        self.question = '노션에서 신한 관련 문서 찾아줘'
        self.parsed = proposal(source='notion', operation='search', scope='all', status=NO_STATUS,
                               terms=['신한'])

    def plan(self):
        return unified.plan_question(self.question, PRIMARY, 'primary-key', USER, {})

    def test_timeout_or_open_circuit_switches_to_local_and_keeps_search_terms(self):
        for error in (requests.ReadTimeout(), llm.ModelCircuitOpenError()):
            with self.subTest(error=type(error).__name__):
                calls = []

                def send(model, key, payload, calls=calls, error=error, **kwargs):
                    calls.append((model['id'], key, payload['model'], kwargs['timeout']))
                    if model['id'] == PRIMARY['id']:
                        raise error
                    return response(self.parsed)

                with patch.object(unified, 'post_model_chat', side_effect=send):
                    result = self.plan()
                self.assertEqual(result.get('source'), 'notion')
                self.assertEqual(result['operation'], 'search')
                self.assertEqual(result['terms'], ['신한'])
                self.assertEqual(result['planner_model_id'], 'local')
                self.assertEqual(result['planner_fallback_from'], 'company')
                self.assertEqual(calls, [('company', 'primary-key', 'company-model', 12),
                                         ('local', 'ollama', 'qwen3.5:9b', 60)])

    def test_local_plan_must_preserve_exclusions_and_can_be_corrected(self):
        self.question = "노션에서 '배포 계획' 문서를 검색해줄래? 시작전 상태는 빼고"
        valid = proposal(source='notion', operation='search', scope='all', status=NO_STATUS,
                         filters=[{'field': 'title', 'operator': 'equals', 'value': '배포 계획',
                                   'evidence': "'배포 계획'"},
                                  {'field': 'status', 'operator': 'not_equals', 'value': '시작전',
                                   'evidence': '시작전 상태는 빼고'}])
        invalid = copy.deepcopy(valid)
        invalid['filters'][0].update(field='keyword', operator='contains')
        local_payloads = []

        def send(model, key, payload, **kwargs):
            if model['id'] == PRIMARY['id']:
                raise requests.ConnectionError()
            local_payloads.append(copy.deepcopy(payload))
            return response(invalid if len(local_payloads) == 1 else valid)

        with patch.object(unified, 'post_model_chat', side_effect=send):
            result = self.plan()
        self.assertEqual(result['operation'], 'search')
        self.assertEqual(result['filter_clauses'], [
            {'field': 'title', 'operator': 'equals', 'value': '배포 계획'},
            {'field': 'status', 'operator': 'not_equals', 'value': '시작전'}])
        self.assertEqual(len(local_payloads), 2)
        self.assertIn('계획 검증에 실패', local_payloads[-1]['messages'][-1]['content'])

    def test_unavailable_local_model_never_starts_an_unfiltered_search(self):
        with patch.object(unified, 'post_model_chat', side_effect=requests.ConnectionError()):
            result = self.plan()
        self.assertEqual(result['operation'], 'clarify')
        self.assertNotIn('resolved_constraints', result)

    def test_disabled_or_remote_alternatives_are_not_used(self):
        candidates = [PRIMARY, {**LOCAL, 'status': 'disabled'},
                      {**PRIMARY, 'id': 'other-remote'}]
        calls = []

        def send(model, *_args, **_kwargs):
            calls.append(model['id'])
            raise requests.ReadTimeout()

        with patch.object(llm, 'get_model_registry', return_value=candidates), \
                patch.object(unified, 'post_model_chat', side_effect=send):
            result = self.plan()
        self.assertEqual(result['operation'], 'clarify')
        self.assertEqual(calls, ['company', 'company'])

    def test_auth_errors_do_not_switch_models(self):
        error = requests.HTTPError(response=requests.Response())
        error.response.status_code = 401
        with patch.object(unified, 'post_model_chat', side_effect=error) as send:
            result = self.plan()
        self.assertEqual(result['operation'], 'clarify')
        self.assertEqual(send.call_count, 1)

    def test_local_planner_keeps_the_complete_prompt_in_its_context(self):
        with patch.object(llm.requests, 'post', return_value=response(self.parsed)) as post:
            result = unified.plan_question(self.question, LOCAL, 'ollama', USER, {})
        self.assertEqual(result['operation'], 'search')
        self.assertEqual(post.call_args.kwargs['json']['options']['num_ctx'], 16384)

    def test_cancellation_does_not_switch_models(self):
        with patch.object(unified, 'post_model_chat', side_effect=RequestStopped(cancelled=True)) as send:
            with self.assertRaises(RequestStopped):
                self.plan()
        self.assertEqual(send.call_count, 1)
