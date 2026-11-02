"""본문·속성 수집이 공유하는 요청 예산과 재시도 상태."""
from __future__ import annotations
import math
import threading
import time
from dataclasses import dataclass
from typing import Any
import requests
from backend.execution import bounded_timeout, interruptible_sleep
from backend.settings.notion import NOTION_API_BASE, get_notion_headers
from backend.notion.transport import notion_request_slot

class NotionReadFailure(Exception):
    pass


@dataclass
class CrawlBudget:
    deadline: float
    requests_left: int


_retry_after_until = 0.0


_retry_after_lock = threading.Lock()


def _wait_for_retry_after(budget: CrawlBudget) -> None:
    # 페이지 예산이 끝나도 같은 프로세스의 다음 요청은 서버 대기를 지킨다.
    while True:
        with _retry_after_lock:
            until = _retry_after_until
        delay = until - time.monotonic()
        if delay <= 0:
            return
        if until >= budget.deadline:
            raise NotionReadFailure("http_429")
        interruptible_sleep(delay)


def request_json(path: str, budget: CrawlBudget, *, payload: dict[str, Any] | None = None,
             post: bool = False) -> dict[str, Any]:
    """모든 시도는 공유 요청 간격과 현재 실행의 남은 예산을 지킨다."""
    global _retry_after_until
    for attempt in range(3):
        remaining = budget.deadline - time.monotonic()
        if remaining <= 0:
            raise NotionReadFailure("time_limit")
        if budget.requests_left <= 0:
            raise NotionReadFailure("request_limit")
        _wait_for_retry_after(budget)
        budget.requests_left -= 1
        notion_request_slot()
        remaining = budget.deadline - time.monotonic()
        if remaining <= 0:
            raise NotionReadFailure("time_limit")
        try:
            kwargs: dict[str, Any] = {"headers": get_notion_headers(), "timeout": bounded_timeout(min(15, remaining))}
            response = (
                requests.post(f"{NOTION_API_BASE}/{path}", json=payload or {}, **kwargs)
                if post else requests.get(f"{NOTION_API_BASE}/{path}", params=payload or {}, **kwargs)
            )
            status = response.status_code
            if status == 200:
                data = response.json()
                if not isinstance(data, dict):
                    raise NotionReadFailure("invalid_response")
                return data
            if status != 429 and status < 500:
                raise NotionReadFailure(f"http_{status}")
            try:
                retry_after = float(response.headers.get("Retry-After", "0"))
            except (AttributeError, TypeError, ValueError):
                retry_after = 0
            if not math.isfinite(retry_after):
                retry_after = 0
            if status == 429:
                with _retry_after_lock:
                    _retry_after_until = max(
                        _retry_after_until, time.monotonic() + max(retry_after, 0.5 * 2**attempt))
            if attempt == 2:
                raise NotionReadFailure(f"http_{status}")
        except requests.RequestException as exc:
            if attempt == 2:
                raise NotionReadFailure("request_failed") from exc
            retry_after = 0
        except (TypeError, ValueError) as exc:
            raise NotionReadFailure("invalid_response") from exc
        # 서버가 지정한 대기 시간을 줄이면 같은 제한을 연속 소진한다.
        if not math.isfinite(retry_after):
            retry_after = 0
        delay = max(retry_after, 0.5 * 2**attempt)
        if time.monotonic() + delay >= budget.deadline:
            raise NotionReadFailure(f"http_{status}" if retry_after > 0 else "time_limit")
        interruptible_sleep(delay)
    raise NotionReadFailure("request_failed")


_ReadFailure = NotionReadFailure
_Budget = CrawlBudget
_request = request_json
