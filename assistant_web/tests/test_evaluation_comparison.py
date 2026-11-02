"""모델 경계만 대체하고 실제 근거 구성·요청·응답 처리를 검증한다."""

import unittest
from unittest.mock import Mock, patch

import requests

from backend.evaluation import comparison
from backend.schemas import ChatRequest


class EvaluationComparisonTests(unittest.TestCase):
    def setUp(self):
        self.request = ChatRequest(
            modelId="fixture-model", maxTokens=2000,
            messages=[{"role": "user", "content": "이슈 조회"}],
        )
        self.retrieval = {
            "query": "이슈 조회", "constraints": {},
            "stages": [{"source": "mantis_page_scan", "status": "ok", "issues_list": [{
                "id": 31585, "summary": "회귀 검증용 제목", "handler": "테스트담당",
                "project": "테스트 프로젝트", "status": "confirmed",
            }]}],
        }
        self.model = {"id": "fixture-model", "model": "fixture-model", "provider": "ollama"}

    def generate(self, variant):
        if variant == "baseline":
            return comparison.generate_basic_grounded_baseline(self.request, "이슈 조회", self.retrieval)
        return comparison.generate_candidate_model_answer(
            self.request, "이슈 조회", self.retrieval, "fixture-model",
        )

    def test_both_variants_send_grounded_payload_and_return_model_sources(self):
        for variant, marker in (("baseline", "기본 검색 결과"), ("candidate", "검증된 검색 결과")):
            with self.subTest(variant=variant):
                response = Mock(status_code=200)
                response.json.return_value = {"choices": [{"message": {"content": "#31585 회귀 검증용 제목"}}]}
                with patch.object(comparison, "get_private_model", return_value=self.model) as get_model, \
                        patch.object(comparison, "get_model_api_key", return_value="fixture-key"), \
                        patch.object(comparison, "post_model_chat", return_value=response) as post:
                    result = self.generate(variant)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["content"], "#31585 회귀 검증용 제목")
                self.assertEqual(result["sourceCount"], 1)
                self.assertEqual([source["issue_id"] for source in result["sources"]], [31585])
                get_model.assert_called_once_with("fixture-model")
                post.assert_called_once()
                model, key, payload = post.call_args.args
                self.assertEqual((model, key), (self.model, "fixture-key"))
                self.assertIn(marker, payload["messages"][0]["content"])
                self.assertIn('"id": 31585', payload["messages"][0]["content"])
                self.assertEqual(payload["messages"][1], {"role": "user", "content": "이슈 조회"})
                self.assertEqual(payload["max_tokens"], 1200)
                self.assertFalse(payload["stream"])
                self.assertFalse(post.call_args.kwargs["think"])

    def test_both_variants_preserve_model_failures_without_retry(self):
        for variant in ("baseline", "candidate"):
            for failure, marker in ((503, "HTTP 503"), (requests.ReadTimeout(), "시간이 초과"),
                                    (requests.ConnectionError(), "ConnectionError")):
                with self.subTest(variant=variant, failure=type(failure).__name__):
                    post = Mock(return_value=Mock(status_code=failure)) if isinstance(failure, int) else Mock(side_effect=failure)
                    with patch.object(comparison, "get_private_model", return_value=self.model), \
                            patch.object(comparison, "get_model_api_key", return_value="fixture-key"), \
                            patch.object(comparison, "post_model_chat", post):
                        result = self.generate(variant)
                    self.assertEqual(result["status"], "error")
                    self.assertIn(marker, result["content"])
                    post.assert_called_once()
