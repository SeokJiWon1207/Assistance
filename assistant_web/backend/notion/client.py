"""Notion HTTP 조회와 TTL 캐시를 담당한다."""

import os
import time
from functools import lru_cache
from typing import Any, Dict, List

import requests
from fastapi import HTTPException

from backend.config import (
    NOTION_API_BASE,
    get_notion_headers as get_notion_headers,
    get_notion_settings as get_notion_settings,
    notion_post_json as notion_post_json,
    notion_request_slot,
)


_schema_seeds: Dict[tuple[str, str], tuple[float, Dict[str, Any]]] = {}


def remember_notion_database_schema(data: Dict[str, Any]) -> None:
    """범위 조회가 이미 받은 스키마를 재사용해 DB마다 같은 요청을 반복하지 않는다."""
    key = (str(data.get("id", "")).replace("-", ""), get_notion_headers().get("Authorization", ""))
    if key[0] and data.get("object") == "database" and "properties" in data:
        _schema_seeds[key] = (time.monotonic() + 300, data)
        while len(_schema_seeds) > 128:
            _schema_seeds.pop(next(iter(_schema_seeds)))


def _seed_schema(database_id: str) -> Dict[str, Any]:
    cached = _schema_seeds.get((database_id.replace("-", ""), get_notion_headers().get("Authorization", "")))
    return cached[1] if cached and cached[0] > time.monotonic() else {}


def notion_rich_text_to_plain(items: List[Dict[str, Any]]) -> str:
    from backend.notion.mapping import notion_rich_text_to_plain as convert
    return convert(items)

def notion_user_name(user: Dict[str, Any]) -> str:
    from backend.notion.mapping import notion_user_name as convert
    return convert(user)

def notion_title(properties: Dict[str, Any]) -> str:
    from backend.notion.mapping import notion_title as convert
    return convert(properties)

def notion_block_text(block: Dict[str, Any]) -> str:
    from backend.notion.mapping import notion_block_text as convert
    return convert(block)


@lru_cache(maxsize=512)
def fetch_notion_relation_title(page_id: str) -> str:
    """Resolve a related Notion page ID to its title for owner grouping."""
    if not page_id:
        return ""
    try:
        notion_request_slot()
        response = requests.get(
            f"{NOTION_API_BASE}/pages/{page_id}",
            headers=get_notion_headers(),
            timeout=20,
        )
    except requests.RequestException:
        return ""
    if response.status_code != 200:
        return ""
    return notion_title(response.json().get("properties", {}))


@lru_cache(maxsize=512)
def fetch_notion_user_name(user_id: str) -> str:
    if not user_id:
        return ""
    try:
        notion_request_slot()
        response = requests.get(
            f"{NOTION_API_BASE}/users/{user_id}",
            headers=get_notion_headers(),
            timeout=20,
        )
    except requests.RequestException:
        return ""
    if response.status_code != 200:
        return ""
    return notion_user_name(response.json())


NOTION_CONTENT_INCOMPLETE = "[본문 일부 미확인: 하위 문서 또는 조회 한도 때문에 전체 내용을 읽지 못했습니다.]"


def fetch_notion_page_blocks(page_id: str, max_blocks: int = 30) -> str:
    if os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() in {"0", "false", "no"}:
        return ""
    return _page_content_cached(page_id, max_blocks, get_notion_headers().get("Authorization", ""), int(time.monotonic() // 60))


@lru_cache(maxsize=256)
def _page_content_cached(page_id: str, max_blocks: int, token: str, bucket: int) -> str:
    """소유한 하위 블록/DB만 따라가며 총 블록·깊이·요청 수를 제한한다.

    relation, 링크, synced_from으로 연결된 다른 업무 영역은 따라가지 않는다.
    하위 문서의 제목과 URL을 함께 남겨 상위 문서와 근거를 구분한다.
    """
    if os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() in {"0", "false", "no"}:
        return ""
    remaining = max(0, min(max_blocks, 400))
    requests_left = 20 if max_blocks > 30 else 3
    deadline = time.monotonic() + (25 if max_blocks > 30 else 4)
    seen: set[str] = set()
    texts: List[str] = []
    incomplete = False

    def same_id(left: str, right: str) -> bool:
        return left.replace("-", "") == right.replace("-", "")

    def read(path: str, payload: Dict[str, Any], post: bool = False) -> Dict[str, Any]:
        nonlocal requests_left, incomplete
        available = deadline - time.monotonic()
        if requests_left <= 0 or available <= 0:
            incomplete = True
            return {}
        requests_left -= 1
        try:
            if post:
                response = notion_post_json(
                    f"{NOTION_API_BASE}/{path}", payload,
                    timeout=max(3, min(8, int(available))), retries=1,
                )
            else:
                notion_request_slot()
                response = requests.get(
                    f"{NOTION_API_BASE}/{path}", headers=get_notion_headers(),
                    params=payload, timeout=min(8, available),
                )
            if response.status_code != 200:
                incomplete = True
                return {}
            return dict(response.json())
        except (requests.RequestException, ValueError):
            incomplete = True
            return {}

    def walk(identifier: str, depth: int, database: bool = False, parent_id: str = "") -> None:
        nonlocal remaining, incomplete
        key = identifier.replace("-", "")
        if key in seen:
            return
        if depth > 5 or remaining <= 0:
            incomplete = True
            return
        seen.add(key)
        if database:
            schema = read(f"databases/{identifier}", {})
            parent = schema.get("parent") or {}
            parent_kind = parent.get("type")
            if parent_kind not in {"page_id", "block_id"} or not same_id(str(parent.get(str(parent_kind), "")), parent_id):
                incomplete = True
                return
        cursor = None
        cursors: set[str] = set()
        while remaining > 0:
            payload: Dict[str, Any] = {"page_size": min(100, remaining)}
            if cursor:
                payload["start_cursor"] = cursor
            if database:
                payload["sorts"] = [{"timestamp": "last_edited_time", "direction": "descending"}]
            data = read(f"databases/{identifier}/query" if database else f"blocks/{identifier}/children", payload, database)
            if not data:
                return
            rows = data.get("results") or []
            children: List[tuple[str, int, bool, str]] = []
            for block in rows:
                if remaining <= 0:
                    incomplete = True
                    break
                remaining -= 1
                block_id = str(block.get("id") or "")
                if database:
                    parent = block.get("parent") or {}
                    if not same_id(str(parent.get("database_id") or ""), identifier):
                        incomplete = True
                        continue
                    # 속성의 직접 텍스트도 읽되 관계형 속성의 목적지는 조회하지 않는다.
                    from backend.notion.mapping import notion_properties_to_text
                    properties = block.get("properties") or {}
                    texts.append(f"[하위 문서: {notion_title(properties)}] https://www.notion.so/{block_id.replace('-', '')}")
                    texts.append(notion_properties_to_text(properties))
                    children.append((block_id, depth + 1, False, ""))
                else:
                    text = notion_block_text(block)
                    if text:
                        texts.append(text)
                    kind = block.get("type")
                    if kind == "child_database":
                        children.append((block_id, depth + 1, True, identifier))
                    elif kind == "child_page":
                        texts.append(f"[하위 문서: {(block.get(kind) or {}).get('title', '')}] https://www.notion.so/{block_id.replace('-', '')}")
                        children.append((block_id, depth + 1, False, ""))
                    elif block.get("has_children"):
                        if kind == "synced_block" and (block.get(kind) or {}).get("synced_from"):
                            incomplete = True
                        else:
                            children.append((block_id, depth + 1, False, ""))
            for child in children:
                walk(*child)
            if not data.get("has_more"):
                return
            cursor = data.get("next_cursor")
            if not cursor or cursor in cursors:
                incomplete = True
                return
            cursors.add(cursor)
        incomplete = True

    walk(page_id, 0)
    if incomplete:
        texts.append(NOTION_CONTENT_INCOMPLETE)
    return "\n".join(text for text in texts if text).strip()


def _notion_cache_bucket() -> int:
    try:
        ttl_seconds = max(30, min(int(os.getenv("NOTION_CACHE_TTL_SECONDS", "600")), 3600))
    except ValueError:
        ttl_seconds = 600
    return int(time.monotonic() // ttl_seconds)


@lru_cache(maxsize=64)
def _fetch_notion_database_title_cached(database_id: str, _bucket: int, token: str = "") -> str:
    try:
        notion_request_slot()
        response = requests.get(
            f"{NOTION_API_BASE}/databases/{database_id}",
            headers=get_notion_headers(),
            timeout=15,
        )
    except requests.RequestException:
        return database_id[-8:] if database_id else "Notion DB"

    if response.status_code != 200:
        return database_id[-8:] if database_id else "Notion DB"

    data = response.json()
    title = notion_rich_text_to_plain(data.get("title", []))
    return title or (database_id[-8:] if database_id else "Notion DB")


def fetch_notion_database_title(database_id: str) -> str:
    cached = _seed_schema(database_id)
    if cached:
        return notion_rich_text_to_plain(cached.get("title", [])) or database_id[-8:]
    return _fetch_notion_database_title_cached(database_id, _notion_cache_bucket(), get_notion_headers().get("Authorization", ""))


@lru_cache(maxsize=64)
def _fetch_notion_database_schema_cached(database_id: str, _bucket: int, token: str = "") -> Dict[str, Any]:
    notion_request_slot()
    response = requests.get(
        f"{NOTION_API_BASE}/databases/{database_id}",
        headers=get_notion_headers(),
        timeout=20,
    )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Notion DB 정보를 불러오지 못했습니다: {response.status_code} {response.text[:300]}",
        )
    return response.json()


def fetch_notion_database_schema(database_id: str) -> Dict[str, Any]:
    return _seed_schema(database_id) or _fetch_notion_database_schema_cached(database_id, _notion_cache_bucket(), get_notion_headers().get("Authorization", ""))


@lru_cache(maxsize=8)
def _fetch_notion_users_cached(_bucket: int) -> tuple[tuple[str, str], ...]:
    users: List[tuple[str, str]] = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        notion_request_slot()
        response = requests.get(
            f"{NOTION_API_BASE}/users",
            headers=get_notion_headers(),
            params=params,
            timeout=20,
        )
        if response.status_code >= 400:
            return tuple(users)
        data = response.json()
        for user in data.get("results", []):
            name = notion_user_name(user)
            user_id = user.get("id")
            if name and user_id:
                users.append((name, user_id))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return tuple(users)


def fetch_notion_users() -> tuple[tuple[str, str], ...]:
    return _fetch_notion_users_cached(_notion_cache_bucket())


__all__ = [
    "fetch_notion_database_schema",
    "fetch_notion_database_title",
    "fetch_notion_page_blocks",
    "fetch_notion_relation_title",
    "fetch_notion_user_name",
    "fetch_notion_users",
    "get_notion_headers",
    "get_notion_settings",
    "notion_post_json",
]
