import os
import unittest
from unittest.mock import Mock, patch
from threading import Event, Thread

import requests
from fastapi import HTTPException

from backend import llm
from backend.intent import constraints
from backend.schemas import ChatRequest
from backend.services import chat_service


MODEL = {
    "id": "test-model",
    "label": "Test",
    "model": "test-model",
    "endpoint": "https://model.example/v1/chat/completions",
}


class ModelCircuitTests(unittest.TestCase):
    def setUp(self):
        llm.reset_model_circuits()
        self.env = patch.dict(
            os.environ,
            {"MODEL_CIRCUIT_FAILURE_THRESHOLD": "2", "MODEL_CIRCUIT_COOLDOWN_SECONDS": "60"},
        )
        self.env.start()
        self.clock = 1000.0
        self.time = patch.object(llm.time, "monotonic", side_effect=lambda: self.clock)
        self.time.start()
        self.logger = patch.object(llm, "LOGGER")
        self.log = self.logger.start()

    def tearDown(self):
        self.logger.stop()
        self.time.stop()
        self.env.stop()
        llm.reset_model_circuits()

    def call(self):
        return llm.post_model_chat(MODEL, "key", {"messages": []}, timeout=8)

    def test_consecutive_timeouts_open_circuit_without_network_wait(self):
        with patch.object(llm.requests, "post", side_effect=requests.Timeout("slow")) as post:
            for _ in range(2):
                with self.assertRaises(requests.Timeout):
                    self.call()
            with self.assertRaises(llm.ModelCircuitOpenError) as blocked:
                self.call()

        self.assertEqual(post.call_count, 2)
        # 호출부의 기존 RequestException 폴백이 그대로 적용되어야 한다.
        self.assertIsInstance(blocked.exception, requests.ConnectionError)
        self.assertIn("60초", str(blocked.exception))
        self.log.warning.assert_called_once()

    def test_single_failure_followed_by_success_does_not_open(self):
        success = Mock(status_code=200)
        with patch.object(
            llm.requests, "post", side_effect=[requests.Timeout("slow"), success, requests.Timeout("slow"), success],
        ) as post:
            with self.assertRaises(requests.Timeout):
                self.call()
            self.assertIs(self.call(), success)
            with self.assertRaises(requests.Timeout):
                self.call()
            self.assertIs(self.call(), success)

        self.assertEqual(post.call_count, 4)

    def test_gateway_errors_count_as_unavailable_but_client_errors_do_not(self):
        with patch.object(llm.requests, "post", side_effect=[Mock(status_code=400), Mock(status_code=429)]):
            self.call()
            self.call()
        self.assertEqual(llm.model_circuit_status_for(MODEL)["circuit"], "closed")

        with patch.object(llm.requests, "post", side_effect=[Mock(status_code=503), Mock(status_code=504)]):
            self.call()
            self.call()
        self.assertEqual(llm.model_circuit_status_for(MODEL)["circuit"], "open")

    def test_half_open_allows_one_probe_and_success_closes(self):
        with patch.object(llm.requests, "post", side_effect=requests.Timeout("slow")):
            for _ in range(2):
                with self.assertRaises(requests.Timeout):
                    self.call()

        self.clock += 61
        success = Mock(status_code=200)
        with patch.object(llm.requests, "post", return_value=success) as post:
            self.assertIs(self.call(), success)
            self.assertIs(self.call(), success)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(llm.model_circuit_status_for(MODEL)["consecutiveFailures"], 0)

    def test_failed_probe_reopens_and_blocks_concurrent_callers(self):
        with patch.object(llm.requests, "post", side_effect=requests.Timeout("slow")):
            for _ in range(2):
                with self.assertRaises(requests.Timeout):
                    self.call()

        self.clock += 61
        with patch.object(llm.requests, "post", side_effect=requests.ConnectionError("down")) as post:
            with self.assertRaises(requests.ConnectionError):
                self.call()
            self.clock += 30
            with self.assertRaises(llm.ModelCircuitOpenError):
                self.call()
        self.assertEqual(post.call_count, 1)

    def test_status_reports_model_ids_without_endpoint(self):
        with patch.object(llm, "get_model_registry", return_value=[MODEL]):
            with patch.object(llm.requests, "post", side_effect=requests.Timeout("slow")):
                for _ in range(2):
                    with self.assertRaises(requests.Timeout):
                        self.call()
            status = llm.model_circuit_status()

        self.assertEqual(
            status,
            [{"id": "test-model", "circuit": "open", "consecutiveFailures": 2, "retryAfterSeconds": 60}],
        )
        self.assertNotIn("model.example", str(status))

    def open_circuit(self):
        with patch.object(llm.requests, "post", side_effect=requests.Timeout("slow")):
            for _ in range(2):
                with self.assertRaises(requests.Timeout):
                    self.call()

    def test_probe_stays_exclusive_until_slow_request_finishes(self):
        self.open_circuit()
        self.clock += 61
        entered, release = Event(), Event()
        outcomes = []

        def slow_post(*_args, **_kwargs):
            entered.set()
            if not release.wait(5):
                raise AssertionError("복구 확인 요청 종료 신호 없음")
            return Mock(status_code=200)

        def probe():
            try:
                outcomes.append(self.call())
            except BaseException as exc:
                outcomes.append(exc)

        with patch.object(llm.requests, "post", side_effect=slow_post) as post:
            worker = Thread(target=probe)
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                self.clock += 181
                with self.assertRaises(llm.ModelCircuitOpenError):
                    self.call()
                self.assertEqual(post.call_count, 1)
            finally:
                release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcomes), 1)
        self.assertNotIsInstance(outcomes[0], BaseException)
        self.assertEqual(llm.model_circuit_status_for(MODEL)["circuit"], "closed")

    def test_unexpected_probe_error_releases_probe_without_counting_failure(self):
        self.open_circuit()
        self.clock += 61
        with patch.object(llm.requests, "post", side_effect=ValueError("bad request")):
            with self.assertRaises(ValueError):
                self.call()
        self.assertEqual(llm.model_circuit_status_for(MODEL)["consecutiveFailures"], 2)
        self.clock += 61
        with patch.object(llm.requests, "post", return_value=Mock(status_code=200)):
            self.call()
        self.assertEqual(llm.model_circuit_status_for(MODEL)["circuit"], "closed")

    def test_late_success_from_before_open_does_not_release_new_probe(self):
        key = llm._model_circuit_key(MODEL)
        old_call = llm._enter_model_circuit(key)
        self.open_circuit()
        self.clock += 61
        probe_call = llm._enter_model_circuit(key)
        llm._record_model_result(old_call, available=True)
        with self.assertRaises(llm.ModelCircuitOpenError):
            self.call()
        llm._record_model_result(probe_call, available=True)
        self.assertEqual(llm.model_circuit_status_for(MODEL)["circuit"], "closed")

    def generate_stream(self, model):
        request = ChatRequest(
            modelId=model["id"], messages=[{"role": "user", "content": "답변해줘"}], stream=True,
        )
        with (
            patch.object(chat_service, "get_model_registry", return_value=[]),
            patch.object(chat_service, "get_model_api_key", return_value="key"),
            patch.object(chat_service, "emit_chat_event"),
            patch.object(chat_service, "emit_chat_status"),
            patch.object(chat_service.CHAT_STREAM_CONTEXT, "content_streamed", False, create=True),
        ):
            return chat_service.generate_model_response(request, model, [], None, {}, 5)

    def test_stream_read_errors_open_circuit_through_answer_generation(self):
        for provider in ("openai-compatible", "ollama"):
            for error in (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
                with self.subTest(provider=provider, error=error.__name__):
                    llm.reset_model_circuits()
                    model = {**MODEL, "provider": provider}
                    response = Mock(status_code=200, headers={"content-type": "text/event-stream"})
                    response.iter_lines.side_effect = error("본문 읽기 실패")
                    with patch.object(llm.requests, "post", return_value=response) as post:
                        for _ in range(2):
                            with self.assertRaises(HTTPException) as raised:
                                self.generate_stream(model)
                            self.assertEqual(raised.exception.status_code, 502)
                        with self.assertRaises(llm.ModelCircuitOpenError):
                            llm.post_model_chat(model, "key", {}, timeout=8, stream=True)
                    self.assertEqual(post.call_count, 2)
                    self.assertEqual(response.close.call_count, 2)
                    self.assertEqual(llm.model_circuit_status_for(model)["consecutiveFailures"], 2)

    def test_stream_probe_succeeds_only_after_body_is_consumed(self):
        self.open_circuit()
        self.clock += 61
        response = Mock(status_code=200, headers={"content-type": "text/event-stream"})

        def lines(**_kwargs):
            self.assertEqual(llm.model_circuit_status_for(MODEL)["consecutiveFailures"], 2)
            self.clock += 181
            with self.assertRaises(llm.ModelCircuitOpenError):
                self.call()
            yield 'data: {"choices":[{"delta":{"content":"정상 응답"}}]}'
            yield "data: [DONE]"

        response.iter_lines.side_effect = lines
        with patch.object(llm.requests, "post", return_value=response) as post:
            result = self.generate_stream(MODEL)
        self.assertEqual(result["content"], "정상 응답")
        self.assertEqual(post.call_count, 1)
        response.close.assert_called_once()
        self.assertEqual(llm.model_circuit_status_for(MODEL)["circuit"], "closed")

    def test_stream_probe_failure_reopens_from_body_failure_time(self):
        self.open_circuit()
        self.clock += 61
        response = Mock(status_code=200, headers={"content-type": "text/event-stream"})

        def lines(**_kwargs):
            yield 'data: {"choices":[{"delta":{"content":"일부 응답"}}]}'
            self.clock += 181
            raise requests.ConnectionError("본문 중단")

        response.iter_lines.side_effect = lines
        with patch.object(llm.requests, "post", return_value=response):
            with self.assertRaises(HTTPException) as raised:
                self.generate_stream(MODEL)
            self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(llm.model_circuit_status_for(MODEL)["retryAfterSeconds"], 60)
        self.assertEqual(llm.model_circuit_status_for(MODEL)["consecutiveFailures"], 3)
        response.close.assert_called_once()

    def test_stream_json_response_and_cancelled_probe_release_resources(self):
        self.open_circuit()
        self.clock += 61
        response = Mock(status_code=200, headers={"content-type": "application/json"})
        response.json.side_effect = ValueError("잘못된 JSON")
        with patch.object(llm.requests, "post", return_value=response):
            with self.assertRaises(ValueError):
                self.generate_stream(MODEL)
        response.close.assert_called_once()
        self.clock += 61
        response.json.side_effect = None
        response.json.return_value = {"choices": [{"message": {"content": "복구"}}]}
        with patch.object(llm.requests, "post", return_value=response):
            self.assertEqual(self.generate_stream(MODEL)["content"], "복구")
        self.assertEqual(llm.model_circuit_status_for(MODEL)["circuit"], "closed")

    def test_query_planner_falls_back_immediately_while_circuit_is_open(self):
        with patch.object(llm.requests, "post", side_effect=requests.Timeout("slow")):
            for _ in range(2):
                with self.assertRaises(requests.Timeout):
                    self.call()

        with patch.object(llm.requests, "post") as post:
            plan = constraints.plan_search_intent_with_model("노션에서 휴가 공지 찾아줘", MODEL, "key")

        post.assert_not_called()
        self.assertFalse(plan["planned"])
        self.assertEqual(plan["reason"], "planner_fallback")

    def test_query_planner_timeout_defaults_to_eight_seconds(self):
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"message": {"content": '{"source":"notion"}'}}]}
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("INTENT_PLANNER_TIMEOUT_SECONDS", None)
            with patch.object(constraints, "post_model_chat", return_value=response) as post:
                constraints.plan_search_intent_with_model("노션에서 휴가 공지 찾아줘", MODEL, "key")

        self.assertEqual(post.call_args.kwargs["timeout"], 8)


if __name__ == "__main__":
    unittest.main()
