"""운영 검증에서 발견한 사용자 결과·장애·데이터 복구 회귀 테스트."""

import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from backend.api.workspace import put_conversation, ConversationWriteRequest
from unittest.mock import patch

import requests
from fastapi import HTTPException
from backend import workspace
from backend.db_migrations import run_sqlite_migrations
from backend.intent.matching import chroma_text_matches_constraints
from backend.query_filters import filter_audit_for_source
from backend.query_planning import build_query_plan
from backend.reports import aggregate, sla
from backend.schemas import ChatMessage, ChatRequest
from backend.intent.request_scope import authenticated_owner_scope, AUTHENTICATED_OWNER
from backend.services import chat_service as chat
from backend.sources import mantis
from backend.understanding import extract_query_constraints, merge_query_plan_constraints
from scripts import runtime_databases


def issue(issue_id, handler="테스트갑", status="assigned"):
    return {
        "id": issue_id,
        "summary": "[1234] 합성 이슈",
        "project": {"name": "KB증권"},
        "handler": {"real_name": handler},
        "reporter": {"real_name": "요청자"},
        "status": {"name": status},
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
        "custom_fields": [{"field": {"name": "화면번호"}, "value": "1234"}],
    }


class SearchRegressionTests(unittest.TestCase):
    def test_notion_title_cannot_be_intercepted_by_mantis_reports(self):
        for title in ("코스콤 KRX 거래시간 연장 관련 테스트 범위 및 공수 확인", "주간보고"):
            question = f"노션에서 '{title}' 항목을 찾아서 알려줘"
            with (
                patch.object(chat, "build_unknown_customer_report") as guard,
                patch.object(chat, "build_weekly_report") as weekly,
                patch.object(chat, "build_effort_report") as effort,
                patch.object(chat, "run_notion_write_command", return_value=None),
            ):
                result = chat.build_deterministic_report(
                    question, question, {"mantis": True, "notion": True}, [], None,
                )
            self.assertIsNone(result.response)
            self.assertIsNone(result.pending_effort_report)
            guard.assert_not_called()
            weekly.assert_not_called()
            effort.assert_not_called()

    def test_personal_sla_report_retains_authenticated_scope(self):
        question = "내 장기 미종료 이슈 알려줘"
        text = chat.apply_authenticated_mantis_scope(question, {"mantisUsername": "테스트갑"})
        with patch.object(sla, "fetch_mantis_recent_issues", return_value=[issue(1), issue(2, "테스트을")]):
            result = chat.build_deterministic_report(
                question, text, {"mantis": True}, [], {"uid": "a", "mantisUsername": "테스트갑"}
            ).response
        self.assertIsNotNone(result)
        self.assertEqual([source["issue_id"] for source in result["sources"]], [1])
        self.assertIn("담당자: 테스트갑", chat.apply_authenticated_mantis_scope(question, {"displayName": "테스트갑"}))

    def test_chat_service_keeps_scope_through_planning_and_final_response(self):
        request = ChatRequest(
            modelId="test", abTest=False, messages=[ChatMessage(role="user", content="내 장기 미종료 이슈 알려줘")]
        )
        with (
            patch.object(chat, "get_private_model", return_value={"id": "test", "status": "active"}),
            patch.object(chat, "get_model_api_key", return_value="synthetic"),
            patch.object(sla, "fetch_mantis_recent_issues", return_value=[issue(1), issue(2, "테스트을")]),
        ):
            result = chat.chat_service.build_response(
                request, user={"uid": "synthetic-scope", "mantisUsername": "테스트갑"}
            )
        self.assertEqual([source["issue_id"] for source in result["sources"]], [1])
        self.assertEqual(AUTHENTICATED_OWNER.get(), "")

    def test_authenticated_scope_does_not_leak_between_workers(self):
        def query(name):
            with authenticated_owner_scope(name):
                return extract_query_constraints("이슈 보여줘")["people"]["names"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(query, ["테스트갑", "테스트을"]))
        self.assertEqual(results, [["테스트갑"], ["테스트을"]])
        self.assertEqual(AUTHENTICATED_OWNER.get(), "")

    def test_screen_exclusion_is_executed_in_live_and_snapshot_search(self):
        clause = {"field": "screen_number", "operator": "not_equals", "value": "1234"}
        constraints = merge_query_plan_constraints({}, {"filter_clauses": [clause]})
        self.assertFalse(mantis.issue_matches_constraints(issue(1), constraints))
        document = "[Mantis 이슈 정보]\n- 이슈 ID: #1\n- 화면번호: 1234\n[제목]\n[1234] 합성 이슈"
        self.assertFalse(chroma_text_matches_constraints(document, constraints))
        self.assertTrue(filter_audit_for_source("mantis", [clause])["fully_applied"])
        clause["operator"] = "gte"
        self.assertFalse(filter_audit_for_source("mantis", [clause])["fully_applied"])

    def test_latest_window_is_cut_before_filtering_and_groups_use_same_rows(self):
        rows = [issue(i) for i in range(200, 0, -1)]
        for i, row in enumerate(rows):
            row["handler"]["real_name"] = "테스트갑" if i < 125 else "테스트을"
        snapshot = {
            "available": True,
            "scanned_count": 150,
            "matched_count": 0,
            "matched_ids": set(),
            "issues": [],
            "group_counts": {"오래된 값": 999},
            "reason": "",
        }
        question = "2026년 KB증권 이슈 최신 150건 기준 담당자별 몇 건이야?"
        plan = build_query_plan(
            question,
            {"source": "mantis"},
            extract_query_constraints(question),
            {"planned": True, "source": "mantis", "operation": "count", "group_by": "owner"},
        )
        plan["group_by"] = "owner"
        with (
            patch.object(aggregate, "scan_mantis_documents", return_value=snapshot),
            patch.object(mantis, "fetch_mantis_issues", side_effect=[rows[:100], rows[100:]]),
        ):
            report = aggregate.build_mantis_count_report(question, plan)
        meta = report["retrieval"]["stages"][0]["meta"]
        self.assertEqual(meta["total_count"], 150)
        self.assertTrue(meta["scan_complete"])
        self.assertIn("테스트갑 — 125건", report["content"])
        self.assertIn("테스트을 — 25건", report["content"])
        self.assertNotIn("999", report["content"])
        with patch.object(mantis, "fetch_mantis_issues", side_effect=[rows[:100], rows[100:]]):
            result = mantis.collect_mantis_live_ids_in_range(
                {"people": {"names": ["테스트을"], "role": "handler"}}, max_pages=2, scan_limit=150
            )
        self.assertEqual(len(result["issues"]), 25)

    def test_page_cap_is_not_reported_as_complete(self):
        with patch.object(mantis, "fetch_mantis_issues", return_value=[issue(1), issue(2)]):
            result = mantis.collect_mantis_live_ids_in_range({}, max_pages=1, page_size=2)
        self.assertFalse(result["complete"])

    def test_revalidation_distinguishes_failure_denial_and_deletion(self):
        stage = {"status": "ok", "issues_list": [{"id": 1}], "meta": {"matched_issue_ids": [1]}}
        constraints = {"statuses": ["open"]}
        for error in (requests.Timeout(), HTTPException(502), HTTPException(429)):
            with (
                self.subTest(error=type(error).__name__),
                patch.object(mantis, "fetch_mantis_issue_by_id", side_effect=error),
            ):
                result = mantis.revalidate_mantis_snapshot_stage(stage, constraints)
            self.assertEqual(result["meta"]["live_fallback_count"], 1)
            self.assertFalse(result["meta"]["live_revalidation_complete"])
        for error in (HTTPException(403), HTTPException(404)):
            with patch.object(mantis, "fetch_mantis_issue_by_id", side_effect=error):
                self.assertEqual(mantis.revalidate_mantis_snapshot_stage(stage, constraints)["issues_list"], [])
        with patch.object(mantis, "fetch_mantis_issue_by_id", return_value=None):
            self.assertEqual(mantis.revalidate_mantis_snapshot_stage(stage, constraints)["issues_list"], [])


class DatabaseRegressionTests(unittest.TestCase):
    def test_stale_conversation_update_is_rejected_without_losing_data(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")}),
        ):
            first = workspace.save_conversation("a", "chat", "first", [], False, expected_updated_at="")
            second = workspace.save_conversation(
                "a", "chat", "second", [], False, expected_updated_at=first["updatedAt"]
            )
            with self.assertRaises(workspace.ConversationConflictError):
                workspace.save_conversation("a", "chat", "stale", [], False, expected_updated_at=first["updatedAt"])
            self.assertEqual(workspace.list_conversations("a")[0]["title"], "second")
            self.assertNotEqual(first["updatedAt"], second["updatedAt"])

    def test_workspace_api_requires_revision_for_existing_conversation(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.dict(os.environ, {"WORKSPACE_DB_PATH": str(Path(directory) / "workspace.sqlite3")}),
        ):
            request = SimpleNamespace(state=SimpleNamespace(user={"uid": "a"}))
            first = put_conversation("chat", ConversationWriteRequest(title="first"), request)
            with self.assertRaises(HTTPException) as raised:
                put_conversation("chat", ConversationWriteRequest(title="stale"), request)
            self.assertEqual(raised.exception.status_code, 409)
            updated = put_conversation(
                "chat",
                ConversationWriteRequest(title="latest", expectedUpdatedAt=first["conversation"]["updatedAt"]),
                request,
            )
            self.assertEqual(updated["conversation"]["title"], "latest")

    def test_failed_ddl_leaves_no_partial_schema_or_version(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.sqlite3"

            def fail(connection):
                connection.execute("CREATE TABLE partial (id INTEGER)")
                raise ValueError("synthetic migration failure")

            with self.assertRaises(ValueError):
                run_sqlite_migrations(database, [fail], database_name="test")
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertIsNone(connection.execute("SELECT name FROM sqlite_master WHERE name='partial'").fetchone())

    def test_concurrent_migration_runs_each_version_once(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.sqlite3"

            def migrate(connection):
                connection.execute("CREATE TABLE records (id INTEGER)")

            with ThreadPoolExecutor(max_workers=5) as pool:
                list(pool.map(lambda _: run_sqlite_migrations(database, [migrate], database_name="test"), range(5)))
            with closing(sqlite3.connect(database)) as connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_legacy_import_is_repeatable_and_failed_deployment_restores_schema_and_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, state = root / "legacy", root / "state"
            legacy.mkdir()
            state.mkdir()
            with closing(sqlite3.connect(legacy / "workspace.sqlite3")) as connection, connection:
                connection.execute("CREATE TABLE records (value TEXT)")
                connection.execute("INSERT INTO records VALUES ('original')")
            runtime_databases.import_legacy_runtime(legacy, state)
            runtime_databases.import_legacy_runtime(legacy, state)
            runtime_databases.backup_runtime(state, root / "backup")
            with closing(sqlite3.connect(state / "workspace.sqlite3")) as connection, connection:
                connection.execute("ALTER TABLE records ADD COLUMN changed TEXT")
                connection.execute("UPDATE records SET value='changed'")
                connection.execute("PRAGMA user_version=9")
            (state / "new.sqlite3").touch()
            runtime_databases.restore_runtime(root / "backup", state)
            with closing(sqlite3.connect(state / "workspace.sqlite3")) as connection:
                self.assertEqual(connection.execute("SELECT * FROM records").fetchall(), [("original",)])
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)
            self.assertFalse((state / "new.sqlite3").exists())

    def test_corrupt_backup_does_not_change_active_database(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            with closing(sqlite3.connect(state / "users.sqlite3")) as connection:
                connection.execute("CREATE TABLE records (id INTEGER)")
            runtime_databases.backup_runtime(state, root / "backup")
            (root / "backup/users.sqlite3").write_bytes(b"corrupt")
            with self.assertRaises(sqlite3.DatabaseError):
                runtime_databases.restore_runtime(root / "backup", state)
            with closing(sqlite3.connect(state / "users.sqlite3")) as connection:
                self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
