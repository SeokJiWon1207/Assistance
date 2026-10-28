"""검증된 문서가 있는 목록 검색의 추가 탐색에만 별도 마감을 둔다."""

import time
from contextlib import contextmanager
from typing import Iterator

from backend.execution import CURRENT_BUDGET, ExecutionBudget, RequestStopped


class _SearchBudget(ExecutionBudget):
    def __init__(self, deadline: float, parent: ExecutionBudget | None):
        super().__init__(deadline)
        self.parent = parent

    def remaining(self) -> float:
        if self.parent is not None:
            self.parent.remaining()
        return super().remaining()


@contextmanager
def bounded_live_search(enabled: bool) -> Iterator[dict[str, bool]]:
    result = {'limited': False}
    if not enabled:
        yield result
        return
    parent = CURRENT_BUDGET.get()
    # 추가 목록/본문 탐색은 최대 30초, 이후 정렬·답변 처리에 30초를 남긴다.
    deadline = time.monotonic() + 30
    if parent is not None:
        deadline = min(deadline, parent.deadline - 30)
    budget = _SearchBudget(deadline, parent)
    token = CURRENT_BUDGET.set(budget)
    try:
        yield result
    except RequestStopped as exc:
        # 사용자의 취소나 상위 요청 마감은 부분 성공으로 숨기지 않는다.
        if parent is not None:
            parent.remaining()
        if exc.status_code == 499:
            raise
        result['limited'] = True
    finally:
        CURRENT_BUDGET.reset(token)
        budget.cancel()
        budget.wait_for_workers()
