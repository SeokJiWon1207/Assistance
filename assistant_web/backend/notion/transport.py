"""Notion 전송의 요청 간격·429 대기·재시도·실행 예산을 한 곳에서 소유한다."""
import os
import random
import threading
import time
from typing import Any, Dict, Optional

import requests

from backend.execution import bounded_timeout, interruptible_sleep, execution_slot
from backend.settings.notion import get_notion_headers
from backend.notion.rate_state import shared_delay


_notion_request_lock = threading.Lock()
_notion_next_request_at = 0.0
_notion_retry_after_until = 0.0
_notion_default_request_interval = os.getenv(
    "NOTION_MIN_REQUEST_INTERVAL_SECONDS", "0.36"
)


class NotionRateLimited(RuntimeError):
    """서버 대기를 지키면 현재 읽기 단계의 마감을 넘는다."""


def wait_for_notion_retry(deadline: float, *, sleep=None) -> None:
    delay, shared_retry = shared_delay()
    with execution_slot(_notion_request_lock):
        local = max(0.0, _notion_retry_after_until - time.monotonic())
    delay = max(local, delay if shared_retry else 0.0)
    if delay:
        if time.monotonic() + delay >= deadline:
            raise NotionRateLimited('http_429')
        (sleep or interruptible_sleep)(delay)


def observe_notion_response(response: requests.Response, attempt: int = 0) -> float:
    global _notion_next_request_at, _notion_retry_after_until
    if response.status_code not in {429, 529}:
        return 0.0
    delay = _notion_retry_delay(response, attempt)
    with execution_slot(_notion_request_lock):
        _notion_retry_after_until = max(_notion_retry_after_until, time.monotonic() + delay)
        _notion_next_request_at = max(_notion_next_request_at, _notion_retry_after_until)
    shared_delay(defer=delay)
    return delay


def notion_request_slot(*, deadline: float = float('inf')) -> None:
    """Serialize Notion calls at the integration's supported average rate."""
    global _notion_next_request_at
    try:
        interval = max(
            0.0,
            min(
                float(
                    os.getenv(
                        "NOTION_MIN_REQUEST_INTERVAL_SECONDS",
                        _notion_default_request_interval,
                    )
                ),
                2.0,
            ),
        )
    except ValueError:
        interval = 0.36
    with execution_slot(_notion_request_lock):
        now = time.monotonic()
        wait_seconds = max(0.0, _notion_next_request_at - now)
        if wait_seconds:
            if now + wait_seconds >= deadline:
                raise NotionRateLimited('http_429' if _notion_retry_after_until > now else 'time_limit')
            interruptible_sleep(wait_seconds)
            now = time.monotonic()
        _notion_next_request_at = now + interval
    while True:
        delay, retry = shared_delay(interval=interval)
        if not delay:
            return
        if time.monotonic() + delay >= deadline:
            raise NotionRateLimited('http_429' if retry else 'time_limit')
        interruptible_sleep(delay)


def _notion_retry_delay(response: requests.Response, attempt: int) -> float:
    retry_after = 0.0
    try:
        retry_after = float(response.headers.get("Retry-After", "0"))
    except (AttributeError, TypeError, ValueError):
        pass
    exponential = min(0.5 * (2**attempt), 4.0)
    import math
    if not math.isfinite(retry_after):
        retry_after = 0.0
    return max(retry_after, exponential) + (0.0 if retry_after > 0 else random.uniform(0.0, 0.15))


def notion_post_json(url: str, payload: Dict[str, Any], timeout: int = 45, retries: int = 2) -> requests.Response:
    # A single Notion tool call must not monopolize the whole chat request.
    # Callers may request a larger timeout/retry count, but the orchestration
    # budget caps each network attempt. Chat orchestration can issue more than
    # one Notion query. 정확 제목 조회의 간헐적인 8초 초과를 흡수하되 전체 요청을
    # 장시간 점유하지 않도록 기본 예산은 12초, 네트워크/429/5xx 재시도 1회로 제한한다.
    max_timeout = max(3, min(int(os.getenv("NOTION_TOOL_TIMEOUT_SECONDS", "12")), 45))
    max_retries = max(0, min(int(os.getenv("NOTION_TOOL_MAX_RETRIES", "1")), 2))
    timeout = min(timeout, max_timeout)
    retries = min(retries, max_retries)
    last_error: Optional[requests.RequestException] = None
    for attempt in range(max(1, retries + 1)):
        try:
            notion_request_slot()
            response = requests.post(
                url,
                headers=get_notion_headers(),
                json=payload,
                timeout=bounded_timeout(timeout),
            )
            if response.status_code in {429, 529}:
                # 재시도 소진 후에도 다른 DB/스레드가 Retry-After 동안 요청하지 않는다.
                observe_notion_response(response, attempt)
                if attempt < retries:
                    response.close()
                    continue
            elif response.status_code >= 500 and attempt < retries:
                delay = _notion_retry_delay(response, attempt)
                response.close()
                interruptible_sleep(delay)
                continue
            return response
        except requests.RequestException as exc:
            last_error = exc
            if attempt < retries:
                interruptible_sleep(min(0.5 * (2**attempt) + random.uniform(0.0, 0.15), 4.0))
    if last_error:
        raise last_error
    raise requests.RequestException("Notion request failed")
