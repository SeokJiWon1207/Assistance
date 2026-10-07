"""Fast, shared access to the local Mantis snapshot used by workspace screens."""

from __future__ import annotations

import os
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Lock
from functools import lru_cache
from datetime import datetime, timezone
from fastapi import HTTPException
from backend.execution import checkpoint, execution_slot, wait_for_result
from backend.sources.mantis_collection import IssueBatch
from backend.sources.mantis_store import read_complete_store, start_store_refresh, store_path, connection_key, MAX_AGE_SECONDS, refresh_in_progress, read_store_metadata
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from backend.common import normalize_chroma_issue_document
from backend.config import CHROMA_DB_PATH
from backend.tools.mantis_index import INDEX_FILENAME
from backend.tools.mantis_snapshot import scan_mantis_documents
from backend.config import get_mantis_settings
from backend.sources.mantis import fetch_mantis_issues


_live_lock = Lock()
_live_key: tuple[str, str] | None = None
_live_pages: list[list[Dict[str, Any]]] = []
_live_expires_at = 0.0
_live_fetched_at = ""
WORKSPACE_LIVE_TTL_SECONDS = 60


def fetch_workspace_recent_issues(
    total_limit: int = 1000, request_timeout: int = 8, *, force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """두 화면의 페이지 조회를 공유하고 동시 요청도 한 번만 실행한다.

    사용자별 응답은 저장하지 않는다. 같은 연결의 원본만 보관하고 호출자가
    권한/담당자 범위를 적용한다. 부분 실패한 세대는 전부 폐기한다.
    """
    global _live_key, _live_pages, _live_expires_at, _live_fetched_at
    stored = None if force_refresh else read_complete_store(summary_only=True)
    if stored is not None:
        return stored
    settings = get_mantis_settings()
    key = (settings["url"], settings["token"])
    limit = max(1, min(int(total_limit), 3000))
    with execution_slot(_live_lock):
        needs_larger_window = bool(
            _live_pages and len(_live_pages[-1]) == 100 and len(_live_pages) * 100 < limit
        )
        if force_refresh or key != _live_key or time.monotonic() >= _live_expires_at or needs_larger_window:
            # 페이지 번호 기반 API의 오래된 앞부분과 새 뒷부분을 합치면
            # 중간 등록/삭제로 중복·누락이 생길 수 있어 범위 확대는 다시 읽는다.
            _live_key, _live_pages = key, []
            _live_fetched_at = datetime.now(timezone.utc).isoformat()
            _live_expires_at = time.monotonic() + WORKSPACE_LIVE_TTL_SECONDS
        try:
            if not _live_pages:
                _live_pages.append(fetch_mantis_issues(
                    {"page_size": 100, "page": 1}, timeout=request_timeout,
                ))
            if len(_live_pages[-1]) == 100 and len(_live_pages) * 100 < limit:
                def fetch_page(page: int) -> List[Dict[str, Any]]:
                    return fetch_mantis_issues({"page_size": 100, "page": page}, timeout=request_timeout)

                with ThreadPoolExecutor(max_workers=3) as pool:
                    while len(_live_pages) * 100 < limit and len(_live_pages[-1]) == 100:
                        first_page = len(_live_pages) + 1
                        last_page = min(first_page + 2, (limit + 99) // 100)
                        checkpoint()
                        futures = [pool.submit(copy_context().run, fetch_page, page)
                                   for page in range(first_page, last_page + 1)]
                        for future in futures:
                            batch = wait_for_result(future)
                            _live_pages.append(batch)
                            if len(batch) < 100:
                                break
        except Exception:
            _live_key, _live_pages, _live_expires_at = None, [], 0.0
            raise
        all_rows = [issue for page in _live_pages for issue in page]
        ids = [int(issue.get("id") or 0) for issue in all_rows]
        if len(ids) != len(set(ids)):
            _live_key, _live_pages, _live_expires_at = None, [], 0.0
            raise HTTPException(502, "Mantis 페이지가 변경되어 집계를 확정하지 못했습니다.")
        complete = bool(_live_pages and len(_live_pages[-1]) < 100 and len(all_rows) <= limit)
        rows = sorted(all_rows[:limit], key=lambda issue: int(issue.get("id") or 0), reverse=True)
        return IssueBatch(deepcopy(rows), {
            "status": "live", "fetchedAt": _live_fetched_at,
            "search_complete": complete, "incomplete_reasons": [] if complete else ["page_limit"],
        })


def clear_workspace_live_cache() -> None:
    global _live_key, _live_pages, _live_expires_at
    with execution_slot(_live_lock):
        _live_key, _live_pages, _live_expires_at = None, [], 0.0


def _signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


@lru_cache(maxsize=8)
def _load_snapshot_cached(
    database_path: str,
    database_mtime_ns: int,
    database_size: int,
    index_path: str,
    index_mtime_ns: int,
    index_size: int,
    index_mode: str,
    limit: int,
) -> Tuple[tuple[Dict[str, Any], ...], Dict[str, Any]]:
    del database_mtime_ns, database_size, index_mtime_ns, index_size, index_mode
    result = scan_mantis_documents(
        Path(database_path),
        matches=lambda _text: True,
        normalize=normalize_chroma_issue_document,
        example_limit=limit,
        scan_limit=max(limit, 3000),
        index_database_path=Path(index_path),
    )
    meta = {
        "status": "snapshot" if result.get("available") else "error",
        "reason": str(result.get("reason") or "")[:300],
        "matchedCount": int(result.get("matched_count") or 0),
        "search_complete": False,
        "incomplete_reasons": ["legacy_snapshot_window"],
    }
    return tuple(result.get("issues") or []), meta


class _FrozenIssue(dict):
    """집계에서 공유하는 읽기 전용 원본. 필요한 응답 행만 일반 dict로 복사한다."""
    def _deny(self, *args, **kwargs):
        raise TypeError("공유된 업무 원본은 수정할 수 없습니다.")

    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _deny

    def __deepcopy__(self, memo):
        return {key: deepcopy(value, memo) for key, value in self.items()}


def _freeze(value):
    if isinstance(value, dict):
        return _FrozenIssue({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@lru_cache(maxsize=1)
def _complete_workspace_cached(path: str, mtime: int, size: int, identity: str):
    del path, mtime, size, identity
    from backend.sources.mantis import normalize_issue
    stored = read_complete_store(summary_only=True)
    if stored is None:
        # lru_cache는 예외를 저장하지 않으므로 일시적인 읽기 실패는 바로 복구된다.
        raise LookupError("전체 업무 원본을 읽지 못했습니다.")
    return tuple(_freeze(normalize_issue(issue)) for issue in stored), stored.meta


def complete_store_expired() -> bool:
    meta = read_store_metadata(allow_stale=True)
    return bool(meta is not None and not meta["search_complete"])


def load_complete_workspace(*, force_refresh: bool = False, predicate: Callable | None = None):
    path = store_path()
    if not path.exists():
        return None
    if force_refresh:
        start_store_refresh()
        # 전체 수집은 원자적으로 교체된다. 유효한 전체 저장본을 유지하면서
        # 갱신 상태를 알리고, 제한된 최근 페이지로 전체 집계를 대체하지 않는다.
    try:
        identity = connection_key()
        rows, saved_meta = _complete_workspace_cached(str(path), *_signature(path), identity)
    except (HTTPException, LookupError):
        return None
    age = time.time() - datetime.fromisoformat(saved_meta["fetchedAt"]).timestamp()
    if not 0 <= age < MAX_AGE_SECONDS:
        return None
    meta = {**saved_meta, "refreshPending": refresh_in_progress(), "search_complete": True,
            "incomplete_reasons": [], "reason": ""}
    # 집계는 읽기 전용 원본 참조만 전달하고, 개인 업무는 담당자 조건 이후에만 복사한다.
    meta["cacheUntil"] = datetime.fromtimestamp(
        min(time.time() + WORKSPACE_LIVE_TTL_SECONDS, datetime.fromisoformat(meta["fetchedAt"]).timestamp() + MAX_AGE_SECONDS),
        timezone.utc,
    ).isoformat()
    selected = [deepcopy(row) for row in rows if predicate(row)] if predicate else list(rows)
    return selected, {**meta, "matchedCount": len(selected)}


def load_workspace_snapshot(limit: int = 1000) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return snapshot rows, reusing parsed data until either index file changes."""
    stored = load_complete_workspace()
    if stored is not None:
        return stored
    if complete_store_expired():
        return [], {"status": "expired", "search_complete": False, "reason": "저장 데이터가 만료되어 실시간 조회가 필요합니다."}
    bounded_limit = max(100, min(int(limit), 3000))
    database_path = Path(CHROMA_DB_PATH)
    index_path = Path(os.getenv("MANTIS_INDEX_DB_PATH", str(database_path.parent / INDEX_FILENAME))).expanduser()
    database_signature = _signature(database_path)
    index_signature = _signature(index_path)
    issues, meta = _load_snapshot_cached(
        str(database_path),
        *database_signature,
        str(index_path),
        *index_signature,
        os.getenv("MANTIS_INDEX_MODE", "shadow").strip().lower(),
        bounded_limit,
    )
    return list(issues), dict(meta)


def clear_workspace_snapshot_cache() -> None:
    _load_snapshot_cached.cache_clear()
    _complete_workspace_cached.cache_clear()
