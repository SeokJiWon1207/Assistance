"""OpenAI 모델 등록과 Chat Completions 요청 계약을 검증한다."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

from backend import llm


class OpenAIModelTests(unittest.TestCase):
    def setUp(self):
        llm.reset_model_circuits()
        self.model = {
            "id": "gpt-6-luna",
            "model": "gpt-6-luna",
            "provider": "openai",
            "endpoint": "https://api.openai.com/v1/chat/completions",
            "reasoningEffort": "none",
            "maxOutputTokens": 2048,
        }

    def tearDown(self):
        llm.reset_model_circuits()

    def test_registered_openai_key_selects_openai_and_stays_private(self):
        with tempfile.TemporaryDirectory() as directory:
            for config_dir in (Path(directory), Path(__file__).resolve().parents[1] / "config"):
                with self.subTest(config=config_dir), patch.object(llm, "CONFIG_DIR", config_dir), patch.dict(
                    os.environ, {"OPENAI_API_KEY": "test-openai-key", "GEMINI_API_KEY": "test-gemini-key"}, clear=True,
                ):
                    model = llm.get_model_registry()[0]
                    self.assertEqual(model["id"], self.model["id"])
                    self.assertEqual(model["status"], "active")
                    self.assertEqual(llm.get_model_api_key(model), "test-openai-key")
                    public = llm.public_model(model)
                    self.assertNotIn("test-openai-key", json.dumps(public))
                    self.assertNotIn("apiKeyEnv", public)
                    self.assertNotIn("endpoint", public)

    def test_missing_openai_key_keeps_the_existing_active_default(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(llm, "CONFIG_DIR", Path(directory)), patch.dict(
            os.environ, {"GEMINI_API_KEY": "test-gemini-key"}, clear=True,
        ):
            models = llm.get_model_registry()
            self.assertEqual(models[0]["id"], "gemini-3.1-flash-lite")
            model = next(model for model in models if model["id"] == self.model["id"])
            self.assertEqual(model["status"], "missing_key")

    def test_openai_json_request_preserves_sampling_and_excludes_other_provider_options(self):
        payload = {
            "model": self.model["id"], "messages": [{"role": "user", "content": "상태를 JSON으로 분류해 줘"}],
            "temperature": 0, "top_p": 0.8, "seed": 42, "top_k": 20, "min_p": 0.1,
            "repetition_penalty": 1.1, "num_ctx": 16384, "chat_template_kwargs": {"enable_thinking": False},
            "response_format": {"type": "json_object"}, "max_tokens": 1100,
        }
        original = json.dumps(payload, sort_keys=True)
        response = requests.Response()
        response.status_code = 200
        with patch.object(llm.requests, "post", return_value=response) as post:
            self.assertIs(llm.post_model_chat(self.model, "test-key", payload, timeout=12, think=False), response)
        self.assertEqual(post.call_args.args, (self.model["endpoint"],))
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer test-key")
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["reasoning_effort"], "none")
        self.assertEqual(sent["temperature"], 0)
        self.assertEqual(sent["top_p"], 0.8)
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertEqual(sent["messages"], payload["messages"])
        self.assertEqual(sent["max_completion_tokens"], 1100)
        for option in ("max_tokens", "seed", "top_k", "min_p", "repetition_penalty", "num_ctx", "chat_template_kwargs"):
            self.assertNotIn(option, sent)
        self.assertEqual(json.dumps(payload, sort_keys=True), original)

    def test_openai_caps_completion_tokens_and_keeps_smaller_limits(self):
        for requested, expected in ((None, 2048), (32768, 2048), (64, 64)):
            with self.subTest(requested=requested):
                payload = {"model": self.model["id"], "messages": []}
                if requested is not None:
                    payload["max_tokens"] = requested
                with patch.object(llm.requests, "post", return_value=requests.Response()) as post:
                    llm.post_model_chat(self.model, "test-key", payload, timeout=12)
                self.assertEqual(post.call_args.kwargs["json"]["max_completion_tokens"], expected)

    def test_reasoning_requests_remove_incompatible_sampling_options(self):
        payload = {"model": self.model["id"], "temperature": 0, "top_p": 0.8, "top_logprobs": 2, "logprobs": True}
        with patch.object(llm.requests, "post", return_value=requests.Response()) as post:
            llm.post_model_chat({**self.model, "reasoningEffort": "low"}, "test-key", payload, timeout=12)
        sent = post.call_args.kwargs["json"]
        self.assertEqual(sent["reasoning_effort"], "low")
        for option in ("temperature", "top_p", "top_logprobs", "logprobs"):
            self.assertNotIn(option, sent)

    def test_openai_stream_preserves_korean_text(self):
        from backend.services.chat_service import _read_model_response

        expected = "OpenAI 연결 정상"
        event = {"choices": [{"delta": {"content": expected}}]}
        wire = "data: " + json.dumps(event, ensure_ascii=False) + "\n\ndata: [DONE]\n\n"
        response = requests.Response()
        response.status_code = 200
        response.headers["Content-Type"] = "text/event-stream"
        response.encoding = "ISO-8859-1"
        response._content = wire.encode("utf-8")
        response._content_consumed = True
        with patch.object(llm.requests, "post", return_value=response):
            actual = llm.post_model_chat(self.model, "test-key", {"model": self.model["id"]}, timeout=12, stream=True)
        with llm.consume_model_response(actual):
            data = _read_model_response(actual, self.model, True)
        self.assertEqual(llm.extract_model_content(data), expected)
