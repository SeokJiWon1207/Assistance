#!/usr/bin/env python3
"""Run backend tests against a deterministic, disposable Mantis snapshot."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
import socket
from unittest.mock import patch
from contextlib import closing
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
FIXTURE_PATH = Path(__file__).resolve().parent / "fixtures" / "mantis_documents.json"


def render_document(item: dict[str, object]) -> str:
    return "\n".join(
        [
            "[Mantis 이슈 정보]",
            f"- 이슈 ID: #{item['issue_id']}",
            f"- 프로젝트(고객사): {item['project']}",
            f"- 매체구분: {item['platform']}",
            f"- 화면번호: {item['screen'] or '미상'}",
            f"- 현재 상태: {item['status']}",
            f"- 담당자: {item['handler']}",
            f"- 요청자: {item['reporter']}",
            f"- 등록일: {item['created_at']}",
            f"- 최종수정일: {item['created_at']}",
            "",
            "[제목]",
            str(item["title"]),
            "",
            "[고객사 본문 요청 내용]",
            f"{item['project']} {item['platform']} {item['title']}",
        ]
    )


def create_snapshot(database_path: Path) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    with closing(sqlite3.connect(database_path)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE embedding_metadata (
                id INTEGER NOT NULL,
                key TEXT NOT NULL,
                string_value TEXT,
                int_value INTEGER,
                PRIMARY KEY (id, key)
            )
            """
        )
        for row_id, item in enumerate(fixture, start=1):
            connection.executemany(
                "INSERT INTO embedding_metadata (id, key, string_value, int_value) VALUES (?, ?, ?, ?)",
                [
                    (row_id, "chroma:document", render_document(item), None),
                    (row_id, "id", None, int(item["issue_id"])),
                ],
            )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="assistant-tests-") as temporary_directory:
        snapshot_path = Path(temporary_directory) / "mantis_fixture.sqlite3"
        create_snapshot(snapshot_path)
        isolated_env = {
            key: value
            for key, value in os.environ.items()
            if key in {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "PYTHONWARNINGS"}
        }
        isolated_env.update(
            {
                "CHROMA_DB_PATH": str(snapshot_path),
                "ASSISTANT_RUNTIME_DIR": temporary_directory,
                "AUTH_USERS_DB_PATH": str(Path(temporary_directory) / "users.sqlite3"),
                "WORKSPACE_DB_PATH": str(Path(temporary_directory) / "workspace.sqlite3"),
                "AUDIT_DB_PATH": str(Path(temporary_directory) / "audit.sqlite3"),
                "ANONYMIZED_TELEMETRY": "False",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        os.chdir(APP_DIR)
        sys.path.insert(0, str(APP_DIR))
        sys.dont_write_bytecode = True
        with patch.dict(os.environ, isolated_env, clear=True):
            from backend import environment
            from pydantic_settings.sources.providers.dotenv import DotEnvSettingsSource

            with (
                patch.object(environment, "load_dotenv"),
                patch.object(DotEnvSettingsSource, "_read_env_files", return_value={}),
                patch.object(socket.socket, "connect", side_effect=RuntimeError("Test network access blocked")),
                patch.object(socket.socket, "connect_ex", side_effect=RuntimeError("Test network access blocked")),
            ):
                suite = unittest.defaultTestLoader.discover(str(APP_DIR / "tests"), pattern="test_*.py")
                result = unittest.TextTestRunner(verbosity=2).run(suite)
                return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
