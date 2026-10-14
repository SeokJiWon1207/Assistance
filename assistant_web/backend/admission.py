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


class ChatAdmissionController:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active_total = 0
        self._active_by_user: Dict[str, int] = defaultdict(int)
        self._request_times: Dict[str, Deque[float]] = defaultdict(deque)

    def acquire(self, user_key: str) -> ChatAdmissionLease:
        key = str(user_key or "anonymous").strip() or "anonymous"
        per_user_limit = clamp_int(os.getenv("CHAT_MAX_CONCURRENT_PER_USER", "2"), 1, 20) or 2
        total_limit = clamp_int(os.getenv("CHAT_MAX_CONCURRENT_TOTAL", "2"), 1, 100) or 2
        rate_limit = clamp_int(os.getenv("CHAT_RATE_LIMIT_PER_MINUTE", "20"), 1, 1000) or 20
        minimum_mb = clamp_int(os.getenv("CHAT_MIN_AVAILABLE_MEMORY_MB", "192"), 1, 1048576) or 192
        reserve_mb = clamp_int(os.getenv("CHAT_MEMORY_RESERVE_PER_REQUEST_MB", "128"), 1, 1048576) or 128
        now = time.monotonic()

        with self._lock:
            history = self._request_times[key]
            while history and now - history[0] >= 60:
                history.popleft()
            if len(history) >= rate_limit:
                retry_after = max(1, int(60 - (now - history[0])))
                raise HTTPException(
                    status_code=429,
                    detail="요청이 너무 많습니다. 잠시 후 다시 시도해 주세요.",
                    headers={"Retry-After": str(retry_after)},
                )
            if self._active_by_user[key] >= per_user_limit or self._active_total >= total_limit:
                raise HTTPException(
                    status_code=429,
                    detail="처리 중인 요청이 많습니다. 현재 응답이 끝난 뒤 다시 시도해 주세요.",
                    headers={"Retry-After": "2"},
                )
            available = available_memory_bytes()
            required = (minimum_mb + reserve_mb * (self._active_total + 1)) * 1024 ** 2
            # 동시 요청의 아직 할당되지 않은 작업 메모리까지 같은 잠금 아래 예약한다.
            # 계측 불가 플랫폼에서도 위의 고정 동시성 제한은 유지한다.
            if available is not None and available < required:
                raise HTTPException(
                    status_code=429,
                    detail="서버 메모리 여유가 부족해 새 요청을 잠시 제한합니다. 잠시 후 다시 시도해 주세요.",
                    headers={"Retry-After": "5"},
                )
            history.append(now)
            self._active_by_user[key] += 1
            self._active_total += 1

        return ChatAdmissionLease(self, key)

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
            self._active_total = 0
            self._active_by_user.clear()
            self._request_times.clear()


CHAT_ADMISSION = ChatAdmissionController()
