"""모델 헤더 이후 스트림 장애와 부분 답변 폴백을 네트워크 없이 검증한다."""

import os
import json
import unittest
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException

from backend import llm
from backend.schemas import ChatRequest
from backend.services import chat_service


PRIMARY = {
    "id": "primary", "model": "primary", "status": "active",
    "provider": "openai-compatible", "endpoint": "https://primary.example/chat",
}
SECONDARY = {**PRIMARY, "id": "secondary", "endpoint": "https://secondary.example/chat"}


def stream_response(content="대체 답변", *, error=None, prefix=""):
    response = Mock(status_code=200, headers={"content-type": "text/event-stream"})

    def lines(**_kwargs):
        if prefix:
            yield 'data: {"choices":[{"delta":{"content":"' + prefix + '"}}]}'
        if error is not None:
            raise error
        yield ('data: {"choices":[{"delta":{"content":"' + content + '"}}]}').encode()
        yield b"data: [DONE]"

    response.iter_lines.side_effect = lines
    return response


class StreamFallbackTests(unittest.TestCase):
    def setUp(self):
        llm.reset_model_circuits()
        self.addCleanup(llm.reset_model_circuits)
        self.events = []
        patches = [
            patch.object(chat_service, "get_model_registry", return_value=[PRIMARY, SECONDARY]),
            patch.object(chat_service, "get_model_api_key", return_value="test-key"),
            patch.object(chat_service.CHAT_STREAM_CONTEXT, "emitter", self.events.append, create=True),
            patch.object(chat_service.CHAT_STREAM_CONTEXT, "content_streamed", False, create=True),
            patch.dict(os.environ, {
                "MODEL_TIMEOUT_SECONDS": "180", "MODEL_STREAM_READ_TIMEOUT_SECONDS": "60",
                "MODEL_FIRST_CONTENT_TIMEOUT_SECONDS": "60",
                "MODEL_CIRCUIT_FAILURE_THRESHOLD": "2",
            }),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        self.request = ChatRequest(modelId="primary", messages=[{"role": "user", "content": "질문"}], stream=True)

    def generate(self, retrieval=None):
        return chat_service.generate_model_response(self.request, PRIMARY, [], retrieval, {}, 5)

    def test_body_read_failure_uses_next_model_and_records_circuit_failure(self):
        for error_type in (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
            with self.subTest(error=error_type.__name__):
                llm.reset_model_circuits()
                failed = stream_response(error=error_type("본문 읽기 실패"))
                successful = stream_response()
                with patch.object(llm.requests, "post", side_effect=[failed, successful]) as post:
                    result = self.generate()
                self.assertEqual(result["modelId"], "secondary")
                self.assertEqual(result["usedFallbackModel"], "secondary")
                self.assertEqual(result["content"], "대체 답변")
                self.assertEqual(post.call_count, 2)
                self.assertEqual(llm.model_circuit_status_for(PRIMARY)["consecutiveFailures"], 1)
                failed.close.assert_called_once()
                successful.close.assert_called_once()

    def test_partial_and_secondary_answers_remain_buffered_until_verification(self):
        failed = stream_response(prefix="폐기할 부분 답변", error=requests.ConnectionError("중단"))
        with patch.object(llm.requests, "post", side_effect=[failed, stream_response()]):
            result = self.generate()
        content_events = [event for event in self.events if event["type"] in {"delta", "reset"}]
        self.assertEqual(content_events, [])
        self.assertEqual(result["content"], "대체 답변")

    def test_exhausted_body_failures_buffer_grounded_fallback(self):
        failed = stream_response(prefix="폐기", error=requests.ConnectionError("본문 중단"))
        retrieval = {"issues": [{"id": 12345}]}
        with (
            patch.object(chat_service, "get_model_registry", return_value=[]),
            patch.object(llm.requests, "post", return_value=failed),
            patch.object(chat_service, "build_sources", return_value=[{"issue_id": 12345}]),
            patch.object(chat_service, "format_model_timeout_fallback", return_value="확인한 근거 답변"),
        ):
            result = self.generate(retrieval)
        self.assertEqual(result["raw"]["mode"], "model_timeout_fallback")
        self.assertEqual(result["raw"]["errorType"], "ConnectionError")
        self.assertEqual(result["content"], "확인한 근거 답변")
        self.assertEqual(result["sources"], [{"issue_id": 12345}])
        self.assertFalse(any(event["type"] in {"reset", "delta"} for event in self.events))
        # 최종 조립이 근거 답변의 delta를 다시 전달할 수 있어야 한다.
        self.assertFalse(chat_service.CHAT_STREAM_CONTEXT.content_streamed)
        failed.close.assert_called_once()

    def test_exhausted_body_failure_without_evidence_returns_controlled_502(self):
        failed = stream_response(error=requests.ConnectionError("본문 중단"))
        with (
            patch.object(chat_service, "get_model_registry", return_value=[]),
            patch.object(llm.requests, "post", return_value=failed),
            self.assertRaises(HTTPException) as raised,
        ):
            self.generate()
        self.assertEqual(raised.exception.status_code, 502)
        failed.close.assert_called_once()

    def test_empty_stream_does_not_mask_failure_as_success(self):
        empty = Mock(status_code=200, headers={"content-type": "text/event-stream"})
        empty.iter_lines.return_value = iter(['data: {"choices":[{"delta":{"role":"assistant"}}]}', 'data: [DONE]'])
        with patch.object(llm.requests, "post", side_effect=[empty, stream_response()]):
            result = self.generate()
        self.assertEqual(result["modelId"], "secondary")
        self.assertEqual(llm.model_circuit_status_for(PRIMARY)["consecutiveFailures"], 1)
        empty.close.assert_called_once()

    def test_usage_only_and_malformed_chunks_do_not_drop_valid_answer(self):
        malformed = [None, [], 1, {"choices": []}, {"choices": "invalid"},
                     {"choices": [None]}, {"choices": [{"delta": None}]},
                     {"choices": [{"delta": {"content": ["invalid"]}}]}]
        chunks = [*malformed, {"choices": [{"delta": {"content": "정상 본문"}}]},
                  {"choices": [], "usage": {"total_tokens": 10}}]
        response = Mock(status_code=200, headers={"content-type": "text/event-stream"})
        response.iter_lines.return_value = iter([*("data: " + json.dumps(chunk) for chunk in chunks), "data: [DONE]"])
        with patch.object(llm.requests, "post", return_value=response) as post:
            result = self.generate()
        self.assertEqual(result["content"], "정상 본문")
        self.assertEqual(post.call_count, 1)
        response.close.assert_called_once()

    def test_ollama_ignores_malformed_message_and_nontext_content(self):
        chunks = [None, [], {"message": None}, {"message": []},
                  {"message": {"content": 42}}, {"message": {"content": "정상 본문"}}]
        response = Mock(status_code=200)
        response.iter_lines.return_value = iter(json.dumps(chunk).encode() for chunk in chunks)
        data = chat_service._read_model_response(response, {**PRIMARY, "provider": "ollama"}, True)
        self.assertEqual(llm.extract_model_content(data), "정상 본문")

    def test_nonstream_content_extraction_handles_empty_choices(self):
        for data in ({"choices": []}, {"choices": [None]}, {"choices": [{"message": None}]},
                     {"choices": [{"message": {"content": []}}]}, {"message": None}):
            with self.subTest(data=data):
                self.assertEqual(llm.extract_model_content(data), "")
        self.assertEqual(llm.extract_model_content({"choices": [], "message": {"content": "응답"}}), "응답")

    def test_invalid_json_body_falls_back_and_closes_the_failed_response(self):
        invalid = Mock(status_code=200, headers={"content-type": "application/json"})
        invalid.json.side_effect = requests.exceptions.JSONDecodeError("invalid", "x", 0)
        with patch.object(llm.requests, "post", side_effect=[invalid, stream_response()]):
            result = self.generate()
        self.assertEqual(result["modelId"], "secondary")
        invalid.close.assert_called_once()

    def test_failed_http_response_and_successful_nonstream_response_are_closed(self):
        self.request = self.request.model_copy(update={"stream": False})
        failure = Mock(status_code=503, text="unavailable")
        success = Mock(status_code=200)
        success.json.return_value = {"choices": [{"message": {"content": "응답"}}]}
        with patch.object(llm.requests, "post", side_effect=[failure, success]) as post:
            result = self.generate()
        self.assertEqual(result["content"], "응답")
        self.assertEqual(post.call_args.kwargs["timeout"], 180)
        failure.close.assert_called_once()
        success.close.assert_called_once()

    def test_stream_read_timeout_is_separate_and_respects_lower_existing_limit(self):
        for configured, expected in (("180", 60), ("30", 30)):
            with self.subTest(model_timeout=configured):
                with (
                    patch.dict(os.environ, {"MODEL_TIMEOUT_SECONDS": configured}),
                    patch.object(llm.requests, "post", return_value=stream_response()) as post,
                ):
                    self.generate()
                self.assertEqual(post.call_args.kwargs["timeout"], expected)

    def test_reasoning_only_chunks_hit_first_content_deadline(self):
        response = Mock(status_code=200, headers={"content-type": "text/event-stream"})
        response.iter_lines.return_value = iter([
            'data: {"choices":[{"delta":{"role":"assistant"}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"추론"}}]}',
        ])
        with patch.object(chat_service, "perf_counter", side_effect=[59.0, 60.0]):
            with self.assertRaises(requests.Timeout):
                chat_service._read_model_response(response, PRIMARY, True, first_content_deadline=60.0)
        self.assertFalse(any(event["type"] == "delta" for event in self.events))

    def test_first_content_deadline_does_not_limit_ongoing_answer(self):
        response = stream_response(content="마지막", prefix="처음")
        with patch.object(chat_service, "perf_counter", return_value=59.0) as clock:
            data = chat_service._read_model_response(response, PRIMARY, True, first_content_deadline=60.0)
        self.assertEqual(data["choices"][0]["message"]["content"], "처음마지막")
        # 첫 내용 이후에는 deadline 시계를 더 검사하지 않는다.
        clock.assert_called_once()

    def test_whitespace_deltas_do_not_disable_first_content_deadline(self):
        response = Mock(status_code=200, headers={"content-type": "text/event-stream"})
        response.iter_lines.return_value = iter([
            'data: {"choices":[{"delta":{"content":" "}}]}',
            'data: {"choices":[{"delta":{"reasoning_content":"추론"}}]}',
        ])
        with patch.object(chat_service, "perf_counter", side_effect=[59.0, 60.0]):
            with self.assertRaises(requests.Timeout):
                chat_service._read_model_response(response, PRIMARY, True, first_content_deadline=60.0)
        self.assertEqual(self.events, [])


if __name__ == "__main__":
    unittest.main()
