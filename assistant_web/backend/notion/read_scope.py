"""설정한 루트 DB의 실제 자손만 읽기 검색 범위에 포함한다.

링크/관계형 속성은 따라가지 않고 Notion이 반환한 parent 체인만 검증한다.
쓰기 허용 목록은 기존 루트 목록 그대로 유지한다.
"""

import json
import os
import time
from functools import lru_cache
from datetime import datetime, timezone
from typing import Any, Dict, Tuple

import requests

from backend.config import (
    NOTION_API_BASE,
    RUNTIME_DIR,
    get_notion_headers,
    notion_post_json,
    notion_request_slot,
)
from backend.notion.client import remember_notion_database_schema


READ_SCOPE_FILENAME = "notion_read_scope.json"
TITLE_INDEX_FILENAME = "notion_title_index.json"


def _id(value: str) -> str:
    return value.replace("-", "").lower()


def child_database_search_enabled() -> bool:
    return os.getenv("NOTION_INCLUDE_CHILD_DATABASES", "false").lower() in {"1", "true", "yes"}


def _read_scope_max_age() -> int:
    # Scope discovery is much less volatile than the title catalog and is
    # refreshed only by maintenance. Keep its lifetime independent from the
    # 24-hour title-index optimization window.
    try:
        configured = int(os.getenv("NOTION_READ_SCOPE_TTL_SECONDS", "2592000"))
    except ValueError:
        configured = 2592000
    return max(3600, min(configured, 31536000))


def save_complete_notion_read_scope(
    roots: list[str], scope: Dict[str, str], generated_at: str
) -> None:
    """Atomically preserve the last fully discovered descendant scope."""
    normalized_roots = tuple(_id(root) for root in roots if root)
    normalized_scope = {
        _id(identifier): _id(root)
        for identifier, root in scope.items()
        if identifier and root
    }
    if not normalized_roots or not all(root in normalized_scope for root in normalized_roots):
        raise ValueError("complete Notion read scope must contain every configured root")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNTIME_DIR / READ_SCOPE_FILENAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "scope_complete": True,
                "root_database_ids": list(normalized_roots),
                "read_scope": normalized_scope,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)


def persisted_notion_read_scope(roots: tuple[str, ...]) -> tuple[Dict[str, str], bool]:
    # Prefer the dedicated last-complete snapshot. Fall back to the current
    # title index so deployments made before this file existed keep their known
    # child DBs immediately after upgrading.
    fallback: Dict[str, str] = {}
    for filename in (READ_SCOPE_FILENAME, TITLE_INDEX_FILENAME):
        path = RUNTIME_DIR / filename
        try:
            stat = path.stat()
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            continue
        stored_roots = tuple(_id(value) for value in data.get("root_database_ids") or [])
        scope = {
            _id(identifier): _id(root)
            for identifier, root in (data.get("read_scope") or {}).items()
            if identifier and root
        }
        if stored_roots != roots or not scope:
            continue
        fallback = fallback or scope
        try:
            generated = data.get("generated_at")
            timestamp = datetime.fromisoformat(generated) if generated else None
            # 최초 형식에 시각이 없을 때만 원본 파일 시각을 사용한다.
            if timestamp is not None and timestamp.tzinfo is None:
                raise ValueError("scope timestamp requires a timezone")
            age = time.time() - (timestamp.timestamp() if timestamp else stat.st_mtime)
            fresh = 0 <= age <= _read_scope_max_age()
        except (TypeError, ValueError):
            fresh = False
        # ``complete`` in the first title-index format described the page
        # catalog. Only ``scope_complete`` proves descendant discovery.
        if data.get("scope_complete") is True and fresh:
            return scope, True
    return fallback, False


def preserve_legacy_notion_read_scope(roots: list[str]) -> None:
    """Migrate once, preserving the original successful discovery time."""
    if (RUNTIME_DIR / READ_SCOPE_FILENAME).exists():
        return
    path = RUNTIME_DIR / TITLE_INDEX_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        normalized = tuple(_id(root) for root in roots)
        if tuple(_id(root) for root in data.get("root_database_ids", [])) != normalized:
            return
        if data.get("scope_complete") is not True:
            return
        generated = data.get("generated_at") or datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc
        ).isoformat()
        save_complete_notion_read_scope(roots, data["read_scope"], generated)
    except (OSError, KeyError, TypeError, ValueError):
        return


@lru_cache(maxsize=16)
def _scope_cached(roots: Tuple[str, ...], token: str, bucket: int) -> tuple[Dict[str, str], bool]:
    scope = {identifier: identifier for identifier in roots}
    parents: Dict[str, Dict[str, Any]] = {}
    complete = True
    deadline = time.monotonic() + 45
    budget = 160

    def request(path: str, payload: Dict[str, Any], post: bool = False) -> Dict[str, Any]:
        nonlocal budget, complete
        remaining = deadline - time.monotonic()
        if remaining <= 0 or budget <= 0:
            complete = False
            return {}
        budget -= 1
        try:
            if post:
                response = notion_post_json(
                    f"{NOTION_API_BASE}/{path}", payload,
                    timeout=max(3, min(8, int(remaining))), retries=1,
                )
            else:
                notion_request_slot()
                response = requests.get(
                    f"{NOTION_API_BASE}/{path}", headers=get_notion_headers(),
                    timeout=min(8, remaining),
                )
            if response.status_code != 200:
                complete = False
                return {}
            return dict(response.json())
        except (requests.RequestException, ValueError):
            complete = False
            return {}

    cursor = None
    seen_cursors: set[str] = set()
    for _ in range(5):
        payload: Dict[str, Any] = {"filter": {"property": "object", "value": "database"}, "page_size": 100}
        if cursor:
            payload["start_cursor"] = cursor
        data = request("search", payload, True)
        for database in data.get("results", []):
            if database.get("object") == "database" and not database.get("archived") and not database.get("in_trash"):
                parents[_id(database["id"])] = database.get("parent") or {}
                remember_notion_database_schema(database)
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
        if not cursor or cursor in seen_cursors:
            complete = False
            break
        seen_cursors.add(cursor)
    else:
        complete = False

    resolved: Dict[str, str] = dict(scope)

    def root_for(identifier: str, kind: str, trail: frozenset[str] = frozenset()) -> str:
        nonlocal complete
        identifier = _id(identifier)
        if identifier in resolved:
            return resolved[identifier]
        if identifier in trail or len(trail) >= 12:
            complete = False
            return ""
        if identifier not in parents:
            endpoint = {"page_id": "pages", "block_id": "blocks", "database_id": "databases"}.get(kind)
            if not endpoint:
                return ""
            data = request(f"{endpoint}/{identifier}", {})
            if not data or data.get("archived") or data.get("in_trash"):
                return ""
            parents[identifier] = data.get("parent") or {}
        parent = parents[identifier]
        parent_kind = str(parent.get("type") or "")
        if parent_kind == "workspace":
            resolved[identifier] = ""
        elif parent_kind in {"page_id", "block_id", "database_id"} and parent.get(parent_kind):
            resolved[identifier] = root_for(str(parent[parent_kind]), parent_kind, trail | {identifier})
        else:
            complete = False
            return ""
        return resolved[identifier]

    for database_id in list(parents):
        root = root_for(database_id, "database_id")
        if root:
            scope[database_id] = root
    return scope, complete


def refresh_notion_read_scope(roots: list[str], token: str) -> tuple[Dict[str, str], bool]:
    """Discover descendants for an explicit maintenance/index refresh."""
    normalized = tuple(_id(root) for root in roots)
    scope, complete = _scope_cached(normalized, token, int(time.monotonic() // 300))
    return dict(scope), complete


def resolve_notion_read_scope(roots: list[str], token: str) -> tuple[Dict[str, str], bool]:
    normalized = tuple(_id(root) for root in roots)
    if not child_database_search_enabled():
        return dict.fromkeys(normalized, ""), True
    persisted, complete = persisted_notion_read_scope(normalized)
    if persisted:
        return persisted, complete
    # User-facing searches must not spend up to 45 seconds rebuilding the
    # descendant inventory while holding the integration-wide request slot.
    # The maintenance script refreshes it explicitly; until then search only
    # configured roots and report that the child scope is incomplete.
    return {identifier: identifier for identifier in normalized}, False
