"""Gemini 시험 모델의 키 처리와 유료 호출 옵션 계약을 검증한다."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from backend import llm


class GeminiModelTests(unittest.TestCase):
    def setUp(self):
        llm.reset_model_circuits()
        self.model = {
            'id': 'gemini-3.1-flash-lite',
            'model': 'gemini-3.1-flash-lite',
            'provider': 'gemini',
            'endpoint': 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
            'reasoningEffort': 'minimal',
            'maxOutputTokens': 2048,
        }

    def tearDown(self):
        llm.reset_model_circuits()

    def test_gemini_registry_uses_its_own_key_and_hides_private_settings(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(llm, 'CONFIG_DIR', Path(directory)), \
                patch.dict(os.environ, {'GEMINI_API_KEY': 'test-gemini-key'}, clear=True):
            model = next((m for m in llm.get_model_registry() if m['id'] == self.model['id']), None)
            self.assertIsNotNone(model, 'Gemini 모델이 기본 레지스트리에 있어야 합니다')
            self.assertEqual(model['status'], 'active')
            self.assertEqual(llm.get_model_api_key(model), 'test-gemini-key')
            public = llm.public_model(model)
        self.assertNotIn('test-gemini-key', json.dumps(public))
        self.assertNotIn('apiKeyEnv', public)
        self.assertNotIn('endpoint', public)

    def test_gemini_is_the_default_even_when_the_company_model_is_available(self):
        with tempfile.TemporaryDirectory() as directory:
            for config_dir in [Path(directory), Path(__file__).resolve().parents[1] / 'config']:
                with self.subTest(config_dir=config_dir), patch.object(llm, 'CONFIG_DIR', config_dir), \
                        patch.dict(os.environ, {'GEMINI_API_KEY': 'test-gemini-key',
                                                'COMPANY_MODEL_46_API_KEY': 'test-company-key'}, clear=True):
                    models = llm.get_model_registry()
                    self.assertEqual(models[0]['id'], self.model['id'])
                    self.assertEqual(next(m for m in models if m['status'] == 'active')['id'], self.model['id'])

    def test_gemini_registry_is_unavailable_without_a_key(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(llm, 'CONFIG_DIR', Path(directory)), \
                patch.dict(os.environ, {}, clear=True):
            model = next((m for m in llm.get_model_registry() if m['id'] == self.model['id']), None)
            self.assertIsNotNone(model, 'Gemini 모델이 기본 레지스트리에 있어야 합니다')
            self.assertEqual(model['status'], 'missing_key')

    def test_gemini_request_keeps_json_contract_without_qwen_only_options(self):
        payload = {
            'model': self.model['id'],
            'messages': [{'role': 'user', 'content': '색상을 JSON으로 분류해 주세요.'}],
            'seed': 42, 'top_k': 20, 'min_p': 0.1, 'repetition_penalty': 1.1,
            'chat_template_kwargs': {'enable_thinking': False},
            'response_format': {'type': 'json_object'}, 'max_tokens': 1100,
        }
        original = json.dumps(payload, sort_keys=True)
        response = requests.Response()
        response.status_code = 200
        response._content_consumed = True
        with patch.object(llm.requests, 'post', return_value=response) as post:
            llm.post_model_chat(self.model, 'test-key', payload, timeout=12, think=False)
        sent = post.call_args.kwargs['json']
        self.assertEqual(sent['reasoning_effort'], 'minimal')
        self.assertEqual(sent['response_format'], {'type': 'json_object'})
        self.assertEqual(sent['messages'], payload['messages'])
        self.assertEqual(sent['max_tokens'], 1100)
        for option in ['seed', 'top_k', 'min_p', 'repetition_penalty', 'chat_template_kwargs']:
            self.assertNotIn(option, sent)
        self.assertEqual(json.dumps(payload, sort_keys=True), original)

    def test_gemini_caps_output_and_preserves_smaller_limits_for_streams(self):
        for requested, expected in [(None, 2048), (32768, 2048), (64, 64)]:
            with self.subTest(requested=requested):
                payload = {'model': self.model['id'], 'messages': [], 'stream': True}
                if requested is not None:
                    payload['max_tokens'] = requested
                response = requests.Response()
                response.status_code = 200
                response._content_consumed = True
                with patch.object(llm.requests, 'post', return_value=response) as post:
                    llm.post_model_chat(self.model, 'test-key', payload, timeout=12, stream=True)
                self.assertEqual(post.call_args.kwargs['json']['max_tokens'], expected)
                self.assertTrue(post.call_args.kwargs['stream'])
                with llm.consume_model_response(response):
                    pass

    def test_gemini_stream_without_a_charset_decodes_korean_as_utf8(self):
        from backend.services.chat_service import _read_model_response
        expected = 'Gemini 연결 정상'
        event = {'choices':[{'delta':{'content':expected}}]}
        wire = 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\ndata: [DONE]\n\n'
        response = requests.Response()
        response.status_code = 200
        response.headers['Content-Type'] = 'text/event-stream'
        response.encoding = 'ISO-8859-1'
        response._content = wire.encode('utf-8')
        response._content_consumed = True
        with patch.object(llm.requests, 'post', return_value=response):
            actual = llm.post_model_chat(self.model, 'test-key', {'model':self.model['id']},
                                         timeout=12, stream=True)
        with llm.consume_model_response(actual):
            data = _read_model_response(actual, self.model, True)
        self.assertEqual(llm.extract_model_content(data), expected)

    def test_company_thinking_option_does_not_receive_gemini_controls(self):
        model = {**self.model, 'provider': 'openai-compatible', 'think': False}
        response = requests.Response()
        response.status_code = 200
        with patch.object(llm.requests, 'post', return_value=response) as post:
            llm.post_model_chat(model, 'test-key', {'model': 'company', 'max_tokens': 4096}, timeout=12)
        sent = post.call_args.kwargs['json']
        self.assertEqual(sent['chat_template_kwargs'], {'enable_thinking': False})
        self.assertEqual(sent['max_tokens'], 4096)
        self.assertNotIn('reasoning_effort', sent)
