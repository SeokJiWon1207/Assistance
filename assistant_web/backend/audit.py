"""감사 로그 DB와 채팅 스트리밍 컨텍스트(Layer 2).

- CHAT_STREAM_CONTEXT: 요청 스레드별 SSE emitter를 담는 thread-local.
  api/chat.py의 스트리밍 핸들러가 emitter를 설정하고, emit_chat_* 가 읽는다.
  같은 객체를 import로 공유하므로 thread-local 의미가 그대로 유지된다.
- validation_logs / evaluation_runs 테이블 스키마 초기화와 근거 조회.

backend.config 외의 backend 모듈은 참조하지 않는다(순환 방지).
"""

import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from backend.config import AUDIT_DB_PATH
from backend.db_migrations import run_sqlite_migrations


CHAT_STREAM_CONTEXT = threading.local()


def emit_chat_event(event_type: str, **payload: Any) -> None:
    emitter = getattr(CHAT_STREAM_CONTEXT, "emitter", None)
    if emitter:
        emitter({"type": event_type, **payload})


def emit_chat_status(message: str, stage: str) -> None:
    emit_chat_event("status", message=message, stage=stage)


def _create_audit_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
            """
            CREATE TABLE IF NOT EXISTS validation_logs (
                id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                question TEXT NOT NULL,
                model_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL
            )
            """
        )
    connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_validation_logs_created_at ON validation_logs(created_at DESC)"
        )
    connection.execute(
            """
            CREATE TABLE IF NOT EXISTS evaluation_runs (
                id TEXT PRIMARY KEY,
                run_at TEXT NOT NULL,
                total INTEGER NOT NULL,
                passed INTEGER NOT NULL,
                failed INTEGER NOT NULL,
                score INTEGER NOT NULL,
                duration_ms INTEGER NOT NULL,
                results TEXT NOT NULL
            )
            """
        )
    connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_evaluation_runs_run_at ON evaluation_runs(run_at DESC)"
        )
    connection.execute(
            """
            CREATE TABLE IF NOT EXISTS answer_feedback (
                id TEXT PRIMARY KEY,
                audit_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                rating TEXT NOT NULL,
                reason TEXT,
                comment TEXT NOT NULL DEFAULT '',
                template_id TEXT,
                FOREIGN KEY (audit_id) REFERENCES validation_logs(id)
            )
            """
        )
    connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_answer_feedback_updated_at ON answer_feedback(updated_at DESC)"
        )
    connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_answer_feedback_rating ON answer_feedback(rating)"
        )


def _add_validation_log_user_uid(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(validation_logs)").fetchall()
    }
    if "user_uid" not in columns:
        connection.execute(
            "ALTER TABLE validation_logs ADD COLUMN user_uid TEXT NOT NULL DEFAULT ''"
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_validation_logs_user_created "
        "ON validation_logs(user_uid, created_at DESC)"
    )


def _add_failed_checks_summary(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(validation_logs)").fetchall()
    }
    if "failed_checks_json" not in columns:
        connection.execute(
            "ALTER TABLE validation_logs "
            "ADD COLUMN failed_checks_json TEXT NOT NULL DEFAULT '[]'"
        )
    rows = connection.execute(
        "SELECT id, payload FROM validation_logs WHERE failed_checks_json = '[]'"
    ).fetchall()
    for log_id, raw_payload in rows:
        try:
            payload = json.loads(raw_payload or "{}")
        except (TypeError, ValueError):
            continue
        checks = (payload.get("verification") or {}).get("checks") or []
        failed = [
            {
                "id": str(check.get("id") or "unknown")[:128],
                "label": str(check.get("label") or check.get("id") or "unknown")[:256],
            }
            for check in checks
            if isinstance(check, dict) and check.get("status") == "fail"
        ]
        if failed:
            connection.execute(
                "UPDATE validation_logs SET failed_checks_json = ? WHERE id = ?",
                (json.dumps(failed, ensure_ascii=False), log_id),
            )


def init_audit_db() -> None:
    run_sqlite_migrations(
        AUDIT_DB_PATH,
        [_create_audit_schema, _add_validation_log_user_uid, _add_failed_checks_summary],
        database_name="audit",
    )


def prune_audit_logs(retention_days: int | None = None) -> Dict[str, int]:
    init_audit_db()
    if retention_days is None:
        try:
            retention_days = int(os.getenv("AUDIT_RETENTION_DAYS", "90"))
        except ValueError:
            retention_days = 90
    safe_days = max(7, min(int(retention_days), 3650))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=safe_days)).isoformat()
    with closing(sqlite3.connect(AUDIT_DB_PATH)) as connection, connection:
        feedback = connection.execute(
            """
            DELETE FROM answer_feedback
            WHERE audit_id IN (SELECT id FROM validation_logs WHERE created_at < ?)
            """,
            (cutoff,),
        ).rowcount
        validations = connection.execute(
            "DELETE FROM validation_logs WHERE created_at < ?", (cutoff,)
        ).rowcount
        evaluations = connection.execute(
            "DELETE FROM evaluation_runs WHERE run_at < ?", (cutoff,)
        ).rowcount
    return {
        "validationLogs": max(0, validations),
        "evaluationRuns": max(0, evaluations),
        "feedback": max(0, feedback),
    }


def load_audit_sources(audit_id: str, user_uid: str = "") -> List[Dict[str, Any]]:
    """감사 로그에서 해당 답변이 실제로 표시했던 근거 목록을 그대로 가져온다."""
    if not audit_id or not re.fullmatch(r"[0-9a-f]{32}", audit_id):
        return []
    try:
        init_audit_db()
        with closing(sqlite3.connect(AUDIT_DB_PATH)) as connection, connection:
            row = connection.execute(
                "SELECT payload FROM validation_logs WHERE id = ? AND user_uid = ?",
                (audit_id, str(user_uid or "").strip()),
            ).fetchone()
    except sqlite3.Error:
        return []
    if not row:
        return []
    try:
        payload = json.loads(row[0])
    except (TypeError, ValueError):
        return []
    sources = payload.get("sources")
    return sources if isinstance(sources, list) else []


def load_audit_query_plan(audit_id: str, user_uid: str, expected_question: str) -> Dict[str, Any]:
    """동일 사용자·직전 질문에 묶인 확정 계획만 상속한다."""
    if not user_uid or not expected_question or not re.fullmatch(r"[0-9a-f]{32}", audit_id or ""):
        return {}
    try:
        init_audit_db()
        with closing(sqlite3.connect(AUDIT_DB_PATH)) as connection:
            row = connection.execute(
                "SELECT payload FROM validation_logs WHERE id = ? AND user_uid = ? AND question = ?",
                (audit_id, user_uid, expected_question),
            ).fetchone()
        payload = json.loads(row[0]) if row else {}
        plan = payload.get("queryPlan")
        return plan if isinstance(plan, dict) else {}
    except (sqlite3.Error, ValueError, TypeError):
        return {}
