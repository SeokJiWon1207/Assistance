"""Notion 목록 검색은 현재 요청의 남은 예산 안에서 부분 결과를 반환한다."""

from contextlib import contextmanager
from typing import Iterator

from backend.execution import CURRENT_BUDGET, ExecutionBudget, RequestStopped


class _SearchBudget(ExecutionBudget):
    def __init__(self, deadline: float, parent: ExecutionBudget):
        super().__init__(deadline)
        self.parent = parent

    def remaining(self) -> float:
        self.parent.remaining()
        return super().remaining()


@contextmanager
def bounded_live_search(enabled: bool, *, include_blocks: bool = False) -> Iterator[dict[str, bool]]:
    result = {'limited': False}
    if not enabled:
        yield result
        return
    parent = CURRENT_BUDGET.get() or ExecutionBudget.create()
    # 목록은 결과 정리, 본문 요청은 표시할 문서 읽기에도 시간이 필요하다.
    # 짧은 에이전트 도구에서도 남은 시간의 최소 80%는 검색에 쓸 수 있다.
    reserve = min(30.0 if include_blocks else 5.0, parent.remaining() * 0.2)
    budget = _SearchBudget(parent.deadline - reserve, parent)
    token = CURRENT_BUDGET.set(budget)
    try:
        yield result
    except RequestStopped as exc:
        # 사용자의 취소나 상위 요청 마감은 부분 성공으로 숨기지 않는다.
        parent.remaining()
        if exc.status_code == 499:
            raise
        result['limited'] = True
    finally:
        CURRENT_BUDGET.reset(token)
        budget.cancel()
        budget.wait_for_workers()
