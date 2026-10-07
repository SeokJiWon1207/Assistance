"""순서를 지키면서 뒤 작업을 미리 실행하는 공용 반복자."""

from contextvars import copy_context
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Generator, Iterable, TypeVar

from backend.execution import CURRENT_BUDGET, checkpoint, wait_for_result

T = TypeVar("T")
R = TypeVar("R")


def iter_in_order(
    func: Callable[[T], R],
    items: Iterable[T],
    *,
    workers: int,
) -> Generator[tuple[T, R], None, None]:
    """items를 순서대로 처리한 결과를 내주면서 뒤 항목을 workers개까지 미리 실행한다.

    종료 판단은 호출자가 순서대로 하므로 결과는 순차 처리와 같다. 호출자가 멈추면
    미리 실행한 뒤 항목의 결과와 오류는 버린다.
    """
    queue = list(items)
    limit = max(1, workers)
    checkpoint()
    pool = ThreadPoolExecutor(max_workers=limit)
    pending: dict[int, Future[R]] = {}
    next_index = 0
    try:
        for index, item in enumerate(queue):
            while next_index < len(queue) and len(pending) < limit:
                pending[next_index] = pool.submit(copy_context().run, func, queue[next_index])
                budget = CURRENT_BUDGET.get()
                if budget is not None:
                    budget.track(pending[next_index])
                next_index += 1
            yield item, wait_for_result(pending.pop(index))
    finally:
        # 정상적인 조기 종료에서는 답변을 지연시키지 않는다. 채팅 요청의 남은
        # 선실행 작업은 ExecutionBudget이 추적하고 API 워커가 슬롯 반환 전에 기다린다.
        pool.shutdown(wait=False, cancel_futures=True)
