"""본문·속성 수집의 요청 예산. 대기 상태는 제목 조회와 같은 전송 계층을 쓴다."""
from __future__ import annotations
import time
from dataclasses import dataclass
from typing import Any
import requests
from backend.execution import bounded_timeout, interruptible_sleep
from backend.settings.notion import NOTION_API_BASE, get_notion_headers
from backend.notion.transport import (NotionRateLimited, notion_request_slot, observe_notion_response,
                                      wait_for_notion_retry)


class NotionReadFailure(Exception):
    def __init__(self, reason: str, *, retryable: bool | None = None):
        super().__init__(reason)
        self.retryable = reason not in {'time_limit', 'request_limit'} if retryable is None else retryable


@dataclass
class CrawlBudget:
    deadline: float
    requests_left: int


def _wait_for_retry_after(budget: CrawlBudget) -> None:
    try:
        wait_for_notion_retry(budget.deadline, sleep=interruptible_sleep)
    except NotionRateLimited as exc:
        raise NotionReadFailure(str(exc), retryable=False) from exc


def request_json(path: str, budget: CrawlBudget, *, payload: dict[str, Any] | None = None,
                 post: bool = False) -> dict[str, Any]:
    for attempt in range(3):
        if time.monotonic() >= budget.deadline:
            raise NotionReadFailure('time_limit')
        if budget.requests_left <= 0:
            raise NotionReadFailure('request_limit')
        _wait_for_retry_after(budget)
        try:
            notion_request_slot(deadline=budget.deadline)
        except NotionRateLimited as exc:
            raise NotionReadFailure(str(exc), retryable=False) from exc
        remaining = budget.deadline - time.monotonic()
        if remaining <= 0:
            raise NotionReadFailure('time_limit')
        budget.requests_left -= 1
        reason = 'request_failed'
        try:
            kwargs: dict[str, Any] = {'headers': get_notion_headers(), 'timeout': bounded_timeout(min(15, remaining))}
            response = (requests.post(f'{NOTION_API_BASE}/{path}', json=payload or {}, **kwargs)
                        if post else requests.get(f'{NOTION_API_BASE}/{path}', params=payload or {}, **kwargs))
            try:
                status = response.status_code
                observe_notion_response(response, attempt)
                if status == 200:
                    data = response.json()
                    if not isinstance(data, dict):
                        raise NotionReadFailure('invalid_response')
                    return data
                reason = f'http_{status}'
                if status not in {429, 529} and status < 500:
                    raise NotionReadFailure(reason)
                if status in {429, 529}:
                    if attempt == 2:
                        raise NotionReadFailure(reason)
                    _wait_for_retry_after(budget)
                    continue
            finally:
                response.close()
        except requests.RequestException as exc:
            if attempt == 2:
                raise NotionReadFailure(reason) from exc
        except (TypeError, ValueError) as exc:
            raise NotionReadFailure('invalid_response') from exc
        if attempt == 2:
            raise NotionReadFailure(reason)
        delay = .5 * 2**attempt
        if time.monotonic() + delay >= budget.deadline:
            raise NotionReadFailure('time_limit')
        interruptible_sleep(delay)
    raise NotionReadFailure('request_failed')


_ReadFailure = NotionReadFailure
_Budget = CrawlBudget
_request = request_json
