"""채팅의 공통 마감 시각과 협력적 취소. 외부 연결·웹 계층에 의존하지 않는다."""

import os
import time
from _thread import LockType
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import Future, TimeoutError as FutureTimeoutError, wait
from dataclasses import dataclass, field
from functools import wraps
from threading import Event, Lock
from typing import Any, Iterator, TypeVar


class RequestStopped(RuntimeError):
    def __init__(self, *, cancelled: bool = False):
        self.status_code = 499 if cancelled else 504
        self.detail = "요청이 취소되었습니다." if cancelled else "요청 처리 시간이 초과되었습니다. 범위를 줄여 다시 요청해 주세요."
        super().__init__(self.detail)


@dataclass
class ExecutionBudget:
    deadline: float
    cancelled: Event = field(default_factory=Event)
    _workers: set[Future[Any]] = field(default_factory=set, init=False, repr=False)
    _lock: LockType = field(default_factory=Lock, init=False, repr=False)

    @classmethod
    def create(cls) -> "ExecutionBudget":
        try:
            seconds = float(os.getenv("CHAT_REQUEST_TIMEOUT_SECONDS", "180"))
        except ValueError:
            seconds = 180.0
        return cls(time.monotonic() + max(1.0, min(seconds, 600.0)))

    def remaining(self) -> float:
        if self.cancelled.is_set():
            raise RequestStopped(cancelled=True)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RequestStopped()
        return remaining

    def cancel(self) -> None:
        self.cancelled.set()

    def track(self, future: Future[Any]) -> None:
        with self._lock:
            self._workers.add(future)
        future.add_done_callback(self._finished)

    def _finished(self, future: Future[Any]) -> None:
        with self._lock:
            self._workers.discard(future)

    def wait_for_workers(self) -> None:
        """먼저 반환한 검색의 선실행 작업까지 끝나야 요청 슬롯을 반환한다."""
        while True:
            with self._lock:
                pending = tuple(self._workers)
            if not pending:
                return
            wait(pending)


CURRENT_BUDGET: ContextVar[ExecutionBudget | None] = ContextVar("execution_budget", default=None)
T = TypeVar("T")


@contextmanager
def execution_scope(budget: ExecutionBudget | None = None) -> Iterator[ExecutionBudget]:
    owned = budget is None and CURRENT_BUDGET.get() is None
    active = budget or CURRENT_BUDGET.get() or ExecutionBudget.create()
    token = CURRENT_BUDGET.set(active)
    try:
        active.remaining()
        yield active
    finally:
        CURRENT_BUDGET.reset(token)
        if owned:
            active.cancel()
            active.wait_for_workers()


def checkpoint() -> None:
    budget = CURRENT_BUDGET.get()
    if budget is not None:
        budget.remaining()


def wait_for_result(future: Future[T]) -> T:
    while True:
        checkpoint()
        try:
            result = future.result(timeout=bounded_timeout(0.1))
        except FutureTimeoutError:
            if future.done():
                raise
            continue
        checkpoint()
        return result


def bounded_timeout(seconds: float) -> float:
    budget = CURRENT_BUDGET.get()
    return min(seconds, budget.remaining()) if budget is not None else seconds


def interruptible_sleep(seconds: float) -> None:
    budget = CURRENT_BUDGET.get()
    if budget is None:
        time.sleep(seconds)
        return
    budget.cancelled.wait(min(seconds, budget.remaining()))
    budget.remaining()


@contextmanager
def execution_slot(lock) -> Iterator[None]:
    """외부 요청 슬롯·공유 캐시 잠금 대기도 요청 예산에 포함한다."""
    while not lock.acquire(timeout=bounded_timeout(0.1)):
        checkpoint()
    try:
        checkpoint()
        yield
    finally:
        lock.release()


def request_execution(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        with execution_scope():
            result = fn(*args, **kwargs)
            checkpoint()
            return result
    return run
