"""Notion HTTP 조회와 TTL 캐시를 담당한다."""

import logging
import os
import time
from functools import lru_cache
from threading import Event, Lock, Thread
from typing import Any, Dict, List

import requests
from fastapi import HTTPException

from backend.execution import bounded_timeout
from backend.notion.schema_cache import load_schema, save_schema
from backend.config import (
    NOTION_API_BASE,
    get_notion_headers as get_notion_headers,
    get_notion_settings as get_notion_settings,
    notion_post_json as notion_post_json,
    notion_request_slot,
)


LOGGER = logging.getLogger(__name__)
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


def fetch_notion_relation_title(page_id: str) -> str:
    return _lookup_name("pages", page_id)


def fetch_notion_user_name(user_id: str) -> str:
    return _lookup_name("users", user_id)


def _lookup_name(kind: str, identifier: str) -> str:
    if not identifier:
        return ""
    try:
        return _lookup_name_cached(
            kind, identifier, get_notion_headers().get("Authorization", ""), int(time.monotonic() // 300),
        )
    except (requests.RequestException, LookupError, ValueError):
        return ""


@lru_cache(maxsize=1024)
def _lookup_name_cached(kind: str, identifier: str, token: str, bucket: int) -> str:
    # 실패는 예외로 전달해야 lru_cache가 빈 값을 저장하지 않는다.
    notion_request_slot()
    response = requests.get(
        f"{NOTION_API_BASE}/{kind}/{identifier}",
        headers={**get_notion_headers(), "Authorization": token}, timeout=bounded_timeout(20),
    )
    if response.status_code != 200:
        raise LookupError("Notion 이름 조회 실패")
    data = response.json()
    value = notion_title(data.get("properties", {})) if kind == "pages" else notion_user_name(data)
    if not value:
        raise LookupError("Notion 이름 없음")
    return value


fetch_notion_relation_title.cache_clear = _lookup_name_cached.cache_clear  # type: ignore[attr-defined]
fetch_notion_user_name.cache_clear = _lookup_name_cached.cache_clear  # type: ignore[attr-defined]


NOTION_CONTENT_INCOMPLETE = "[본문 일부 미확인: 하위 문서 또는 조회 한도 때문에 전체 내용을 읽지 못했습니다.]"


def fetch_notion_page_blocks(page_id: str, max_blocks: int = 30) -> str:
    return str(fetch_notion_page_content(page_id, max_blocks)["text"])


def fetch_notion_page_content(page_id: str, max_blocks: int = 30) -> Dict[str, Any]:
    """동일한 원본 순회에서 본문 문자열과 유형·소유 문서를 보존한 근거를 반환한다."""
    if os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() in {"0", "false", "no"}:
        return {"text": "", "segments": []}
    content = _page_content_cached(
        page_id, max_blocks, get_notion_headers().get("Authorization", ""), int(time.monotonic() // 60),
    )
    # 호출자가 근거를 보강해도 다음 요청이 사용하는 원본 캐시는 바뀌지 않는다.
    return {"text": content["text"], "segments": [dict(segment) for segment in content["segments"]]}


@lru_cache(maxsize=256)
def _page_content_cached(page_id: str, max_blocks: int, token: str, bucket: int) -> Dict[str, Any]:
    """소유한 하위 블록/DB만 따라가며 총 블록·깊이·요청 수를 제한한다.

    relation, 링크, synced_from으로 연결된 다른 업무 영역은 따라가지 않는다.
    하위 문서의 제목과 URL을 함께 남겨 상위 문서와 근거를 구분한다.
    """
    if os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() in {"0", "false", "no"}:
        return {"text": "", "segments": []}
    remaining = max(0, min(max_blocks, 400))
    # 결과 본문은 여러 토글·표에 흩어질 수 있다. 블록 한도에 맞춰
    # 요청 수를 늘리되 총 60회와 기존 25초 마감 안에서만 읽는다.
    requests_left = min(60, max_blocks) if max_blocks > 30 else 3
    deadline = time.monotonic() + (25 if max_blocks > 30 else 4)
    seen: set[str] = set()
    texts: List[str] = []
    segments: List[Dict[str, str]] = []
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
                    params=payload, timeout=bounded_timeout(min(8, available)),
                )
            if response.status_code != 200:
                incomplete = True
                return {}
            return dict(response.json())
        except (requests.RequestException, ValueError):
            incomplete = True
            return {}

    def walk(
        identifier: str, depth: int, database: bool = False, parent_id: str = "", owner_page_id: str = "",
    ) -> None:
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
            children: List[tuple[str, int, bool, str, str]] = []
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
                    children.append((block_id, depth + 1, False, "", block_id))
                else:
                    kind = str(block.get("type") or "")
                    text = notion_block_text(block)
                    if text:
                        texts.append(text)
                        # 하위 문서/DB의 제목 표시는 본문 블록 근거가 아니다.
                        if kind not in {"child_page", "child_database"}:
                            segments.append({"block_id": block_id, "type": kind, "text": text,
                                             "page_id": owner_page_id, "owner_page_id": owner_page_id})
                    if kind == "child_database":
                        children.append((block_id, depth + 1, True, identifier, owner_page_id))
                    elif kind == "child_page":
                        texts.append(f"[하위 문서: {(block.get(kind) or {}).get('title', '')}] https://www.notion.so/{block_id.replace('-', '')}")
                        children.append((block_id, depth + 1, False, "", block_id))
                    elif block.get("has_children"):
                        if kind == "synced_block" and (block.get(kind) or {}).get("synced_from"):
                            incomplete = True
                        else:
                            children.append((block_id, depth + 1, False, "", owner_page_id))
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

    walk(page_id, 0, owner_page_id=page_id)
    if incomplete:
        texts.append(NOTION_CONTENT_INCOMPLETE)
    return {"text": "\n".join(text for text in texts if text).strip(), "segments": segments}


def _notion_cache_ttl() -> int:
    try:
        return max(30, min(int(os.getenv("NOTION_CACHE_TTL_SECONDS", "600")), 3600))
    except ValueError:
        return 600


def _notion_cache_bucket() -> int:
    return int(time.monotonic() // _notion_cache_ttl())


def _notion_schema_max_stale() -> int:
    # 스키마는 속성 추가·이름 변경 때만 바뀐다. 이 시간 안에서는 만료된 값을 즉시
    # 쓰고 백그라운드에서 다시 받아, 사용자 요청이 DB 수만큼의 조회를 기다리지 않는다.
    try:
        return max(0, min(int(os.getenv("NOTION_SCHEMA_MAX_STALE_SECONDS", "86400")), 604800))
    except ValueError:
        return 86400


_DATABASE_FAILURE_TTL_SECONDS = 30
_database_cache_lock = Lock()
# (database_id, token) -> (받은 시각, DB 객체). 제목과 스키마가 같은 응답을 공유한다.
_database_cache: Dict[tuple[str, str], tuple[float, Dict[str, Any]]] = {}
_database_failures: Dict[tuple[str, str], tuple[float, Exception]] = {}
_database_refreshing: set[tuple[str, str]] = set()


def clear_notion_database_cache() -> None:
    with _database_cache_lock:
        _database_cache.clear()
        _database_failures.clear()
        _database_refreshing.clear()


def _request_notion_database(database_id: str) -> Dict[str, Any]:
    notion_request_slot()
    response = requests.get(
        f"{NOTION_API_BASE}/databases/{database_id}",
        headers=get_notion_headers(),
        timeout=bounded_timeout(20),
    )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Notion DB 정보를 불러오지 못했습니다: {response.status_code} {response.text[:300]}",
        )
    return dict(response.json())


def _load_notion_database(key: tuple[str, str]) -> Dict[str, Any]:
    try:
        data = _request_notion_database(key[0])
    except (HTTPException, requests.RequestException, ValueError) as exc:
        # 장애 중에는 같은 DB를 제목·스키마 조회마다 다시 기다리지 않도록 잠시 기억한다.
        with _database_cache_lock:
            _database_failures[key] = (time.monotonic() + _DATABASE_FAILURE_TTL_SECONDS, exc)
        raise
    with _database_cache_lock:
        _database_failures.pop(key, None)
        _database_cache[key] = (time.monotonic(), data)
        while len(_database_cache) > 256:
            _database_cache.pop(next(iter(_database_cache)))
    save_schema(key[0], get_notion_headers(), data)
    return data


def _refresh_notion_database_in_background(key: tuple[str, str]) -> None:
    with _database_cache_lock:
        if key in _database_refreshing:
            return
        failure = _database_failures.get(key)
        if failure and failure[0] > time.monotonic():
            return
        _database_refreshing.add(key)

    def refresh() -> None:
        try:
            _load_notion_database(key)
        except (HTTPException, requests.RequestException, ValueError) as exc:
            # 갱신 실패 시 만료 한도까지 기존 값을 계속 사용한다.
            LOGGER.warning("Notion DB 정보 갱신 실패: %s", type(exc).__name__)
        finally:
            with _database_cache_lock:
                _database_refreshing.discard(key)

    Thread(target=refresh, name="notion-database-refresh", daemon=True).start()


def fetch_notion_database_schema(database_id: str) -> Dict[str, Any]:
    seeded = _seed_schema(database_id)
    if seeded:
        return seeded
    # 설정의 하이픈 ID와 범위 조회의 정규화 ID가 같은 캐시를 쓰게 한다.
    key = (database_id.replace("-", "").lower(), get_notion_headers().get("Authorization", ""))
    now = time.monotonic()
    with _database_cache_lock:
        cached = _database_cache.get(key)
        failure = _database_failures.get(key)
    if not cached:
        persisted = load_schema(database_id, get_notion_headers(), max(_notion_cache_ttl(), _notion_schema_max_stale()))
        if persisted:
            age, data = persisted
            cached = (now - age, data)
            with _database_cache_lock:
                _database_cache[key] = cached
    if cached:
        age = now - cached[0]
        if age < _notion_cache_ttl():
            return cached[1]
        if age < _notion_schema_max_stale():
            _refresh_notion_database_in_background(key)
            return cached[1]
    if failure and failure[0] > now:
        raise failure[1]
    return _load_notion_database(key)


def fetch_notion_database_title(database_id: str) -> str:
    fallback = database_id[-8:] if database_id else "Notion DB"
    try:
        data = fetch_notion_database_schema(database_id)
    except (HTTPException, requests.RequestException, ValueError):
        return fallback
    return notion_rich_text_to_plain(data.get("title", [])) or fallback


def warm_notion_database_cache() -> int:
    """검색 대상 DB의 메타데이터를 사용자 요청 전에 채운다."""
    from backend.notion.read_scope import ensure_notion_read_scope

    settings = get_notion_settings()
    database_ids = list(settings.get("database_ids") or [])
    if not settings.get("token") or not database_ids:
        return 0
    read_scope, _complete = ensure_notion_read_scope(database_ids, settings["token"])
    warmed = 0
    for database_id in dict.fromkeys([*read_scope, *(root for root in read_scope.values() if root)]):
        try:
            fetch_notion_database_schema(database_id)
        except (HTTPException, requests.RequestException, ValueError):
            continue
        warmed += 1
    return warmed


def start_notion_cache_warmer(interval_seconds: int = 21600) -> Event:
    """시작 직후와 이후 주기적으로 캐시를 채워 첫 검색과 장시간 유휴 뒤 검색을 보호한다."""
    stop = Event()

    def run() -> None:
        while not stop.is_set():
            try:
                warmed = warm_notion_database_cache()
                LOGGER.info("Notion DB 정보 %d건을 미리 불러왔습니다.", warmed)
            except Exception as exc:
                LOGGER.warning("Notion DB 정보 사전 로딩 실패: %s", type(exc).__name__)
            stop.wait(interval_seconds)

    Thread(target=run, name="notion-cache-warmer", daemon=True).start()
    return stop


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
            timeout=bounded_timeout(20),
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
    "fetch_notion_page_content",
    "fetch_notion_relation_title",
    "fetch_notion_user_name",
    "fetch_notion_users",
    "get_notion_headers",
    "get_notion_settings",
    "notion_post_json",
]
