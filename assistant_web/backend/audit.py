"""감사 로그 DB와 채팅 스트리밍 컨텍스트(Layer 2).

- CHAT_STREAM_CONTEXT: 요청 스레드별 SSE emitter를 담는 thread-local.
  app.py의 스트리밍 핸들러가 emitter를 설정하고, emit_chat_* 가 읽는다.
  같은 객체를 import로 공유하므로 thread-local 의미가 그대로 유지된다.
- validation_logs / evaluation_runs 테이블 스키마 초기화와 근거 조회.

backend.config 외의 backend 모듈은 참조하지 않는다(순환 방지).
app.py는 `from backend.audit import *`로 이 이름들을 그대로 재노출한다.
"""

import json
import re
import sqlite3
import threading
from typing import Any, Dict, List, Optional

from backend.config import AUDIT_DB_PATH


CHAT_STREAM_CONTEXT = threading.local()


def emit_chat_event(event_type: str, **payload: Any) -> None:
    emitter = getattr(CHAT_STREAM_CONTEXT, "emitter", None)
    if emitter:
        emitter({"type": event_type, **payload})


def emit_chat_status(message: str, stage: str) -> None:
    emit_chat_event("status", message=message, stage=stage)


def init_audit_db() -> None:
    with sqlite3.connect(AUDIT_DB_PATH) as connection:
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


def load_audit_sources(audit_id: str) -> List[Dict[str, Any]]:
    """감사 로그에서 해당 답변이 실제로 표시했던 근거 목록을 그대로 가져온다."""
    if not audit_id or not re.fullmatch(r"[0-9a-f]{32}", audit_id):
        return []
    try:
        init_audit_db()
        with sqlite3.connect(AUDIT_DB_PATH) as connection:
            row = connection.execute(
                "SELECT payload FROM validation_logs WHERE id = ?",
                (audit_id,),
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
