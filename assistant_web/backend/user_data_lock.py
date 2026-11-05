"""진행 중인 사용자 요청과 데이터 삭제 사이의 프로세스 간 잠금."""
import fcntl
import hashlib
import os
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def user_data_operation(uid: str, database_path: Path, *, exclusive: bool = False):
    directory = database_path.parent / '.user-data-locks'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(uid.encode()).hexdigest() + '.lock')
    # inode 교체로 동시 잠금이 우회되지 않도록 잠금 파일은 유지한다.
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), 'a') as lock:
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        fcntl.flock(lock, mode | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
