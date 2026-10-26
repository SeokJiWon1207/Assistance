"""로그인 사용자별 업무 홈, 대화 기록, 알림과 팀 현황 서비스."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.display import customer_name
from backend.common import extract_chroma_field, normalize_chroma_issue_document, normalize_for_match
from backend.config import CHROMA_DB_PATH, NOTION_API_BASE, RUNTIME_DIR, get_notion_settings, notion_post_json
from backend.notion.property_roles import responsible_properties
from backend.db_migrations import run_sqlite_migrations
from backend.sources.mantis import normalize_issue
from backend.sources.mantis_collection import collection_metadata
from backend.workspace_data import fetch_workspace_recent_issues, load_complete_workspace, complete_store_expired
from backend.sources.notion import (
    fetch_notion_database_schema,
    fetch_notion_database_title,
    fetch_notion_users,
    normalize_notion_page,
)
from backend import conversation_history
from backend.conversation_history import ConversationConflictError, ConversationDeletedError, ConversationQuotaError  # noqa: F401
from backend.tools.mantis_snapshot import scan_mantis_documents
from backend.workspace_data import load_workspace_snapshot
from backend.team_scope import filter_team_issues, team_member_profiles


def workspace_db_path() -> Path:
    configured = os.getenv("WORKSPACE_DB_PATH", "").strip()
    return Path(configured).expanduser() if configured else RUNTIME_DIR / "workspace.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _create_workspace_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                user_uid TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                title TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                favorite INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_uid, conversation_id)
            )
            """
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_user_updated "
        "ON conversations(user_uid, favorite DESC, updated_at DESC)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_reads (
            user_uid TEXT NOT NULL,
            notification_id TEXT NOT NULL,
            read_at TEXT NOT NULL,
            PRIMARY KEY (user_uid, notification_id)
        )
        """
    )


def _create_team_brief_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_team_briefs (
            week_start TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            generated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_team_brief_reads (
            user_uid TEXT NOT NULL,
            week_start TEXT NOT NULL,
            read_at TEXT NOT NULL,
            PRIMARY KEY (user_uid, week_start)
        )
        """
    )


def _create_playbook_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS resolution_playbooks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            symptom TEXT NOT NULL,
            cause TEXT NOT NULL DEFAULT '',
            resolution TEXT NOT NULL,
            verification TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_issue_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'published', 'archived')),
            created_by_uid TEXT NOT NULL,
            created_by_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_resolution_playbooks_status_updated "
        "ON resolution_playbooks(status, updated_at DESC)"
    )


def init_workspace_db() -> None:
    from backend.workspace_notifications import create_notification_schema
    run_sqlite_migrations(
        workspace_db_path(),
        [_create_workspace_schema, _create_team_brief_schema, _create_playbook_schema, conversation_history.migrate_messages,
         create_notification_schema],
        database_name="workspace",
    )


def _safe_messages(messages):
    return conversation_history.safe_messages(messages)


def list_conversations(user_uid: str, limit: int = 40):
    init_workspace_db()
    return conversation_history.list_conversations(workspace_db_path(), user_uid, limit)


def conversation_listing(user_uid: str, limit: int = 40):
    init_workspace_db()
    return conversation_history.conversation_listing(workspace_db_path(), user_uid, limit)


def get_conversation(user_uid: str, conversation_id: str, *, before=None, limit=60):
    init_workspace_db()
    return conversation_history.get_conversation(workspace_db_path(), user_uid, conversation_id, before=before, limit=limit)


def save_conversation(user_uid, conversation_id, title, messages, favorite, created_at='',
                      expected_updated_at=None, *, start_index=None, truncate=True):
    init_workspace_db()
    return conversation_history.save_conversation(workspace_db_path(), user_uid, conversation_id,
        title, messages, favorite, created_at, expected_updated_at, start_index=start_index, truncate=truncate)


def delete_conversation(user_uid: str, conversation_id: str):
    init_workspace_db()
    return conversation_history.delete_conversation(workspace_db_path(), user_uid, conversation_id)


def delete_conversations(user_uid: str, ids: list[str], *, all_conversations: bool = False) -> list[str]:
    init_workspace_db()
    return conversation_history.delete_conversations(workspace_db_path(), user_uid, ids,
                                                     all_conversations=all_conversations)


def _parse_date(value: Any) -> Optional[date]:
    raw = str(value or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _is_open(status: str) -> bool:
    from backend.mantis_status import mantis_status_bucket
    return mantis_status_bucket(status) == "open"


def _is_notion_closed(status: str) -> bool:
    normalized = normalize_for_match(str(status or ""))
    return any(marker in normalized for marker in ("closed", "종료", "완료", "보관"))


def _due_date(issue: Dict[str, Any]) -> str:
    date_fields = issue.get("date_fields") or {}
    preferred = ("완료예정일", "수정예정일", "배포예정일", "처리예정일", "마감일")
    for field_name in preferred:
        if date_fields.get(field_name):
            return str(date_fields[field_name])[:10]
    for field_name, value in date_fields.items():
        if any(keyword in str(field_name) for keyword in ("예정", "마감", "완료")) and value:
            return str(value)[:10]
    return ""


def _issue_summary(issue: Dict[str, Any]) -> Dict[str, Any]:
    due = _due_date(issue)
    today = date.today()
    due_value = _parse_date(due)
    created = _parse_date(issue.get("created_at"))
    age_days = (today - created).days if created else None
    return {
        "id": issue.get("id"),
        "title": issue.get("summary", "제목 없음"),
        "project": issue.get("project", "미상"),
        "customer_name": customer_name(issue.get("project")),
        "status": issue.get("status", "상태 미상"),
        "isOpen": _is_open(issue.get("status", "")),
        "priority": issue.get("priority", ""),
        "handler": issue.get("handler", "담당자 미지정"),
        "createdAt": issue.get("created_at", ""),
        "updatedAt": issue.get("updated_at", ""),
        "dueDate": due,
        "overdue": bool(due_value and due_value < today and _is_open(issue.get("status", ""))),
        "ageDays": age_days,
        "screenNumber": issue.get("screen_number", ""),
    }


def _matches_person(value: str, person: str) -> bool:
    normalized_value = normalize_for_match(value)
    normalized_person = normalize_for_match(person)
    return bool(normalized_person and normalized_person in normalized_value)


def user_mantis_name(user: Dict[str, Any]) -> str:
    configured = str(user.get("mantisUsername") or "").strip()
    if configured:
        return configured
    display_name = str(user.get("displayName") or "").strip()
    return display_name if "@" not in display_name else ""


def _my_mantis_issues(
    user: Dict[str, Any], total_limit: int = 600, *, prefer_snapshot: bool = False, force_refresh: bool = False
) -> tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    person = user_mantis_name(user)
    if not person:
        return "", [], {"status": "unlinked", "matchedCount": 0}
    complete = load_complete_workspace(
        force_refresh=force_refresh, predicate=lambda issue: _matches_person(issue.get("handler", ""), person),
    )
    if complete is not None:
        rows, meta = complete
        return person, rows, meta
    if prefer_snapshot:
        snapshot_issues, snapshot_meta = load_workspace_snapshot(max(total_limit, 1000))
        if snapshot_meta.get("status") == "snapshot":
            matched = [
                issue
                for issue in snapshot_issues
                if _matches_person(issue.get("handler", ""), person)
            ]
            return person, matched, {**snapshot_meta, "matchedCount": len(matched)}
    try:
        raw_issues = fetch_workspace_recent_issues(
            total_limit=total_limit, request_timeout=6, force_refresh=force_refresh
        )
        normalized = [normalize_issue(issue) for issue in raw_issues]
        matched = [
            issue
            for issue in normalized
            if _matches_person(issue.get("handler", ""), person)
        ]
        return person, matched, {"status": "live", **collection_metadata(raw_issues), "matchedCount": len(matched)}
    except Exception as exc:
        if complete_store_expired():
            return person, [], {"status": "error", "matchedCount": 0, "search_complete": False,
                                "reason": "저장 데이터가 만료됐고 실시간 조회에도 실패했습니다."}
        snapshot = scan_mantis_documents(
            CHROMA_DB_PATH,
            matches=lambda text: _matches_person(
                extract_chroma_field(text, "담당자"), person
            ),
            normalize=normalize_chroma_issue_document,
            example_limit=100,
        )
        status = "snapshot" if snapshot.get("available") else "error"
        return person, snapshot.get("issues") or [], {
            "status": status,
            "matchedCount": int(snapshot.get("matched_count") or 0),
            "reason": snapshot.get("reason") or type(exc).__name__,
        }


def _my_notion_pages(user: Dict[str, Any], limit: int = 12) -> Dict[str, Any]:
    person = str(user.get("displayName") or user_mantis_name(user)).strip()
    if not person or "@" in person:
        return {"status": "skipped", "notion_pages": [], "reason": "사용자 이름이 연결되지 않았습니다."}
    try:
        settings = get_notion_settings()
        if not settings.get("token") or not settings.get("database_ids"):
            return {"status": "skipped", "notion_pages": [], "reason": "Notion 연결 정보가 없습니다."}
        notion_user_id = next(
            (
                user_id
                for user_name, user_id in fetch_notion_users()
                if _matches_person(user_name, person)
            ),
            "",
        )
        if not notion_user_id:
            return {
                "status": "unlinked",
                "notion_pages": [],
                "reason": f"Notion 사용자 '{person}'을 찾지 못했습니다.",
            }
        pages: List[Dict[str, Any]] = []
        errors = []
        for database_id in settings["database_ids"]:
            schema = fetch_notion_database_schema(database_id)
            owner_properties = [
                name
                for name in responsible_properties(schema.get("properties") or {})
                if schema["properties"][name].get("type") == "people"
            ]
            if not owner_properties:
                continue
            database_label = fetch_notion_database_title(database_id)
            for property_name in owner_properties:
                response = notion_post_json(
                    f"{NOTION_API_BASE}/databases/{database_id}/query",
                    {
                        "page_size": min(100, max(limit, 20)),
                        "filter": {
                            "property": property_name,
                            "people": {"contains": notion_user_id},
                        },
                        "sorts": [
                            {"timestamp": "last_edited_time", "direction": "descending"}
                        ],
                    },
                    timeout=10,
                    retries=0,
                )
                if response.status_code >= 400:
                    errors.append(f"{database_label}: {response.status_code}")
                    continue
                for raw_page in response.json().get("results", []):
                    page = normalize_notion_page(
                        raw_page,
                        include_blocks=False,
                        database_id=database_id,
                        database_label=database_label,
                    )
                    if page.get("id") not in {item.get("id") for item in pages}:
                        pages.append(page)
        pages.sort(key=lambda page: page.get("last_edited_time", ""), reverse=True)
        return {
            "status": "partial_error" if errors and pages else "error" if errors else "ok",
            "notion_pages": pages[:limit],
            "reason": "; ".join(errors)[:300] if errors else None,
        }
    except Exception as exc:
        return {"status": "error", "notion_pages": [], "reason": type(exc).__name__}


def _notifications(user_uid: str, issues: List[Dict[str, Any]], notion_pages: List[Dict[str, Any]],
                   *, observed_events: List[Dict[str, Any]] | None = None) -> List[Dict[str, Any]]:
    init_workspace_db()
    with closing(sqlite3.connect(workspace_db_path())) as connection, connection:
        read_ids = {
            row[0]
            for row in connection.execute(
                "SELECT notification_id FROM notification_reads WHERE user_uid = ?",
                (user_uid,),
            ).fetchall()
        }
    notifications: List[Dict[str, Any]] = [dict(event) for event in observed_events or []]
    for issue in issues:
        summary = _issue_summary(issue)
        if summary["overdue"]:
            notifications.append(
                {
                    "id": f"mantis-overdue-{summary['id']}-{summary['dueDate']}",
                    "type": "overdue",
                    "title": f"#{summary['id']} 일정이 지났습니다",
                    "description": summary["title"],
                    "date": summary["dueDate"],
                    "read": False,
                }
            )
        elif _is_open(summary["status"]) and (summary["ageDays"] or 0) >= 30:
            notifications.append(
                {
                    "id": f"mantis-aging-{summary['id']}",
                    "type": "aging",
                    "title": f"#{summary['id']} 장기 미종료",
                    "description": summary["title"],
                    "date": summary["updatedAt"],
                    "read": False,
                }
            )
    today = date.today()
    for page in notion_pages:
        due = _parse_date(page.get("due_date"))
        if due and due < today and not _is_notion_closed(str(page.get("status") or "")):
            notifications.append(
                {
                    "id": f"notion-overdue-{page.get('id')}-{due.isoformat()}",
                    "type": "overdue",
                    "title": "Notion 업무 기한이 지났습니다",
                    "description": page.get("title", "제목 없음"),
                    "date": due.isoformat(),
                    "read": False,
                }
            )
    for notification in notifications:
        notification["read"] = notification["id"] in read_ids
    notifications.sort(key=lambda item: item.get("date", ""), reverse=True)
    notifications.sort(key=lambda item: item["read"])
    return notifications[:30]


def mark_notification_read(user_uid: str, notification_id: str) -> None:
    init_workspace_db()
    with closing(sqlite3.connect(workspace_db_path())) as connection, connection:
        connection.execute(
            """
            INSERT INTO notification_reads (user_uid, notification_id, read_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_uid, notification_id) DO UPDATE SET read_at = excluded.read_at
            """,
            (user_uid, notification_id, _now()),
        )


def build_my_workspace(user: Dict[str, Any], *, refresh: bool = False, live: bool = False) -> Dict[str, Any]:
    person, all_issues, mantis_meta = _my_mantis_issues(
        user, prefer_snapshot=not (refresh or live), force_refresh=refresh
    )
    issues = [_issue_summary(issue) for issue in all_issues]
    issues.sort(
        key=lambda item: (
            not item["overdue"],
            not ((item.get("ageDays") or 0) >= 30),
            not _is_open(item["status"]),
            item.get("dueDate") or "9999-12-31",
            str(item.get("updatedAt") or ""),
        )
    )
    open_issues = [item for item in issues if _is_open(item["status"])]
    today = date.today()
    week_start = today - timedelta(days=6)
    new_this_week = [item for item in issues if week_start <= (_parse_date(item["createdAt"]) or date.min) <= today]
    updated_this_week = [item for item in issues if week_start <= (_parse_date(item["updatedAt"]) or date.min) <= today]
    overdue = [item for item in open_issues if item["overdue"]]
    aging = [item for item in open_issues if (item["ageDays"] or 0) >= 30]
    from backend.workspace_notifications import collect_workspace_events
    init_workspace_db()
    events, notification_scope = collect_workspace_events(workspace_db_path(), user["uid"], person, all_issues, mantis_meta)
    notifications = _notifications(user["uid"], all_issues, [], observed_events=events)
    return {
        "user": {
            "displayName": user.get("displayName", ""),
            "email": user.get("email", ""),
            "team": user.get("team", ""),
            "mantisUsername": person,
        },
        "summary": {
            "assigned": mantis_meta.get("matchedCount", len(issues)),
            "open": len(open_issues),
            "overdue": len(overdue),
            "aging": len(aging),
            "unreadNotifications": sum(not item["read"] for item in notifications),
        },
        "mantis": {
            **mantis_meta,
            "status": mantis_meta.get("status", "unknown"),
            "reason": mantis_meta.get("reason", ""),
            # 개인 워크스페이스의 기본 목록은 현재 할 일이다. 담당 이력 전체를
            # 내려보내면 상단의 '미종료' 수와 목록 건수가 어긋나고 종료된 이슈가
            # 할 일처럼 보이므로 미종료 이슈만 표시한다.
            "issues": open_issues,
        },
        "notion": {
            "status": "hidden",
            "reason": "Mantis 중심 모드에서는 Notion을 조회하지 않습니다.",
            "pages": [],
        },
        "notifications": notifications,
        "notificationScope": notification_scope,
        "weeklyBrief": {
            "period": f"{week_start.isoformat()} ~ {today.isoformat()}",
            "newIssues": len(new_this_week),
            "updatedIssues": len(updated_this_week),
            "openIssues": len(open_issues),
            "overdueIssues": len(overdue),
            "highlights": list({
                str(item["id"]): item
                for item in (overdue[:3] + aging[:3] + updated_this_week[:3])
            }.values())[:6],
        },
    }


def build_team_dashboard(users: List[Dict[str, Any]]) -> Dict[str, Any]:
    source_status = "live"
    source_reason = ""
    source_meta: Dict[str, Any] = {"search_complete": False}
    try:
        complete = load_complete_workspace()
        if complete is not None:
            issues, source_meta = complete
        else:
            raw_issues = fetch_workspace_recent_issues(total_limit=1000, request_timeout=6)
            source_meta = collection_metadata(raw_issues)
            issues = [normalize_issue(issue) for issue in raw_issues]
    except Exception as exc:
        snapshot = {"available": False, "reason": "저장 데이터 만료 및 실시간 조회 실패"} if complete_store_expired() else scan_mantis_documents(
            CHROMA_DB_PATH,
            matches=lambda _text: True,
            normalize=normalize_chroma_issue_document,
            example_limit=1000,
            scan_limit=3000,
        )
        issues = snapshot.get("issues") or []
        source_status = "snapshot" if snapshot.get("available") else "error"
        source_reason = str(snapshot.get("reason") or type(exc).__name__)
    issues = filter_team_issues(issues)
    representative_users = team_member_profiles(users)
    members = []
    for user in representative_users:
        person = user_mantis_name(user)
        assigned = [issue for issue in issues if normalize_for_match(str(issue.get("handler") or "")) == normalize_for_match(person)]
        open_items = [_issue_summary(issue) for issue in assigned if _is_open(issue.get("status", ""))]
        members.append(
            {
                "uid": user.get("uid"),
                "displayName": user.get("displayName") or user.get("email"),
                "mantisUsername": person,
                "role": user.get("role"),
                "assigned": len(assigned),
                "open": len(open_items),
                "overdue": sum(item["overdue"] for item in open_items),
                "aging": sum((item["ageDays"] or 0) >= 30 for item in open_items),
            }
        )
    members.sort(key=lambda item: (item["overdue"], item["open"]), reverse=True)
    team_open = [_issue_summary(issue) for issue in issues if _is_open(issue.get("status", ""))]
    aging = sorted(
        [item for item in team_open if (item["ageDays"] or 0) >= 30],
        key=lambda item: item["ageDays"] or 0,
        reverse=True,
    )
    return {
        "summary": {
            "members": len(members),
            "approvedUsers": sum(bool(user.get("approved")) for user in representative_users),
            "recentIssues": len(issues),
            "open": len(team_open),
            "overdue": sum(item["overdue"] for item in team_open),
            "aging": len(aging),
        },
        "members": members,
        "agingIssues": aging[:20],
        "source": {"status": source_status, "reason": source_reason, **source_meta},
        "generatedAt": _now(),
    }
