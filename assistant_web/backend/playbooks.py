"""유사 사례에서 축적한 해결 플레이북의 저장·검색·큐레이션 서비스."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from typing import Any, Dict, Iterable, List, Optional

from backend.workspace import _now, init_workspace_db, workspace_db_path


PLAYBOOK_STATUSES = {"draft", "published", "archived"}


def _clean_text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _clean_tags(values: Iterable[Any]) -> List[str]:
    tags: List[str] = []
    seen = set()
    for value in values:
        tag = _clean_text(value, 40)
        key = tag.casefold()
        if not tag or key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= 12:
            break
    return tags


def _clean_issue_ids(values: Iterable[Any]) -> List[int]:
    issue_ids: List[int] = []
    seen = set()
    for value in values:
        try:
            issue_id = int(str(value).lstrip("#"))
        except (TypeError, ValueError):
            continue
        if issue_id <= 0 or issue_id in seen:
            continue
        seen.add(issue_id)
        issue_ids.append(issue_id)
        if len(issue_ids) >= 20:
            break
    return issue_ids


def _json_list(value: Any) -> List[Any]:
    try:
        parsed = json.loads(value or "[]")
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _row_to_playbook(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "title": row["title"],
        "symptom": row["symptom"],
        "cause": row["cause"],
        "resolution": row["resolution"],
        "verification": row["verification"],
        "tags": _json_list(row["tags_json"]),
        "sourceIssueIds": _json_list(row["source_issue_ids_json"]),
        "status": row["status"],
        "createdBy": {
            "uid": row["created_by_uid"],
            "name": row["created_by_name"],
        },
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "publishedAt": row["published_at"],
    }


def list_playbooks(
    query: str = "",
    *,
    include_unpublished: bool = False,
    status: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    init_workspace_db()
    clauses: List[str] = []
    parameters: List[Any] = []
    normalized_status = status if status in PLAYBOOK_STATUSES else ""
    if include_unpublished:
        if normalized_status:
            clauses.append("status = ?")
            parameters.append(normalized_status)
    else:
        clauses.append("status = 'published'")
    keyword = _clean_text(query, 200)
    if keyword:
        like = f"%{keyword}%"
        clauses.append(
            "(title LIKE ? OR symptom LIKE ? OR cause LIKE ? OR resolution LIKE ? "
            "OR verification LIKE ? OR tags_json LIKE ? OR source_issue_ids_json LIKE ?)"
        )
        parameters.extend([like] * 7)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    parameters.append(max(1, min(int(limit or 100), 200)))
    with closing(sqlite3.connect(workspace_db_path())) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT * FROM resolution_playbooks
            {where}
            ORDER BY
                CASE status WHEN 'draft' THEN 0 WHEN 'published' THEN 1 ELSE 2 END,
                updated_at DESC
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    return [_row_to_playbook(row) for row in rows]


def get_playbook(playbook_id: str) -> Optional[Dict[str, Any]]:
    init_workspace_db()
    with closing(sqlite3.connect(workspace_db_path())) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM resolution_playbooks WHERE id = ?", (playbook_id,)
        ).fetchone()
    return _row_to_playbook(row) if row else None


def create_playbook(payload: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    init_workspace_db()
    playbook_id = uuid.uuid4().hex
    now = _now()
    status = payload.get("status") if payload.get("status") in PLAYBOOK_STATUSES else "draft"
    values = (
        playbook_id,
        _clean_text(payload.get("title"), 200),
        _clean_text(payload.get("symptom"), 4000),
        _clean_text(payload.get("cause"), 4000),
        _clean_text(payload.get("resolution"), 8000),
        _clean_text(payload.get("verification"), 4000),
        json.dumps(_clean_tags(payload.get("tags") or []), ensure_ascii=False),
        json.dumps(_clean_issue_ids(payload.get("sourceIssueIds") or [])),
        status,
        _clean_text(user.get("uid"), 128),
        _clean_text(user.get("displayName") or user.get("email"), 200),
        now,
        now,
        now if status == "published" else "",
    )
    with closing(sqlite3.connect(workspace_db_path())) as connection, connection:
        connection.execute(
            """
            INSERT INTO resolution_playbooks (
                id, title, symptom, cause, resolution, verification, tags_json,
                source_issue_ids_json, status, created_by_uid, created_by_name,
                created_at, updated_at, published_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
    return get_playbook(playbook_id) or {}


def update_playbook(playbook_id: str, changes: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    current = get_playbook(playbook_id)
    if not current:
        return None
    allowed = {
        "title": ("title", 200),
        "symptom": ("symptom", 4000),
        "cause": ("cause", 4000),
        "resolution": ("resolution", 8000),
        "verification": ("verification", 4000),
    }
    assignments: List[str] = []
    parameters: List[Any] = []
    for key, (column, limit) in allowed.items():
        if key in changes:
            assignments.append(f"{column} = ?")
            parameters.append(_clean_text(changes[key], limit))
    if "tags" in changes:
        assignments.append("tags_json = ?")
        parameters.append(json.dumps(_clean_tags(changes.get("tags") or []), ensure_ascii=False))
    if "sourceIssueIds" in changes:
        assignments.append("source_issue_ids_json = ?")
        parameters.append(json.dumps(_clean_issue_ids(changes.get("sourceIssueIds") or [])))
    if changes.get("status") in PLAYBOOK_STATUSES:
        next_status = changes["status"]
        assignments.append("status = ?")
        parameters.append(next_status)
        assignments.append("published_at = ?")
        parameters.append(_now() if next_status == "published" else "")
    if not assignments:
        return current
    assignments.append("updated_at = ?")
    parameters.extend([_now(), playbook_id])
    with closing(sqlite3.connect(workspace_db_path())) as connection, connection:
        connection.execute(
            f"UPDATE resolution_playbooks SET {', '.join(assignments)} WHERE id = ?",
            parameters,
        )
    return get_playbook(playbook_id)

