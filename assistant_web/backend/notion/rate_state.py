"""같은 호스트의 동일 연결이 요청 간격과 서버 대기 시간을 공유한다."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time

from backend.execution import checkpoint, interruptible_sleep
from backend.settings.notion import get_notion_token


@contextmanager
def _state():
    token = get_notion_token()
    if not token or os.getenv('NOTION_SHARED_RATE_LIMIT_ENABLED', 'true').lower() in {'false', '0', 'off'}:
        yield None
        return
    directory = Path(os.getenv('NOTION_RATE_STATE_DIR') or (Path(tempfile.gettempdir()) / f'assistant-notion-rate-{os.getuid()}'))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(token.encode()).hexdigest() + '.json')
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'r+') as handle:
        while True:
            checkpoint()
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                interruptible_sleep(.05)
        try:
            yield handle
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def shared_delay(*, interval=0.0, defer=0.0) -> tuple[float, bool]:
    """잠금 안에서는 상태만 갱신한다. 대기는 잠금 밖에서 취소·마감에 응답한다."""
    with _state() as handle:
        if handle is None:
            return 0.0, False
        now = time.time()
        try:
            state = json.load(handle)
            next_at = float(state['next_at'])
            retry_until = float(state['retry_until'])
        except (ValueError, KeyError, TypeError):
            next_at = retry_until = 0.0
        if defer:
            retry_until = max(retry_until, now + defer)
        delay = max(0.0, next_at - now, retry_until - now)
        if not delay and interval:
            next_at = now + interval
        handle.seek(0)
        json.dump({'next_at': next_at, 'retry_until': retry_until}, handle)
        handle.truncate()
        handle.flush()
        return delay, retry_until > now
