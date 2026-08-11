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
)


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


def fetch_notion_page_blocks(page_id: str, max_blocks: int = 30) -> str:
    if os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() in {"0", "false", "no"}:
        return ""

    blocks: List[Dict[str, Any]] = []
    cursor = None
    while len(blocks) < max_blocks:
        params = {"page_size": min(100, max_blocks - len(blocks))}
        if cursor:
            params["start_cursor"] = cursor
        try:
            response = requests.get(
                f"{NOTION_API_BASE}/blocks/{page_id}/children",
                headers=get_notion_headers(),
                params=params,
                timeout=20,
            )
        except requests.RequestException:
            break

        if response.status_code != 200:
            break
        data = response.json()
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return "\n".join(text for text in (notion_block_text(block) for block in blocks) if text).strip()


def _notion_cache_bucket() -> int:
    try:
        ttl_seconds = max(30, min(int(os.getenv("NOTION_CACHE_TTL_SECONDS", "600")), 3600))
    except ValueError:
        ttl_seconds = 600
    return int(time.monotonic() // ttl_seconds)


@lru_cache(maxsize=64)
def _fetch_notion_database_title_cached(database_id: str, _bucket: int) -> str:
    try:
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
    return _fetch_notion_database_title_cached(database_id, _notion_cache_bucket())


@lru_cache(maxsize=64)
def _fetch_notion_database_schema_cached(database_id: str, _bucket: int) -> Dict[str, Any]:
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
    return _fetch_notion_database_schema_cached(database_id, _notion_cache_bucket())


@lru_cache(maxsize=8)
def _fetch_notion_users_cached(_bucket: int) -> tuple[tuple[str, str], ...]:
    users: List[tuple[str, str]] = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
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
