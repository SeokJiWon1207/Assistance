"""사용자에게 확인받은 Notion 작업을 영속 저장하고 한 번만 실행한다."""

import json
import sqlite3
import time
import uuid
from contextlib import closing
from typing import Any, Callable

from fastapi import HTTPException
from backend.config import RUNTIME_DIR


def _connect() -> sqlite3.Connection:
    path = RUNTIME_DIR / "notion_actions.sqlite3"
    connection = sqlite3.connect(path, timeout=5)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS actions (id TEXT PRIMARY KEY, uid TEXT NOT NULL, "
        "payload TEXT NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL, result TEXT)"
    )
    connection.commit()
    path.chmod(0o600)
    return connection


def save_action(uid: str, payload: dict[str, Any]) -> str:
    if not uid:
        raise HTTPException(401, "Notion 작업 확인에는 로그인이 필요합니다.")
    action_id = uuid.uuid4().hex
    with closing(_connect()) as connection, connection:
        connection.execute("DELETE FROM actions WHERE expires < ?", (time.time() - 86400,))
        connection.execute(
            "INSERT INTO actions VALUES (?, ?, ?, ?, 'pending', NULL)",
            (action_id, uid, json.dumps(payload, ensure_ascii=False), time.time() + 600),
        )
    return action_id


def consume_action(
    action_id: str, uid: str, execute: Callable[[dict[str, Any]], dict[str, Any]], *, cancel: bool = False
) -> dict[str, Any]:
    with closing(_connect()) as connection, connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload, expires, state, result FROM actions WHERE id=? AND uid=?", (action_id, uid)
        ).fetchone()
        if not uid or row is None or row[1] < time.time():
            raise HTTPException(409, "확인 요청이 만료되었거나 유효하지 않습니다. 작업을 다시 요청해 주세요.")
        if row[2] == "completed" and not cancel:
            return json.loads(row[3])
        if row[2] != "pending":
            raise HTTPException(409, "이미 처리 중이거나 처리된 작업입니다. Notion에서 결과를 확인해 주세요.")
        connection.execute("UPDATE actions SET state=? WHERE id=?", ("cancelled" if cancel else "executing", action_id))
    if cancel:
        return {"content": "🚫 Notion 작업을 취소했습니다.", "sources": [], "raw": {"mode": "notion_write_cancelled"}}
    # 외부 쓰기 후 통신이 끊기면 성공 여부를 알 수 없다. 자동 재실행하지 않는다.
    result = execute(json.loads(row[0]))
    with closing(_connect()) as connection, connection:
        connection.execute(
            "UPDATE actions SET state='completed', result=? WHERE id=? AND uid=?",
            (json.dumps(result, ensure_ascii=False), action_id, uid),
        )
    return result
