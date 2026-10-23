"""완전한 업무 저장본의 관찰 간 차이를 사용자별 알림으로 저장한다.

업무 조회 시 수집하며 최초 관찰·부분 결과·오래된 세대를 새 이벤트로 해석하지 않는다.
노트 본문은 복제하지 않고 ID·작성 시각·작성자만 비교한다.
"""
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from backend.sources.mantis_store import connection_key, store_path, MAX_AGE_SECONDS


def create_notification_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE workspace_observations (
        user_uid TEXT PRIMARY KEY, scope_key TEXT NOT NULL, observed_at TEXT NOT NULL, facts_json TEXT NOT NULL)""")
    db.execute("""CREATE TABLE workspace_events (
        id TEXT PRIMARY KEY, user_uid TEXT NOT NULL, scope_key TEXT NOT NULL,
        issue_id INTEGER NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL)""")
    db.execute("CREATE INDEX workspace_events_user_time ON workspace_events(user_uid, observed_at DESC)")


def _timestamp(value: str) -> float:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (ValueError, TypeError, AttributeError):
        return 0


def read_notification_facts(issues: list[dict], meta: dict) -> tuple[str, dict] | None:
    """현재 업무 목록과 같은 연결·세대의 노트 식별자만 읽는다."""
    if meta.get("search_complete") is not True or not meta.get("snapshot_complete"):
        return None
    path = store_path()
    if not path.exists():
        return None
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            saved = json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])
            identity = connection_key()
            age = datetime.now(timezone.utc).timestamp() - _timestamp(saved.get("fetchedAt", ""))
            if (saved.get("connection") != identity or saved.get("fetchedAt") != meta.get("fetchedAt")
                    or not 0 <= age < MAX_AGE_SECONDS):
                return None
            facts = {}
            # 개인 담당 범위만 읽으며 본문·첨부·노트 텍스트를 저장하지 않는다.
            for issue in issues:
                row = db.execute("SELECT json_extract(payload, '$.notes') FROM issues WHERE id = ?",
                                 (issue["id"],)).fetchone()
                if row is None:
                    return None
                notes = json.loads(row[0]) if row[0] else None
                facts[int(issue["id"])] = {"title": str(issue.get("summary") or "제목 없음"),
                    "notes": {str(note["id"]): {"createdAt": str(note.get("created_at") or ""),
                        "reporter": str((note.get("reporter") or {}).get("real_name") or "")}
                        for note in notes if isinstance(note, dict) and type(note.get("id")) is int and note["id"] > 0}
                        if isinstance(notes, list) else None}
        return identity, facts
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError, AttributeError, HTTPException):
        return None


def observe_workspace_changes(path: Path, user_uid: str, scope_key: str, facts: dict,
                              observed_at: str, *, complete: bool) -> list[dict]:
    facts = {str(key): value for key, value in facts.items()}
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT scope_key, observed_at, facts_json FROM workspace_observations WHERE user_uid = ?",
                              (user_uid,)).fetchone()
        if complete and _timestamp(observed_at) and (previous is None or previous[0] != scope_key
                                                     or _timestamp(observed_at) > _timestamp(previous[1])):
            old = json.loads(previous[2]) if previous and previous[0] == scope_key else None
            events = []
            if old is not None:
                for issue_id, fact in facts.items():
                    before = old.get(issue_id)
                    if before is None:
                        events.append({"type": "assigned", "issueId": int(issue_id),
                                       "title": f"#{issue_id} 새 배정이 확인되었습니다", "description": fact["title"]})
                        continue
                    for note_id, note in (fact.get("notes") or {}).items():
                        if note_id in (before.get("notes") or {}):
                            continue
                        # 직전 원본에 노트 범위가 없었다면 이후 작성 시각이 있는 것만 새 노트다.
                        if before.get("notes") is None and _timestamp(note.get("createdAt", "")) <= _timestamp(previous[1]):
                            continue
                        events.append({"type": "note", "issueId": int(issue_id), "noteId": int(note_id),
                            "title": f"#{issue_id} 새 노트가 확인되었습니다",
                            "description": fact["title"] + (f" · 작성자 {note['reporter']}" if note.get("reporter") else ""),
                            "occurredAt": note.get("createdAt", "")})
            for event in events:
                key = f"{user_uid}:{scope_key}:{event['type']}:{event['issueId']}:{event.get('noteId', observed_at)}"
                event.update(id="mantis-event-" + hashlib.sha256(key.encode()).hexdigest()[:32],
                             date=observed_at, observedAt=observed_at, read=False)
                db.execute("INSERT OR IGNORE INTO workspace_events VALUES (?,?,?,?,?,?)",
                           (event["id"], user_uid, scope_key, event["issueId"], observed_at, json.dumps(event, ensure_ascii=False)))
            db.execute("INSERT INTO workspace_observations VALUES (?,?,?,?) ON CONFLICT(user_uid) DO UPDATE SET "
                       "scope_key=excluded.scope_key, observed_at=excluded.observed_at, facts_json=excluded.facts_json",
                       (user_uid, scope_key, observed_at, json.dumps(facts, ensure_ascii=False)))
        result = [json.loads(row[0]) for row in db.execute(
            "SELECT payload_json FROM workspace_events WHERE user_uid=? AND scope_key=? "
            "AND issue_id IN (SELECT CAST(value AS INTEGER) FROM json_each(?)) "
            "ORDER BY observed_at DESC, id DESC LIMIT 30",
            (user_uid, scope_key, json.dumps(list(facts))))]
    # 배정 해제나 연결 사용자 변경 이후 이전 담당 범위의 내용을 노출하지 않는다.
    return result


def collect_workspace_events(path: Path, user_uid: str, person: str, issues: list[dict], meta: dict) -> tuple[list[dict], dict]:
    snapshot = read_notification_facts(issues, meta)
    if snapshot is None or not person:
        return [], {"status": "unavailable", "message": "완전한 동일 세대의 원본이 없어 배정·노트 변경 수집을 보류했습니다."}
    identity, facts = snapshot
    scope_key = hashlib.sha256(f"{identity}:{person}".encode()).hexdigest()
    events = observe_workspace_changes(path, user_uid, scope_key, facts, meta["fetchedAt"], complete=True)
    return events, {"status": "observing", "asOf": meta["fetchedAt"],
                    "message": "업무 조회 시 전체 저장본을 비교합니다. 최초 기준선 이후 확인된 배정·노트만 표시합니다."}
