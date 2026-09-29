"""범위가 제한된 Mantis 페이지 수집과 원본 데이터 단기 캐시."""

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from threading import Lock
import time
from typing import Any, Callable, Generator

from fastapi import HTTPException

from backend.ordered_prefetch import iter_in_order


_lock = Lock()
_connection: tuple[str, str] | None = None
_cache: OrderedDict[tuple[int, int, int], tuple[float, list[dict[str, Any]]]] = OrderedDict()


class IssueBatch(list[dict[str, Any]]):
    """원본 목록과 같은 수집 세대의 범위/시각을 함께 전달한다."""

    def __init__(self, rows: list[dict[str, Any]], meta: dict[str, Any]):
        super().__init__(rows)
        self.meta = meta


def collection_metadata(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return dict(getattr(rows, "meta", {"search_complete": False, "incomplete_reasons": ["unknown_coverage"]}))


def clear_recent_cache() -> None:
    global _connection
    with _lock:
        _connection = None
        _cache.clear()


def _collect(
    fetch_page: Callable[[int], list[dict[str, Any]]],
    limit: int,
    page_size: int,
    workers: int,
) -> list[dict[str, Any]]:
    """API 응답 순서의 동일 범위를 수집한다. 전역 최신 N건을 뜻하지 않는다."""
    page_count = (limit + page_size - 1) // page_size
    rows: list[dict[str, Any]] = []
    fetched_at = datetime.now(timezone.utc).isoformat()
    complete = False
    pool = ThreadPoolExecutor(max_workers=workers)
    futures = [pool.submit(fetch_page, page) for page in range(1, page_count + 1)]
    try:
        # 페이지 순서로 결과를 읽으며 짧은 페이지까지의 오류는 전파한다.
        # 짧은 페이지를 확인한 뒤의 future는 읽지 않아 그 요청의 오류도 전파하지 않는다.
        for future in futures:
            batch = future.result()
            rows.extend(batch)
            if len(batch) < page_size:
                complete = len(rows) <= limit
                break
    finally:
        # 짧은 페이지/오류 이후 아직 시작하지 않은 요청을 취소한다.
        pool.shutdown(wait=True, cancel_futures=True)
    return IssueBatch(rows[:limit], {
        "search_complete": complete, "fetchedAt": fetched_at,
        "incomplete_reasons": [] if complete else ["page_limit"],
    })


def _collect_unique(
    fetch_page: Callable[[int], list[dict[str, Any]]], limit: int, page_size: int, workers: int,
) -> list[dict[str, Any]]:
    for _attempt in range(2):
        rows = _collect(fetch_page, limit, page_size, workers)
        ids = [int(row.get("id") or 0) for row in rows if row.get("id")]
        if len(ids) == len(set(ids)):
            return rows
    raise HTTPException(502, "Mantis 페이지가 조회 중 변경되어 결과를 확정하지 못했습니다.")


def iter_pages_in_order(
    fetch_page: Callable[[int], list[dict[str, Any]]],
    *,
    max_pages: int,
    workers: int,
) -> Generator[tuple[int, list[dict[str, Any]]], None, None]:
    """페이지를 1쪽부터 순서대로 내주면서 뒤 페이지를 workers개까지 미리 요청한다.

    종료 판단은 호출자가 페이지 순서대로 하므로 결과는 순차 수집과 같다. 호출자가
    멈추면 미리 받은 뒤 페이지의 결과와 오류는 버린다.
    """
    return iter_in_order(fetch_page, range(1, max_pages + 1), workers=workers)


def fetch_recent_cached(
    connection: tuple[str, str],
    fetch_page: Callable[[int], list[dict[str, Any]]],
    *,
    limit: int,
    page_size: int,
    timeout: int,
    workers: int,
    ttl: int,
    force_refresh: bool,
) -> list[dict[str, Any]]:
    """연결/범위별 캐시. API 순서의 복사된 원본에 호출자가 조건/정렬을 적용한다."""
    global _connection
    if ttl <= 0:
        # 캐시 비활성화 시 네트워크 작업 동안 전역 락을 잡지 않는다.
        # HTTP 동시 요청 상한은 fetch_mantis_issues의 세마포어가 적용한다.
        return _collect_unique(fetch_page, limit, page_size, workers)
    key = (limit, page_size, timeout)
    with _lock:
        if _connection != connection or force_refresh:
            _cache.clear()
            _connection = connection
        now = time.monotonic()
        for expired_key in list(_cache):
            if _cache[expired_key][0] <= now:
                del _cache[expired_key]
        cached = _cache.get(key)
        if ttl > 0 and cached is not None:
            _cache.move_to_end(key)
            return deepcopy(cached[1])
        _cache.pop(key, None)
        # 오래 걸린 조회를 완료 시각부터 새 데이터인 것처럼 연장하지 않는다.
        expires_at = now + ttl
        try:
            rows = _collect_unique(fetch_page, limit, page_size, workers)
        except Exception:
            _cache.clear()
            raise
        if ttl > 0 and time.monotonic() < expires_at:
            _cache[key] = (expires_at, deepcopy(rows))
            while len(_cache) > 4:
                _cache.popitem(last=False)
        return rows
