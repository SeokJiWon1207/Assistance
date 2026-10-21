"""유사 비교 한 요청에서만 쓰는 짧은 원문 갱신 예산과 원문 재사용."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, Callable

import requests

from backend.execution import checkpoint


@dataclass
class RefreshBudget:
    total_seconds: float = 5.0
    request_seconds: float = 1.5
    spent_seconds: float = 0.0
    cache: dict[int, Any] = field(default_factory=dict)
    cache_hits: int = 0


_CURRENT: ContextVar[RefreshBudget | None] = ContextVar('mantis_short_refresh', default=None)


@contextmanager
def short_refresh_scope(*, total_seconds: float = 5.0, request_seconds: float = 1.5):
    current = _CURRENT.get()
    token = _CURRENT.set(current or RefreshBudget(total_seconds, request_seconds))
    try:
        yield _CURRENT.get()
    finally:
        _CURRENT.reset(token)


def fetch_with_refresh_budget(issue_id: int, fetch: Callable):
    budget = _CURRENT.get()
    checkpoint()
    if budget is None:
        return fetch(20, 3)
    if issue_id in budget.cache:
        budget.cache_hits += 1
        return deepcopy(budget.cache[issue_id])
    remaining = budget.total_seconds - budget.spent_seconds
    if remaining <= 0:
        raise requests.Timeout('comparison refresh budget exhausted')
    started = monotonic()
    try:
        # 일반 상세 조회는 유지하고 비교 후보의 추가 갱신만 짧게 제한한다.
        row = fetch(min(budget.request_seconds, remaining), 1)
        checkpoint()
        budget.cache[issue_id] = deepcopy(row)
        return row
    finally:
        budget.spent_seconds += max(0.0, monotonic() - started)
