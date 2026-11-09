"""요청별 단계 시간. 서로 다른 사용자/스레드의 측정값을 섞지 않는다."""

from contextvars import ContextVar
from functools import wraps
from time import perf_counter
from threading import Lock

from backend.execution import checkpoint

TIMINGS: ContextVar[dict | None] = ContextVar("chat_timings", default=None)
_TIMING_LOCK = Lock()


def measured(stage):
    def decorate(fn):
        @wraps(fn)
        def run(*args, **kwargs):
            started = perf_counter()
            try:
                checkpoint()
                result = fn(*args, **kwargs)
                checkpoint()
                return result
            finally:
                timings = TIMINGS.get()
                if timings is not None:
                    duration = round((perf_counter() - started) * 1000, 2)
                    # 복사된 요청 문맥의 계측 dict도 공유된다. 시간 합산을 읽기/쓰기 한 연산으로 유지한다.
                    with _TIMING_LOCK:
                        entry = timings.setdefault(stage, {"calls": 0, "durationMs": 0.0})
                        entry["calls"] += 1
                        entry["durationMs"] += duration
        return run
    return decorate


def request_timing(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        token = TIMINGS.set({})
        try:
            return fn(*args, **kwargs)
        finally:
            TIMINGS.reset(token)
    return run
