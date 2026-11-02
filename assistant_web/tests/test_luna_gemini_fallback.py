"""Luna 장애 시 Gemini로 질문 해석과 답변 생성을 이어가는 계약."""

import backend.services.chat.generate as chat_generate
import backend.llm as chat_llm
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException

from backend import llm
from backend.execution import RequestStopped
from backend.intent import unified
from backend.schemas import ChatRequest
from backend.services import chat_service
from test_stream_fallback import stream_response
from test_unified_accuracy_failures import NO_STATUS, USER, response
from test_unified_planner import proposal


LUNA = {
    "id": "gpt-6-luna", "model": "gpt-6-luna", "provider": "openai", "status": "active",
    "endpoint": "https://api.openai.com/v1/chat/completions", "reasoningEffort": "none",
    "fallbackModelIds": ["gemini-3.1-flash-lite"],
}
GEMINI = {
    "id": "gemini-3.1-flash-lite", "model": "gemini-3.1-flash-lite", "provider": "gemini", "status": "active",
    "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
    "reasoningEffort": "minimal",
}
OTHER = {"id": "other", "model": "other", "provider": "ollama", "status": "active"}


class LunaGeminiFallbackTests(unittest.TestCase):
    def setUp(self):
        llm.reset_model_circuits()
        self.addCleanup(llm.reset_model_circuits)
        self.registry = [LUNA, OTHER, GEMINI]
        self.enterContext(patch.object(llm, "get_model_registry", return_value=self.registry))
        self.enterContext(patch.object(chat_generate, "get_model_registry", return_value=self.registry))
        self.enterContext(patch.object(llm, "get_model_api_key", return_value="test-key"))
        self.enterContext(patch.object(chat_llm, "get_model_api_key", return_value="test-key"))
        self.enterContext(patch.object(chat_service.CHAT_STREAM_CONTEXT, "content_streamed", False, create=True))
        self.enterContext(patch.object(chat_generate, "emit_chat_event"))
        self.enterContext(patch.object(chat_generate, "emit_chat_status"))
        self.parsed = proposal(source="notion", operation="search", scope="all", status=NO_STATUS,
                               filters=[{'field': 'customer', 'operator': 'equals', 'value': '신한', 'evidence': '신한'}])

    def plan(self):
        return unified.plan_question("노션에서 신한 관련 문서 찾아줘", LUNA, "test-key", USER, {})

    def generate(self):
        request = ChatRequest(modelId=LUNA["id"], messages=[{"role": "user", "content": "안녕"}], stream=True)
        return chat_service.generate_model_response(request, LUNA, [], None, {}, 5)

    def test_luna_registry_declares_gemini_as_the_fallback(self):
        # 일반 테스트의 registry patch를 해제하고 실제/안전한 기본 설정을 모두 확인한다.
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            for config in (Path(directory), Path(__file__).resolve().parents[1] / "config"):
                with self.subTest(config=config), patch.object(llm, "CONFIG_DIR", config):
                    raw = llm._default_model_registry() if config == Path(directory) else json.loads((config / "models.json").read_text())
                    model = next(model for model in raw if model["id"] == LUNA["id"])
                    self.assertEqual(model.get("fallbackModelIds"), [GEMINI["id"]])

    def test_generation_uses_gemini_next_even_if_registry_order_changes(self):
        with patch.object(llm.requests, "post", side_effect=[requests.Timeout(), stream_response("Gemini 대체 답변")]) as post:
            result = self.generate()
        self.assertEqual([call.args[0] for call in post.call_args_list], [LUNA["endpoint"], GEMINI["endpoint"]])
        self.assertEqual(result["modelId"], GEMINI["id"])
        self.assertEqual(result["usedFallbackModel"], GEMINI["id"])
        self.assertEqual(result["content"], "Gemini 대체 답변")
        self.assertEqual(post.call_args_list[1].kwargs["json"]["reasoning_effort"], "minimal")

    def test_generation_does_not_use_other_models_if_both_fail(self):
        with patch.object(chat_generate, "post_model_chat", side_effect=requests.ConnectionError()) as send:
            with self.assertRaises(HTTPException) as raised:
                self.generate()
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual([call.args[0]["id"] for call in send.call_args_list], [LUNA["id"], GEMINI["id"]])

    def test_generation_http_errors_switch_to_gemini(self):
        for status in (401, 429, 500, 503):
            with self.subTest(status=status):
                failed = Mock(status_code=status, text="unavailable")
                with patch.object(llm.requests, "post", side_effect=[failed, stream_response("Gemini 답변")]) as post:
                    result = self.generate()
                self.assertEqual(result["modelId"], GEMINI["id"])
                self.assertEqual([call.args[0] for call in post.call_args_list], [LUNA["endpoint"], GEMINI["endpoint"]])

    def test_primary_success_never_calls_gemini(self):
        with patch.object(llm.requests, "post", return_value=stream_response("Luna 답변")) as send:
            result = self.generate()
        self.assertEqual(result["modelId"], LUNA["id"])
        self.assertIsNone(result["usedFallbackModel"])
        send.assert_called_once()

    def test_planning_transport_and_http_errors_switch_to_gemini(self):
        errors = [requests.Timeout(), requests.ConnectionError(), llm.ModelCircuitOpenError()]
        for status in (400, 401, 402, 403, 429, 500, 502, 503, 504):
            failed = requests.Response(); failed.status_code = status
            errors.append(requests.HTTPError(response=failed))
        for error in errors:
            with self.subTest(error=type(error).__name__, status=getattr(error.response, "status_code", None)):
                with patch.object(unified, "post_model_chat", side_effect=[error, response(self.parsed)]) as send:
                    result = self.plan()
                self.assertEqual([call.args[0]["id"] for call in send.call_args_list], [LUNA["id"], GEMINI["id"]])
                self.assertEqual(result["planner_model_id"], GEMINI["id"])
                self.assertEqual(result["planner_fallback_from"], LUNA["id"])
                self.assertEqual(result["terms"], [])
                self.assertEqual(result['resolved_constraints']['customer_key'], '신한')

    def test_empty_planner_reply_switches_to_gemini(self):
        empty = Mock(status_code=200)
        empty.json.return_value = {"choices": [{"message": {"content": ""}}]}
        with patch.object(unified, "post_model_chat", side_effect=[empty, response(self.parsed)]) as send:
            result = self.plan()
        self.assertEqual(result["planner_model_id"], GEMINI["id"])
        self.assertEqual(send.call_count, 2)

    def test_missing_primary_key_uses_configured_gemini(self):
        with patch.object(unified, "post_model_chat", return_value=response(self.parsed)) as send:
            result = unified.plan_question("노션에서 신한 관련 문서 찾아줘", LUNA, "", USER, {})
        self.assertEqual(result["planner_model_id"], GEMINI["id"])
        self.assertEqual(send.call_args.args[0]["id"], GEMINI["id"])
        send.assert_called_once()

    def test_gemini_fallback_keeps_title_and_status_exclusion(self):
        fixture = Path(__file__).parent / "fixtures" / "routing_cases.jsonl"
        case = next(row for row in map(json.loads, fixture.read_text().splitlines()) if row.get("id") == "luna-gemini-exclusion")
        valid = case["unified"]
        invalid = copy.deepcopy(valid)
        invalid["filters"][0].update(field="keyword", operator="contains")
        with patch.object(unified, "post_model_chat", side_effect=[requests.Timeout(), response(invalid), response(valid)]) as send:
            result = unified.plan_question(case["q"], LUNA, "test-key", USER, {})
        self.assertEqual([call.args[0]["id"] for call in send.call_args_list], [case["model_fallback"]["from"], case["model_fallback"]["to"], case["model_fallback"]["to"]])
        self.assertEqual(result["filter_clauses"], [{key: clause[key] for key in ("field", "operator", "value")} for clause in valid["filters"]])

    def test_explicit_fallback_list_deduplicates_and_cannot_loop_to_primary(self):
        model = {**LUNA, "fallbackModelIds": [LUNA["id"], GEMINI["id"], GEMINI["id"], "missing"]}
        self.assertEqual(llm.get_fallback_models(model, self.registry), [GEMINI])
        for value in ([], "other", None):
            with self.subTest(value=value):
                self.assertEqual(llm.get_fallback_models({**LUNA, "fallbackModelIds": value}, self.registry), [])

    def test_unavailable_gemini_never_uses_unconfigured_models(self):
        for status in ("disabled", "expired", "missing_key"):
            with self.subTest(status=status), patch.object(llm, "get_model_registry", return_value=[LUNA, OTHER, {**GEMINI, "status": status}]), patch.object(unified, "post_model_chat", side_effect=requests.Timeout()) as send:
                result = self.plan()
            self.assertEqual(result["operation"], "clarify")
            self.assertTrue(all(call.args[0]["id"] == LUNA["id"] for call in send.call_args_list))

    def test_cancellation_never_switches_models(self):
        with patch.object(unified, "post_model_chat", side_effect=RequestStopped(cancelled=True)) as send:
            with self.assertRaises(RequestStopped):
                self.plan()
        send.assert_called_once()
