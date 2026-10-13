import os
import tempfile
import unittest
from contextlib import closing
import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from backend.api import chat as chat_api
from backend.api import notion as notion_api
from backend.api import operations as operations_api
from backend.api import playbooks as playbooks_api
from backend.api import quality as quality_api
from backend.api import search as search_api
from backend.api import templates as templates_api
from backend.api import workspace as workspace_api
from backend import audit as audit_module
from backend.main import app


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.auth_env = patch.dict(os.environ, {"FIREBASE_AUTH_ENABLED": "false"})
        cls.auth_env.start()
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.auth_env.stop()

    def test_health_and_frontend_assets_are_served(self):
        health = self.client.get("/health")
        index = self.client.get("/")
        core = self.client.get("/static/frontend_core.js")
        app_js = self.client.get("/static/app.js")

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})
        self.assertEqual(index.status_code, 200)
        self.assertEqual(index.headers["cache-control"], "no-store, max-age=0")
        self.assertEqual(index.headers["pragma"], "no-cache")
        self.assertIn('id="chatForm"', index.text)
        self.assertIn("/static/frontend_core.js", index.text)
        self.assertEqual(core.status_code, 200)
        self.assertIn("AssistantFrontendCore", core.text)
        self.assertEqual(app_js.status_code, 200)
        self.assertEqual(app_js.headers["cache-control"], "no-store, max-age=0")

    def test_app_version_uses_release_environment(self):
        with patch.dict(os.environ, {"APP_RELEASE_ID": "20260911T050000Z"}):
            response = self.client.get("/api/app-version")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": "20260911T050000Z"})

    def test_readiness_reports_missing_dependency_without_affecting_liveness(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "backend.api.operations.CHROMA_DB_PATH",
                Path(directory) / "missing.sqlite3",
            ),
        ):
            response = self.client.get("/ready")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "not_ready")
        self.assertEqual(response.json()["checks"]["snapshotDb"]["reason"], "missing")
        self.assertEqual(self.client.get("/health").status_code, 200)

    def test_missing_structured_index_does_not_block_legacy_readiness(self):
        def check_database(path, _table, **_kwargs):
            if path == operations_api.MANTIS_INDEX_DB_PATH:
                return {"status": "error", "reason": "missing"}
            return {"status": "ok"}

        with (
            patch.object(operations_api, "_sqlite_check", side_effect=check_database),
            patch.object(
                operations_api,
                "get_model_transport_security_status",
                return_value={"status": "secure"},
            ),
        ):
            response = self.client.get("/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ready")
        self.assertEqual(response.json()["checks"]["structuredIndexDb"]["reason"], "missing")

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

    def test_team_templates_endpoint_returns_validated_config(self):
        response = self.client.get("/api/templates")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["version"], 1)
        self.assertEqual(len(payload["templates"]), 13)
        template_ids = {item["id"] for item in payload["templates"]}
        self.assertIn("customer-profile", template_ids)
        self.assertIn("monthly-customer-report", template_ids)
        self.assertIn("incident-triage", template_ids)
        self.assertIn("issue-handoff", template_ids)
        self.assertIn("incident-review", template_ids)
        self.assertIn("customer-notice", template_ids)
        customer_fields = [
            field
            for template in payload["templates"]
            for field in template["fields"]
            if field["name"] == "customer"
        ]
        self.assertTrue(customer_fields)
        self.assertTrue(all(field["type"] == "customer" for field in customer_fields))
        self.assertTrue(all(len(field["options"]) == 33 for field in customer_fields))

    def test_team_templates_endpoint_lists_customers_with_recent_issues_first(self):
        class Batch(list):
            meta = {"fetchedAt": "2026-10-04T09:00:00+00:00"}

        from datetime import date, timedelta

        recent = (date.today() - timedelta(days=3)).isoformat()
        old = (date.today() - timedelta(days=200)).isoformat()
        rows = Batch([
            {"project": {"name": "146케이프증권(CAPE증권)"}, "created_at": recent},
            {"project": {"name": "146케이프증권(CAPE증권)"}, "created_at": recent},
            {"project": {"name": "120NH(NH증권)"}, "created_at": recent},
            {"project": {"name": "112KBStar(KB증권)"}, "created_at": old},
        ])
        templates_api._counts_cache.update({"key": None, "counts": {}})
        try:
            with patch.object(templates_api, "read_complete_store", return_value=rows):
                payload = self.client.get("/api/templates").json()
        finally:
            templates_api._counts_cache.update({"key": None, "counts": {}})

        options = next(
            field["options"]
            for template in payload["templates"]
            for field in template["fields"]
            if field["type"] == "customer"
        )
        self.assertEqual(options[:3], ["케이프증권", "NH투자증권", "KB증권"])
        self.assertEqual(len(options), 33)

    @patch.object(workspace_api, "get_weekly_team_brief")
    @patch.object(workspace_api, "current_request_user")
    def test_team_brief_endpoint_uses_authenticated_user(self, current_user, get_brief):
        current_user.return_value = {"uid": "uid-1", "role": "member"}
        get_brief.return_value = {"weekStart": "2026-08-10", "viewed": False}

        response = self.client.get("/api/workspace/team-brief")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["weekStart"], "2026-08-10")
        get_brief.assert_called_once_with("uid-1")

    @patch.object(workspace_api, "list_users")
    @patch.object(workspace_api, "build_operations_dashboard")
    @patch.object(workspace_api, "current_request_user")
    def test_dashboard_endpoint_gives_member_aggregated_team_context(
        self, current_user, build_dashboard, list_users_mock
    ):
        current_user.return_value = {
            "uid": "uid-1",
            "displayName": "팀원",
            "role": "member",
        }
        list_users_mock.return_value = [{"uid": "uid-1", "approved": True}]
        build_dashboard.return_value = {"meta": {"scope": "team", "periodDays": 30}}

        response = self.client.get("/api/workspace/dashboard?days=30")

        self.assertEqual(response.status_code, 200)
        build_dashboard.assert_called_once_with(
            current_user.return_value,
            users=list_users_mock.return_value,
            days=30,
            refresh=False,
            live=False,
        )

    @patch.object(workspace_api, "list_users")
    @patch.object(workspace_api, "build_operations_dashboard")
    @patch.object(workspace_api, "current_request_user")
    def test_dashboard_endpoint_gives_manager_aggregated_team_context(
        self, current_user, build_dashboard, list_users_mock
    ):
        current_user.return_value = {
            "uid": "manager-1",
            "displayName": "팀장",
            "role": "manager",
        }
        list_users_mock.return_value = [{"uid": "member-1", "approved": True}]
        build_dashboard.return_value = {"meta": {"scope": "team", "periodDays": 14}}

        response = self.client.get("/api/workspace/dashboard?days=14&refresh=true")

        self.assertEqual(response.status_code, 200)
        build_dashboard.assert_called_once_with(
            current_user.return_value,
            users=list_users_mock.return_value,
            days=14,
            refresh=True,
            live=False,
        )

    @patch.object(workspace_api, "build_my_workspace")
    @patch.object(workspace_api, "current_request_user")
    def test_personal_workspace_refresh_bypasses_fast_snapshot(self, current_user, build_workspace):
        current_user.return_value = {"uid": "uid-1", "role": "member"}
        build_workspace.return_value = {"summary": {"open": 0}}

        response = self.client.get("/api/workspace/me?refresh=true")

        self.assertEqual(response.status_code, 200)
        build_workspace.assert_called_once_with(current_user.return_value, refresh=True, live=False)

    @patch.object(workspace_api, "build_my_workspace")
    @patch.object(workspace_api, "current_request_user")
    def test_personal_live_request_can_reuse_shared_data(self, current_user, build_workspace):
        current_user.return_value = {"uid": "uid-1", "role": "member"}
        build_workspace.return_value = {"summary": {"open": 0}}
        response = self.client.get("/api/workspace/me?live=true")
        self.assertEqual(response.status_code, 200)
        build_workspace.assert_called_once_with(current_user.return_value, refresh=False, live=True)

    @patch.object(workspace_api, "current_request_user")
    def test_dashboard_endpoint_rejects_unsupported_period(self, current_user):
        current_user.return_value = {"uid": "uid-1", "role": "manager"}

        response = self.client.get("/api/workspace/dashboard?days=15")

        self.assertEqual(response.status_code, 422)
        self.assertIn("7일", response.json()["detail"])

    @patch.object(workspace_api, "current_request_user")
    def test_member_cannot_refresh_team_brief(self, current_user):
        current_user.return_value = {"uid": "uid-1", "role": "member"}

        response = self.client.post("/api/workspace/team-brief/refresh")

        self.assertEqual(response.status_code, 403)

    @patch.object(playbooks_api, "list_playbooks")
    @patch.object(playbooks_api, "current_request_user")
    def test_member_searches_only_published_playbooks(self, current_user, list_items):
        current_user.return_value = {"uid": "uid-1", "role": "member"}
        list_items.return_value = []

        response = self.client.get("/api/playbooks?q=세션&includeDrafts=true")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["canCurate"])
        self.assertFalse(list_items.call_args.kwargs["include_unpublished"])

    @patch.object(playbooks_api, "current_request_user")
    def test_member_cannot_create_playbook(self, current_user):
        current_user.return_value = {"uid": "uid-1", "role": "member"}

        response = self.client.post(
            "/api/playbooks",
            json={"title": "제목", "symptom": "증상", "resolution": "해결"},
        )

        self.assertEqual(response.status_code, 403)

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

    @patch.object(chat_api.chat_service, "build_admitted_response")
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

    @patch.object(chat_api.chat_service, "build_admitted_response")
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

    @patch.object(chat_api, "current_request_user")
    @patch.object(chat_api.chat_service, "build_admitted_response")
    def test_chat_passes_authenticated_user_context(self, build_response, current_user):
        current_user.return_value = {
            "uid": "uid-1",
            "displayName": "석지원",
            "role": "admin",
            "mantisUsername": "석지원",
        }
        build_response.return_value = {
            "modelId": "test-model",
            "content": "석지원님으로 로그인되어 있습니다.",
            "sources": [],
        }

        response = self.client.post(
            "/api/chat",
            json={
                "modelId": "test-model",
                "messages": [{"role": "user", "content": "내가 누구야?"}],
                "stream": False,
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(build_response.call_args.kwargs["user"]["uid"], "uid-1")

    @patch.object(chat_api.chat_service, "build_admitted_response")
    def test_stream_rate_limit_is_real_http_429_with_retry_after(self, build_response):
        build_response.return_value = {
            "modelId": "test-model",
            "content": "완료",
            "sources": [],
        }
        chat_api.CHAT_ADMISSION.reset()
        request_body = {
            "modelId": "test-model",
            "messages": [{"role": "user", "content": "테스트"}],
            "stream": True,
        }
        with patch.dict(
            os.environ,
            {
                "CHAT_MAX_CONCURRENT_PER_USER": "1",
                "CHAT_MAX_CONCURRENT_TOTAL": "2",
                "CHAT_RATE_LIMIT_PER_MINUTE": "1",
            },
        ):
            first = self.client.post("/api/chat", json=request_body)
            second = self.client.post("/api/chat", json=request_body)
        chat_api.CHAT_ADMISSION.reset()

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 429)
        self.assertIsNotNone(second.headers.get("Retry-After"))
        self.assertNotIn('"type":"error"', second.text.replace(" ", ""))

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
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(quality_api, "ACCURACY_REPORT_DIR", Path(directory)),
        ):
            response = self.client.get("/api/accuracy/reports/accuracy-20260722-000000.json")
        self.assertEqual(response.status_code, 404)

    def test_feedback_is_saved_updated_and_summarized_by_audit_log(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.sqlite3"
            with (
                patch.object(audit_module, "AUDIT_DB_PATH", audit_path),
                patch.object(quality_api, "AUDIT_DB_PATH", audit_path),
            ):
                audit_module.init_audit_db()
                audit_id = "a" * 32
                with closing(sqlite3.connect(audit_path)) as connection, connection:
                    connection.execute(
                        """
                INSERT INTO validation_logs (
                    id, created_at, question, model_id, mode, score, status, payload
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                        (audit_id, "2026-07-24T12:00:00+09:00", "테스트 질문", "test-model", "model", 95, "pass", "{}"),
                    )

                missing_reason = self.client.post(
                    "/api/feedback",
                    json={"auditId": audit_id, "rating": "needs_improvement"},
                )
                self.assertEqual(missing_reason.status_code, 400)

                saved = self.client.post(
                    "/api/feedback",
                    json={
                        "auditId": audit_id,
                        "rating": "needs_improvement",
                        "reason": "wrong_filters",
                        "comment": "기간 조건이 달라요.",
                        "templateId": "customer-analysis",
                    },
                )
                self.assertEqual(saved.status_code, 200)
                self.assertEqual(saved.json()["feedback"]["reasonLabel"], "검색 조건이 잘못됨")

                updated = self.client.post(
                    "/api/feedback",
                    json={
                        "auditId": audit_id,
                        "rating": "helpful",
                        "templateId": "customer-analysis",
                    },
                )
                self.assertEqual(updated.status_code, 200)
                self.assertIsNone(updated.json()["feedback"]["reason"])

                summary = self.client.get("/api/feedback/summary?limit=20")
                self.assertEqual(summary.status_code, 200)
                payload = summary.json()
                self.assertEqual(payload["total"], 1)
                self.assertEqual(payload["helpfulCount"], 1)
                self.assertEqual(payload["helpfulRate"], 100)
                self.assertEqual(payload["templateCounts"][0]["templateId"], "customer-analysis")
                self.assertEqual(payload["recent"][0]["question"], "테스트 질문")

    def test_feedback_rejects_an_unknown_audit_log(self):
        with tempfile.TemporaryDirectory() as directory:
            audit_path = Path(directory) / "audit.sqlite3"
            with (
                patch.object(audit_module, "AUDIT_DB_PATH", audit_path),
                patch.object(quality_api, "AUDIT_DB_PATH", audit_path),
            ):
                response = self.client.post(
                    "/api/feedback",
                    json={"auditId": "b" * 32, "rating": "helpful"},
                )
        self.assertEqual(response.status_code, 404)

    @patch.object(quality_api.subprocess, "Popen")
    def test_accuracy_runner_passes_follow_up_scenario_count(self, popen):
        process = Mock()
        process.stdout = iter(
            [
                'PROGRESS {"type":"run_start","total":14}\n',
                'PROGRESS {"type":"summary","summary":{"passed":14,"total":14}}\n',
            ]
        )
        process.wait.return_value = 0
        popen.return_value = process

        frames = list(quality_api.stream_accuracy_run(10, 4))

        command = popen.call_args.args[0]
        self.assertIn("--questions", command)
        self.assertIn("--follow-up-scenarios", command)
        self.assertEqual(command[command.index("--follow-up-scenarios") + 1], "4")
        self.assertTrue(any('"total": 14' in frame for frame in frames))

    @patch.object(quality_api.subprocess, "Popen")
    def test_accuracy_runner_terminates_child_when_stream_closes(self, popen):
        process = Mock()
        process.stdout = iter(['PROGRESS {"type":"run_start","total":1}\n'])
        process.poll.return_value = None
        process.wait.return_value = 0
        popen.return_value = process
        stream = quality_api.stream_accuracy_run(1, 0)

        self.assertIn('"run_start"', next(stream))
        stream.close()

        process.terminate.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=3)
        self.assertFalse(quality_api.ACCURACY_RUN_LOCK.locked())


if __name__ == "__main__":
    unittest.main()
