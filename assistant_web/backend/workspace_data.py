"""Fast, shared access to the local Mantis snapshot used by workspace screens."""

from __future__ import annotations

import os
import time
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

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
WORKSPACE_LIVE_TTL_SECONDS = 60


def fetch_workspace_recent_issues(
    total_limit: int = 1000, request_timeout: int = 8, *, force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """두 화면의 페이지 조회를 공유하고 동시 요청도 한 번만 실행한다.

    사용자별 응답은 저장하지 않는다. 같은 연결의 원본만 보관하고 호출자가
    권한/담당자 범위를 적용한다. 부분 실패한 세대는 전부 폐기한다.
    """
    global _live_key, _live_pages, _live_expires_at
    settings = get_mantis_settings()
    key = (settings["url"], settings["token"])
    limit = max(1, min(int(total_limit), 3000))
    with _live_lock:
        needs_larger_window = bool(
            _live_pages and len(_live_pages[-1]) == 100 and len(_live_pages) * 100 < limit
        )
        if force_refresh or key != _live_key or time.monotonic() >= _live_expires_at or needs_larger_window:
            # 페이지 번호 기반 API의 오래된 앞부분과 새 뒷부분을 합치면
            # 중간 등록/삭제로 중복·누락이 생길 수 있어 범위 확대는 다시 읽는다.
            _live_key, _live_pages = key, []
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
                        # map은 완료 순서가 아니라 페이지 번호 순서로 돌려준다.
                        for batch in pool.map(fetch_page, range(first_page, last_page + 1)):
                            _live_pages.append(batch)
                            if len(batch) < 100:
                                break
        except Exception:
            _live_key, _live_pages, _live_expires_at = None, [], 0.0
            raise
        rows = [issue for page in _live_pages for issue in page][:limit]
        return deepcopy(sorted(rows, key=lambda issue: int(issue.get("id") or 0), reverse=True))


def clear_workspace_live_cache() -> None:
    global _live_key, _live_pages, _live_expires_at
    with _live_lock:
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
    }
    return tuple(result.get("issues") or []), meta


def load_workspace_snapshot(limit: int = 1000) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return snapshot rows, reusing parsed data until either index file changes."""
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
