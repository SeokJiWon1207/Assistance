"""연결된 Notion DB 탐색과 토큰별 성공·실패 캐시."""
from backend.notion import transport
import os
import threading
import time
from typing import Any, Dict, List, Tuple

import requests

from backend.settings.notion import NOTION_API_BASE, get_configured_notion_settings
from backend.notion.transport import notion_post_json as notion_post_json


NOTION_DISCOVERY_MAX_PAGES = 10
NOTION_DISCOVERY_FAILURE_TTL_SECONDS = 30
_notion_discovery_lock = threading.Lock()
_notion_discovery_cache: Dict[str, Tuple[float, List[str], str, str]] = {}


def notion_discovery_ttl_seconds() -> int:
    try:
        return max(30, min(int(os.getenv("NOTION_CACHE_TTL_SECONDS", "600")), 3600))
    except ValueError:
        return 600


def notion_discovery_max_databases() -> int:
    # 질문마다 DB별로 스키마·제목·본문 조회를 하므로 DB 수가 곧 요청 수다.
    # 상위 페이지를 연결해 수백 개 DB가 잡히면 한 질문에 수천 번 호출한다.
    try:
        return max(1, min(int(os.getenv("NOTION_DISCOVERY_MAX_DATABASES", "20")), 200))
    except ValueError:
        return 20


def discover_notion_database_ids(token: str) -> Tuple[List[str], str, str]:
    """API 키의 integration에 연결된 DB를 Notion 검색 API로 찾는다.

    ``NOTION_DATABASE_IDS``를 비워 두면 연결(Connections)된 DB 전체가 검색
    대상이다. 설정 조회가 요청마다 여러 번 일어나므로 결과를 캐시하고,
    실패도 짧게 캐시해 Notion 장애 때 요청마다 재시도하지 않는다.
    반환값은 (DB ID 목록, 오류 메시지, 오류 종류 dependency|configuration)이다.
    """
    now = time.monotonic()
    with _notion_discovery_lock:
        cached = _notion_discovery_cache.get(token)
        if cached and cached[0] > now:
            return list(cached[1]), cached[2], cached[3]

    database_ids: List[str] = []
    error = ""
    error_kind = ""
    has_more = False
    payload: Dict[str, Any] = {
        "filter": {"property": "object", "value": "database"},
        "page_size": 100,
    }
    try:
        for _ in range(NOTION_DISCOVERY_MAX_PAGES):
            response = transport.notion_post_json(f"{NOTION_API_BASE}/search", payload, timeout=20)
            if response.status_code >= 400:
                error = f"Notion DB 목록 조회 실패: {response.status_code} {response.text[:300]}"
                error_kind = "dependency"
                break
            data = response.json()
            database_ids.extend(
                str(item.get("id") or "").replace("-", "")
                for item in data.get("results", [])
                if item.get("object") == "database"
                and item.get("id")
                and not item.get("archived")
                and not item.get("in_trash")
            )
            next_cursor = data.get("next_cursor")
            has_more = bool(data.get("has_more") and next_cursor)
            if not has_more:
                break
            payload["start_cursor"] = next_cursor
    except (requests.RequestException, ValueError) as exc:
        error = f"Notion DB 목록 조회 실패: {type(exc).__name__}"
        error_kind = "dependency"

    database_ids = list(dict.fromkeys(database_ids))
    max_databases = notion_discovery_max_databases()
    if not error and (has_more or len(database_ids) > max_databases):
        count = f"{len(database_ids)}개 이상" if has_more else f"{len(database_ids)}개"
        error = (
            f"API 키에 연결된 Notion DB가 {count}로 자동 검색 한도({max_databases}개)를 넘습니다. "
            "검색할 DB에만 integration을 연결하거나 NOTION_DATABASE_IDS에 DB ID를 지정하세요."
        )
        error_kind = "configuration"
    if error:
        # 일부 목록이나 임의로 자른 목록으로 '전체 연결 DB'를 대신하지 않는다.
        database_ids = []
    ttl = NOTION_DISCOVERY_FAILURE_TTL_SECONDS if error_kind == "dependency" else notion_discovery_ttl_seconds()
    with _notion_discovery_lock:
        _notion_discovery_cache[token] = (time.monotonic() + ttl, database_ids, error, error_kind)
    return list(database_ids), error, error_kind


def resolve_notion_settings() -> Dict[str, Any]:
    """명시 DB가 없고 토큰이 있으면 기존 정책으로 연결된 DB를 탐색한다."""
    settings = get_configured_notion_settings()
    if settings["token"] and not settings["database_ids"]:
        database_ids, error, error_kind = discover_notion_database_ids(settings["token"])
        settings.update(database_ids=database_ids, database_source="discovered",
                        database_discovery_error=error, database_discovery_error_kind=error_kind)
    return settings


def notion_is_configured() -> bool:
    settings = resolve_notion_settings()
    return bool(settings["token"] and settings["database_ids"])
