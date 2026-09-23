"""순서를 지키면서 뒤 작업을 미리 실행하는 공용 반복자."""

from concurrent.futures import Future, ThreadPoolExecutor
from typing import Callable, Generator, Iterable, TypeVar

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
    pool = ThreadPoolExecutor(max_workers=limit)
    pending: dict[int, Future[R]] = {}
    next_index = 0
    try:
        for index, item in enumerate(queue):
            while next_index < len(queue) and len(pending) < limit:
                pending[next_index] = pool.submit(func, queue[next_index])
                next_index += 1
            yield item, pending.pop(index).result()
    finally:
        # 조기 종료가 정상 경로라 진행 중인 선실행을 기다리지 않는다.
        pool.shutdown(wait=False, cancel_futures=True)
