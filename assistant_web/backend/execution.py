"""채팅의 공통 마감 시각과 협력적 취소. 외부 연결·웹 계층에 의존하지 않는다."""

import os
import time
from _thread import LockType
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from concurrent.futures import Executor, Future, TimeoutError as FutureTimeoutError, wait
from dataclasses import dataclass, field
from functools import wraps
from threading import Event, Lock
from typing import Any, Callable, Iterator, TypeVar

T = TypeVar('T')


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
        with self._lock:
            self.cancelled.set()
            pending = tuple(self._workers)
        # cancel의 완료 콜백도 _lock을 쓰므로 잠금 밖에서 실행한다.
        for future in pending:
            future.cancel()

    def publish(self, action: Callable[[], T]) -> T:
        """취소와 확정 결과 반영 사이의 선후 관계를 하나의 잠금으로 정한다."""
        with self._lock:
            self.remaining()
            return action()

    def child(self, seconds: float) -> "ExecutionBudget":
        """부모 마감을 넘지 않고 취소·선실행 작업 추적을 공유하는 하위 예산."""
        child = ExecutionBudget(deadline=min(self.deadline, time.monotonic() + max(0.0, seconds)),
                                cancelled=self.cancelled)
        child._workers, child._lock = self._workers, self._lock
        return child

    def track(self, future: Future[Any]) -> None:
        with self._lock:
            self._workers.add(future)
            cancelled = self.cancelled.is_set()
        future.add_done_callback(self._finished)
        if cancelled:
            future.cancel()

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


class StepTimeout(TimeoutError):
    """하위 단계의 제한 시간만 지났고 요청 전체는 계속 실행할 수 있다."""


def run_with_deadline(seconds: float, action: Callable[[], T]) -> T:
    """요청 마감 안에서 한 단계에 더 짧은 제한을 둔다. 요청 취소·마감은 그대로 올린다."""
    parent = CURRENT_BUDGET.get()
    child = parent.child(seconds) if parent is not None else ExecutionBudget(time.monotonic() + max(0.0, seconds))
    try:
        with execution_scope(child):
            result = action()
            checkpoint()
            return result
    except RequestStopped:
        if parent is not None:
            parent.remaining()
        if child.cancelled.is_set():
            raise
        raise StepTimeout("단계 제한 시간을 초과했습니다.") from None


def submit_with_context(pool: Executor, action: Callable[..., T], *args, **kwargs) -> Future[T]:
    """요청 정책·마감·취소 문맥을 복사하고 대기/실행 중인 작업을 요청이 소유한다."""
    checkpoint()
    context = copy_context()
    budget = CURRENT_BUDGET.get()

    def invoke():
        checkpoint()
        result = action(*args, **kwargs)
        checkpoint()
        return result

    future = pool.submit(context.run, invoke)
    if budget is not None:
        budget.track(future)
    return future


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
