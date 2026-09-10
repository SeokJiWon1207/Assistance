"""서버가 확인한 담당자 범위를 현재 요청의 보고서 빌더에 전달한다.

문자열에 붙인 이름을 자연어 인식기로 다시 추측하지 않는다. ContextVar 토큰을
반드시 복원하므로 동시 요청이나 이후 질문에 다른 사용자의 범위가 남지 않는다.
"""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


AUTHENTICATED_OWNER: ContextVar[str] = ContextVar("authenticated_mantis_owner", default="")


@contextmanager
def authenticated_owner_scope(owner: str) -> Iterator[None]:
    token = AUTHENTICATED_OWNER.set(owner)
    try:
        yield
    finally:
        AUTHENTICATED_OWNER.reset(token)
