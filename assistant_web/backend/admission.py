"""In-process admission control for expensive chat generation requests."""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Deque, Dict, Iterator

from fastapi import HTTPException

from backend.common import clamp_int
from backend.memory_pressure import available_memory_bytes
from backend.execution import checkpoint


class ChatAdmissionLease:
    def __init__(self, controller: "ChatAdmissionController", user_key: str) -> None:
        self._controller = controller
        self.user_key = user_key
        self._released = False
        self._release_lock = threading.Lock()

    def release(self) -> None:
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._controller._release(self.user_key)

    def __enter__(self) -> "ChatAdmissionLease":
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()


class ChatAdmissionTicket:
    """실행 슬롯을 소비하지 않는 대기 요청. 수명은 접속한 호출자가 소유한다."""

    def __init__(self, controller: "ChatAdmissionController", user_key: str, expires_at: float) -> None:
        self._controller = controller
        self.user_key = user_key
        self.expires_at = expires_at
        self._state = "pending"
        self._lease: ChatAdmissionLease | None = None

    @property
    def position(self) -> int:
        with self._controller._lock:
            if self._state != "pending":
                return 0
            return self._controller._pending.index(self) + 1

    def try_start(self) -> ChatAdmissionLease | None:
        return self._controller._try_start(self)

    def cancel(self) -> None:
        with self._controller._lock:
            if self._state == "pending":
                self._controller._remove_pending_locked(self)
                self._state = "cancelled"


class ChatAdmissionController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_total = 0
        self._active_by_user: Dict[str, int] = defaultdict(int)
        self._request_times: Dict[str, Deque[float]] = defaultdict(deque)
        self._pending: Deque[ChatAdmissionTicket] = deque()
        self._queued_by_user: Dict[str, int] = defaultdict(int)

    def acquire(self, user_key: str) -> ChatAdmissionLease:
        key = str(user_key or "anonymous").strip() or "anonymous"
        with self._lock:
            self._expire_pending_locked(time.monotonic())
            return self._acquire_locked(key)

    def _check_rate_locked(self, key: str, now: float) -> Deque[float]:
        rate_limit = clamp_int(os.getenv("CHAT_RATE_LIMIT_PER_MINUTE", "20"), 1, 1000) or 20
        history = self._request_times[key]
        while history and now - history[0] >= 60:
            history.popleft()
        if len(history) >= rate_limit:
            raise HTTPException(
                status_code=429,
                detail="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
                headers={"Retry-After": str(max(1, int(60 - (now - history[0]))))},
            )
        return history

    def _acquire_locked(self, key: str, *, queued: bool = False) -> ChatAdmissionLease:
        per_user_limit = clamp_int(os.getenv("CHAT_MAX_CONCURRENT_PER_USER", "2"), 1, 20) or 2
        total_limit = clamp_int(os.getenv("CHAT_MAX_CONCURRENT_TOTAL", "2"), 1, 100) or 2
        minimum_mb = clamp_int(os.getenv("CHAT_MIN_AVAILABLE_MEMORY_MB", "192"), 1, 1048576) or 192
        reserve_mb = clamp_int(os.getenv("CHAT_MEMORY_RESERVE_PER_REQUEST_MB", "128"), 1, 1048576) or 128
        now = time.monotonic()

        history = self._check_rate_locked(key, now) if not queued else None
        if ((self._pending and not queued) or self._active_by_user.get(key, 0) >= per_user_limit
                or self._active_total >= total_limit):
            raise HTTPException(
                status_code=429,
                detail="처리 중인 요청이 많습니다. 현재 응답이 끝난 뒤 다시 시도해 주세요.",
                headers={"Retry-After": "2"},
            )
        available = available_memory_bytes()
        required = (minimum_mb + reserve_mb * (self._active_total + 1)) * 1024 ** 2
        if available is not None and available < required:
            raise HTTPException(
                status_code=429,
                detail="서버 메모리 여유가 부족해 새 요청을 잠시 제한합니다. 잠시 후 다시 시도해 주세요.",
                headers={"Retry-After": "5"},
            )
        if history is not None:
            history.append(now)
        self._active_by_user[key] += 1
        self._active_total += 1
        return ChatAdmissionLease(self, key)

    def enqueue(self, user_key: str) -> ChatAdmissionTicket:
        key = str(user_key or "anonymous").strip() or "anonymous"
        maximum = clamp_int(os.getenv("CHAT_MAX_QUEUED_TOTAL", "32"), 1, 1000) or 32
        per_user = clamp_int(os.getenv("CHAT_MAX_QUEUED_PER_USER", "4"), 1, 100) or 4
        timeout = clamp_int(os.getenv("CHAT_QUEUE_TIMEOUT_SECONDS", "120"), 1, 600) or 120
        now = time.monotonic()
        with self._lock:
            self._expire_pending_locked(now)
            history = self._check_rate_locked(key, now)
            if len(self._pending) >= maximum or self._queued_by_user.get(key, 0) >= per_user:
                raise HTTPException(429, "대기 요청이 많습니다. 잠시 후 다시 시도해 주세요.",
                                    headers={"Retry-After": "5"})
            ticket = ChatAdmissionTicket(self, key, now + timeout)
            self._pending.append(ticket)
            self._queued_by_user[key] += 1
            # 대기 요청도 접수 시 분당 한도를 적용해 무제한 재접수를 막는다.
            history.append(now)
            return ticket

    def _remove_pending_locked(self, ticket: ChatAdmissionTicket) -> None:
        self._pending.remove(ticket)
        self._queued_by_user[ticket.user_key] -= 1
        if self._queued_by_user[ticket.user_key] <= 0:
            del self._queued_by_user[ticket.user_key]

    def _expire_pending_locked(self, now: float) -> None:
        for ticket in list(self._pending):
            if now >= ticket.expires_at:
                self._remove_pending_locked(ticket)
                ticket._state = "expired"

    def _try_start(self, ticket: ChatAdmissionTicket) -> ChatAdmissionLease | None:
        with self._lock:
            self._expire_pending_locked(time.monotonic())
            if ticket._state == "expired":
                raise HTTPException(503, "대기 시간이 초과되었습니다. 잠시 후 다시 질문해 주세요.",
                                    headers={"Retry-After": "5"})
            if ticket._state == "cancelled":
                raise HTTPException(499, "요청이 취소되었습니다.")
            if ticket._state == "started":
                return ticket._lease
            if self._pending[0] is not ticket:
                return None
            try:
                lease = self._acquire_locked(ticket.user_key, queued=True)
            except HTTPException as exc:
                if exc.status_code != 429:
                    raise
                return None
            self._remove_pending_locked(ticket)
            ticket._state, ticket._lease = "started", lease
            return lease

    @contextmanager
    def wait(self, user_key: str) -> Iterator[None]:
        ticket = self.enqueue(user_key)
        lease = None
        try:
            while lease is None:
                checkpoint()
                lease = ticket.try_start()
                if lease is None:
                    time.sleep(0.05)
            yield
        finally:
            ticket.cancel()
            if lease is not None:
                lease.release()

    def _release(self, key: str) -> None:
        with self._lock:
            self._active_by_user[key] -= 1
            self._active_total -= 1
            if self._active_by_user[key] <= 0:
                self._active_by_user.pop(key, None)

    @contextmanager
    def admit(self, user_key: str) -> Iterator[None]:
        lease = self.acquire(user_key)
        try:
            yield
        finally:
            lease.release()

    def reset(self) -> None:
        with self._lock:
            for ticket in self._pending:
                ticket._state = "cancelled"
            self._pending.clear()
            self._queued_by_user.clear()
            self._active_total = 0
            self._active_by_user.clear()
            self._request_times.clear()


CHAT_ADMISSION = ChatAdmissionController()
