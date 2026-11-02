"""Notion 전송의 요청 간격·429 대기·재시도·실행 예산을 한 곳에서 소유한다."""
import os
import random
import threading
import time
from typing import Any, Dict, Optional

import requests

from backend.execution import bounded_timeout, interruptible_sleep, execution_slot
from backend.settings.notion import get_notion_headers


_notion_request_lock = threading.Lock()
_notion_next_request_at = 0.0
_notion_default_request_interval = os.getenv(
    "NOTION_MIN_REQUEST_INTERVAL_SECONDS", "0.36"
)


def notion_request_slot() -> None:
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
            interruptible_sleep(wait_seconds)
            now = time.monotonic()
        _notion_next_request_at = now + interval


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
    return max(retry_after, exponential) + random.uniform(0.0, 0.15)


def notion_post_json(url: str, payload: Dict[str, Any], timeout: int = 45, retries: int = 2) -> requests.Response:
    # A single Notion tool call must not monopolize the whole chat request.
    # Callers may request a larger timeout/retry count, but the orchestration
    # budget caps each network attempt. Chat orchestration can issue more than
    # one Notion query. 정확 제목 조회의 간헐적인 8초 초과를 흡수하되 전체 요청을
    # 장시간 점유하지 않도록 기본 예산은 12초, 네트워크/429/5xx 재시도 1회로 제한한다.
    global _notion_next_request_at
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
            if response.status_code == 429:
                # 재시도 소진 후에도 다른 DB/스레드가 Retry-After 동안 요청하지 않는다.
                delay = _notion_retry_delay(response, attempt)
                with execution_slot(_notion_request_lock):
                    _notion_next_request_at = max(_notion_next_request_at, time.monotonic() + delay)
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
