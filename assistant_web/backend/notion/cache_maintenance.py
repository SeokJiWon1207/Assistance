"""앱 시작과 주기 실행에서 스키마·목록 캐시를 갱신한다."""
from backend.notion import read_api
import logging
import os
import time
from threading import Event, Lock, Thread
import requests
from fastapi import HTTPException
from backend.notion.discovery import resolve_notion_settings
from backend.notion.read_api import fetch_notion_database_schema as fetch_notion_database_schema
LOGGER = logging.getLogger("backend.notion.client")
_request_lock = Lock()
_refresh_running = False
_retry_after = 0.0
_retry_needed = Event()


def retry_pending_catalog_refresh() -> None:
    # DB 목록 행이 이미 갱신됐어도 하위 페이지 검증 실패가 남아 있을 수 있다.
    if _retry_needed.is_set():
        request_catalog_refresh()


def request_catalog_refresh() -> None:
    """동시에 들어온 검색은 한 번만 깨운다. 실제 갱신은 운영 타이머와 같은 파일 잠금을 쓴다."""
    global _refresh_running, _retry_after
    if os.getenv("NOTION_CACHE_WARM_ENABLED", "true").strip().lower() != "true":
        return

    def refresh() -> None:
        global _refresh_running, _retry_after
        retry = True
        try:
            from backend.notion.snapshot_maintenance import refresh_catalog_cache
            result = refresh_catalog_cache()
            retry = result["status"] not in {"fresh", "refreshed", "skipped"}
            LOGGER.info("검색에서 요청한 Notion 목록 갱신: %s", result["status"])
        except Exception:
            LOGGER.exception("검색에서 요청한 Notion 목록을 갱신하지 못했습니다.")
        finally:
            with _request_lock:
                if retry:
                    _retry_needed.set()
                else:
                    _retry_needed.clear()
                _refresh_running = False
                _retry_after = time.monotonic() + 60

    with _request_lock:
        if _refresh_running or time.monotonic() < _retry_after:
            return
        _refresh_running = True
        try:
            Thread(target=refresh, name="notion-catalog-recovery", daemon=True).start()
        except RuntimeError:
            _refresh_running = False
            _retry_after = time.monotonic() + 60
            _retry_needed.set()
            LOGGER.exception("Notion 목록 갱신 작업을 시작하지 못했습니다.")


def warm_notion_database_cache() -> int:
    """검색 대상 DB의 메타데이터를 사용자 요청 전에 채운다."""
    from backend.notion.read_scope import ensure_notion_read_scope

    settings = resolve_notion_settings()
    database_ids = list(settings.get("database_ids") or [])
    if not settings.get("token") or not database_ids:
        return 0
    read_scope, _complete = ensure_notion_read_scope(database_ids, settings["token"])
    warmed = 0
    for database_id in dict.fromkeys([*read_scope, *(root for root in read_scope.values() if root)]):
        try:
            read_api.fetch_notion_database_schema(database_id)
        except (HTTPException, requests.RequestException, ValueError):
            continue
        warmed += 1
    return warmed


def start_notion_cache_warmer(interval_seconds: int = 3600) -> Event:
    """시작 직후와 이후 주기적으로 캐시를 채워 첫 검색과 장시간 유휴 뒤 검색을 보호한다."""
    stop = Event()

    def run() -> None:
        while not stop.is_set():
            try:
                warmed = warm_notion_database_cache()
                LOGGER.info("Notion DB 정보 %d건을 미리 불러왔습니다.", warmed)
                from backend.notion.snapshot_maintenance import refresh_catalog_cache
                result = refresh_catalog_cache()
                LOGGER.info("Notion 제목·속성 목록 사전 갱신: %s", result['status'])
                from backend.notion.semantic_index import sync_semantic_index
                semantic = sync_semantic_index(max_chunks=256, max_seconds=30)
                LOGGER.info("Notion 본문 의미 색인 증분 갱신: %s", semantic['status'])
            except Exception as exc:
                LOGGER.warning("Notion DB 정보 사전 로딩 실패: %s", type(exc).__name__)
            from backend.notion.snapshot_state import catalog_max_age_seconds
            stop.wait(min(interval_seconds, catalog_max_age_seconds() / 2))

    Thread(target=run, name="notion-cache-warmer", daemon=True).start()
    return stop
