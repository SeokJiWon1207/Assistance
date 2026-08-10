import gc
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from backend import auth, llm
from backend.schemas import ChatRequest
from backend.services import chat_service as chat_service_module


class RuntimeResilienceTests(unittest.TestCase):
    def test_malformed_model_registry_uses_safe_defaults(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_directory = Path(temporary_directory)
            (config_directory / "models.json").write_text("{broken", encoding="utf-8")
            with patch.object(llm, "CONFIG_DIR", config_directory), patch.dict(
                os.environ,
                {"COMPANY_MODEL_46_ENDPOINT": ""},
            ):
                models = llm.get_model_registry()

        self.assertEqual(
            [model["id"] for model in models],
            ["company-qwen3.6-35b-a3b", "qwen3.5-9b-local"],
        )
        self.assertNotIn("59.6.135.38", str(models))

    def test_model_expiry_environment_overrides_registry_fallback(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            config_directory = Path(temporary_directory)
            (config_directory / "models.json").write_text(
                '[{"id":"company","label":"Company","provider":"openai-compatible",'
                '"endpoint":"https://model.example/v1/chat","apiKeyEnv":"TEST_KEY",'
                '"expiresAt":"2026-12-31","expiresAtEnv":"TEST_MODEL_EXPIRES_AT",'
                '"enabled":true}]',
                encoding="utf-8",
            )
            with patch.object(llm, "CONFIG_DIR", config_directory), patch.dict(
                os.environ,
                {"TEST_KEY": "secret", "TEST_MODEL_EXPIRES_AT": "2027-06-30"},
            ):
                model = llm.get_model_registry()[0]

        self.assertEqual(model["expiresAt"], "2027-06-30")

    def test_model_connection_failures_keep_grounded_retrieval_fallback(self):
        model = {
            "id": "test-model",
            "label": "Test",
            "model": "test-model",
            "endpoint": "https://model.example/v1/chat/completions",
            "apiKeyEnv": "TEST_KEY",
            "status": "active",
        }
        retrieval = {
            "query": "유진증권 로그인 원인 분석",
            "keyword": "로그인",
            "constraints": {"customer_key": "유진증권"},
            "query_plan": {"source": "mantis", "operation": "search"},
            "stages": [
                {
                    "source": "mantis",
                    "status": "ok",
                    "issues_list": [
                        {
                            "id": 30072,
                            "project": "104EuGene(유진증권)",
                            "summary": "로그인 세션 만료 확인",
                            "status": "assigned",
                            "handler": "이준옥",
                            "created_at": "2026-05-12",
                        }
                    ],
                }
            ],
        }
        request = ChatRequest(
            modelId="test-model",
            messages=[{"role": "user", "content": "유진증권 로그인 원인 분석해줘"}],
        )

        for exception in (
            requests.ConnectTimeout("connect timed out"),
            requests.ConnectionError("connection refused"),
        ):
            with self.subTest(exception=type(exception).__name__), patch.object(
                chat_service_module, "get_private_model", return_value=model
            ), patch.object(
                chat_service_module, "get_model_registry", return_value=[model]
            ), patch.object(
                chat_service_module, "get_model_api_key", return_value="key"
            ), patch.object(
                chat_service_module, "plan_search_intent_with_model", return_value={}
            ), patch.object(
                chat_service_module, "run_retrieval_pipeline", return_value=retrieval
            ), patch.object(
                chat_service_module, "should_run_retrieval", return_value=True
            ), patch.object(
                chat_service_module, "should_return_deterministic_issue_list", return_value=False
            ), patch.object(
                chat_service_module, "should_return_deterministic_notion_list", return_value=False
            ), patch.object(
                chat_service_module, "post_model_chat", side_effect=exception
            ), patch.object(
                chat_service_module,
                "persist_chat_response",
                side_effect=lambda _request, _question, response, user_uid="": response,
            ):
                response = chat_service_module.chat_service.build_response(request)

            self.assertEqual(response["raw"]["mode"], "model_timeout_fallback")
            self.assertEqual(response["raw"]["errorType"], type(exception).__name__)
            self.assertEqual(response["sourcesForFollowUps"][0]["issue_id"], 30072)

    @unittest.skipUnless(Path("/dev/fd").exists(), "file descriptor inspection is unavailable")
    def test_repeated_user_lookup_does_not_leak_sqlite_descriptors(self):
        with tempfile.TemporaryDirectory() as temporary_directory, patch.dict(
            os.environ,
            {"AUTH_USERS_DB_PATH": str(Path(temporary_directory) / "users.sqlite3")},
        ):
            auth._init_users_db_at.cache_clear()
            auth.init_users_db()
            gc.collect()
            before = len(list(Path("/dev/fd").iterdir()))
            for index in range(200):
                auth.get_user_by_uid(f"missing-{index}")
            gc.collect()
            after = len(list(Path("/dev/fd").iterdir()))
            auth._init_users_db_at.cache_clear()

        self.assertLessEqual(after - before, 2)


if __name__ == "__main__":
    unittest.main()
