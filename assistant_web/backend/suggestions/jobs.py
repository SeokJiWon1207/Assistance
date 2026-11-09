"""프로세스당 한 워커. DB lease로 사용자별 중복 생성도 막는다."""
from collections import deque
from datetime import datetime, timezone
from threading import Condition, Thread, current_thread
from time import monotonic
from typing import Callable
import logging

from backend.execution import ExecutionBudget
from backend.suggestions.contracts import Lease
from backend.suggestions.repository import SuggestionRepository
from backend.suggestions.settings import QUEUE_LIMIT

logger = logging.getLogger(__name__)


class SuggestionJobs:
    def __init__(self, repository: SuggestionRepository, run: Callable):
        self.repository = repository
        self.run = run
        self.condition = Condition()
        self.pending: deque[Lease] = deque()
        self.thread: Thread | None = None
        self.stopping = False
        self.active: ExecutionBudget | None = None

    def pending_count(self) -> int:
        with self.condition:
            return len(self.pending)

    def start(self) -> None:
        with self.condition:
            if self.thread is None and not self.stopping:
                self.thread = Thread(target=self._work, name='home-suggestions', daemon=True)
                self.thread.start()

    def enqueue(self, lease: Lease) -> bool:
        with self.condition:
            if self.stopping or len(self.pending) >= QUEUE_LIMIT:
                return False
            self.pending.append(lease)
            self.condition.notify()
        self.start()
        return True

    def _work(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopping or self.pending)
                if self.stopping:
                    return
                lease = self.pending.popleft()
                # 한 작업은 60초 이내다. 다음 작업을 시작할 때 기다리는 lease도 갱신한다.
                now = datetime.now(timezone.utc)
                for waiting in self.pending:
                    self.repository.renew(waiting, now)
                if not self.repository.renew(lease, now):
                    continue
                budget = ExecutionBudget(monotonic() + 60)
                self.active = budget
            try:
                self.run(lease, budget)
            except Exception as exc:
                # 메시지·traceback에는 원문이나 토큰이 들어갈 수 있으므로 타입만 기록한다.
                logger.warning('home_suggestions stage=generation error_type=%s', type(exc).__name__)
            finally:
                budget.cancel()
                budget.wait_for_workers()
                self.repository.release(lease, datetime.now(timezone.utc))
                with self.condition:
                    self.active = None

    def stop(self) -> None:
        with self.condition:
            self.stopping = True
            if self.active:
                self.active.cancel()
            for lease in self.pending:
                self.repository.release(lease, datetime.now(timezone.utc), retry=True)
            self.pending.clear()
            self.condition.notify_all()
            worker = self.thread
        if worker is not None and worker is not current_thread():
            worker.join()
