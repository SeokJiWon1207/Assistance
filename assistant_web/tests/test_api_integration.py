import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.api import chat as chat_api
from backend.api import notion as notion_api
from backend.api import quality as quality_api
from backend.api import search as search_api
from backend.main import app


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_health_and_frontend_assets_are_served(self):
        health = self.client.get("/health")
        index = self.client.get("/")
        core = self.client.get("/static/frontend_core.js")
        app_js = self.client.get("/static/app.js")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertEqual(index.status_code, 200)
        self.assertIn('id="chatForm"', index.text)
        self.assertIn("/static/frontend_core.js", index.text)
        self.assertEqual(core.status_code, 200)
        self.assertIn("AssistantFrontendCore", core.text)
        self.assertEqual(app_js.status_code, 200)

    @patch.object(search_api, "get_model_registry")
    def test_models_endpoint_exposes_only_public_fields(self, registry):
        registry.return_value = [
            {
                "id": "test-model",
                "label": "테스트 모델",
                "model": "internal-name",
                "endpoint": "https://secret-model.example/v1/chat/completions",
                "apiKeyEnv": "SECRET_MODEL_KEY",
                "expiresAt": "",
                "enabled": True,
                "status": "active",
                "expired": False,
                "hasKey": True,
            }
        ]

        response = self.client.get("/api/models")

        self.assertEqual(response.status_code, 200)
        model = response.json()["models"][0]
        self.assertEqual(model["id"], "test-model")
        self.assertNotIn("endpoint", model)
        self.assertNotIn("apiKeyEnv", model)

    @patch.object(notion_api, "notion_is_configured", return_value=True)
    @patch.object(notion_api, "get_notion_settings")
    def test_notion_status_masks_token_and_database_ids(self, settings, _configured):
        settings.return_value = {
            "token": "secret-token",
            "database_ids": ["1234567890abcdef1234567890abcdef"],
            "version": "2022-06-28",
        }

        response = self.client.get("/api/notion/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["configured"])
        self.assertEqual(payload["databaseIds"], ["90abcdef"])
        self.assertNotIn("token", payload)
        self.assertNotIn("1234567890abcdef1234567890abcdef", response.text)

    @patch.object(chat_api.chat_service, "build_response")
    def test_non_stream_chat_returns_the_application_response(self, build_response):
        build_response.return_value = {
            "modelId": "test-model",
            "content": "통합 테스트 응답",
            "sources": [],
        }

        response = self.client.post(
            "/api/chat",
            json={
                "modelId": "test-model",
                "messages": [{"role": "user", "content": "테스트"}],
                "stream": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "통합 테스트 응답")
        self.assertFalse(build_response.call_args.args[0].stream)

    @patch.object(chat_api.chat_service, "build_response")
    def test_stream_chat_uses_the_sse_contract(self, build_response):
        build_response.return_value = {
            "modelId": "test-model",
            "content": "스트리밍 완료",
            "sources": [],
        }

        response = self.client.post(
            "/api/chat",
            json={
                "modelId": "test-model",
                "messages": [{"role": "user", "content": "테스트"}],
                "stream": True,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertIn('"type": "result"', response.text)
        self.assertIn("스트리밍 완료", response.text)

    def test_chat_rejects_an_invalid_message_role(self):
        response = self.client.post(
            "/api/chat",
            json={
                "modelId": "test-model",
                "messages": [{"role": "tool", "content": "허용되지 않는 역할"}],
            },
        )

        self.assertEqual(response.status_code, 422)

    @patch.object(notion_api, "build_sources_from_single_notion_page", return_value=[])
    @patch.object(notion_api, "create_notion_page")
    def test_notion_create_endpoint_validates_and_delegates(self, create_page, _sources):
        create_page.return_value = {"id": "page-1", "title": "새 이슈"}

        response = self.client.post(
            "/api/notion/pages",
            json={"databaseLabel": "이슈리스트", "title": "새 이슈", "status": "진행중"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["page"]["id"], "page-1")
        request = create_page.call_args.args[0]
        self.assertEqual(request.title, "새 이슈")
        self.assertEqual(request.status, "진행중")

    def test_accuracy_report_rejects_an_invalid_filename(self):
        response = self.client.get("/api/accuracy/reports/not-a-report.json")
        self.assertEqual(response.status_code, 400)

    def test_missing_accuracy_report_returns_not_found(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            quality_api, "ACCURACY_REPORT_DIR", Path(directory)
        ):
            response = self.client.get("/api/accuracy/reports/accuracy-20260722-000000.json")
        self.assertEqual(response.status_code, 404)

    @patch.object(quality_api.subprocess, "Popen")
    def test_accuracy_runner_passes_follow_up_scenario_count(self, popen):
        process = Mock()
        process.stdout = iter([
            'PROGRESS {"type":"run_start","total":14}\n',
            'PROGRESS {"type":"summary","summary":{"passed":14,"total":14}}\n',
        ])
        process.wait.return_value = 0
        popen.return_value = process

        frames = list(quality_api.stream_accuracy_run(10, 4))

        command = popen.call_args.args[0]
        self.assertIn("--questions", command)
        self.assertIn("--follow-up-scenarios", command)
        self.assertEqual(command[command.index("--follow-up-scenarios") + 1], "4")
        self.assertTrue(any('"total": 14' in frame for frame in frames))


if __name__ == "__main__":
    unittest.main()
