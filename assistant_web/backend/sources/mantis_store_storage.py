"""Mantis 저장본 쓰기의 배타 정리와 디스크 여유 공간 보호."""
from contextlib import contextmanager
import fcntl
import logging
from pathlib import Path
import re
import shutil
import stat

LOGGER = logging.getLogger(__name__)
MIB = 1024 * 1024
RESERVE_BYTES = 128 * MIB
_TEMPORARY = re.compile(r"mantis-complete-[a-z0-9_]{8}\.sqlite3(?:-(?:journal|wal|shm))?")


def _remove_temporary(path: Path) -> int:
    if not _TEMPORARY.fullmatch(path.name):
        return 0
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            return 0
        path.unlink()
        return info.st_size
    except FileNotFoundError:
        return 0


def cleanup_temporary_store(temporary: str) -> None:
    """닫힌 작업 파일과 SQLite 부속 파일을 함께 제거한다."""
    for suffix in ("", "-journal", "-wal", "-shm"):
        _remove_temporary(Path(temporary + suffix))


def check_working_space(path: Path) -> None:
    free = shutil.disk_usage(path.parent).free
    if free < RESERVE_BYTES:
        LOGGER.warning("Mantis 저장 중 공간 부족: free=%d reserve=%d", free, RESERVE_BYTES)
        raise OSError("Mantis 저장 공간 부족: 기존 저장본을 유지합니다.")


@contextmanager
def store_write_guard(path: Path):
    """모든 저장본 작성자가 공유하는 잠금을 얻은 경우에만 정리한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield {"status": "running"}
            return
        reclaimed = sum(_remove_temporary(item) for item in path.parent.glob("mantis-complete-*"))
        size = sum(item.stat().st_size for item in (path, Path(str(path) + "-wal")) if item.is_file())
        required = max(256 * MIB, size + size // 4 + RESERVE_BYTES)
        free = shutil.disk_usage(path.parent).free
        details = {"freeBytes": free, "requiredBytes": required, "reclaimedBytes": reclaimed}
        if reclaimed:
            LOGGER.info("Mantis 임시 파일 정리: reclaimed=%d free=%d", reclaimed, free)
        if free < required:
            LOGGER.warning("Mantis 동기화 보류: free=%d required=%d reclaimed=%d", free, required, reclaimed)
            yield {"status": "deferred", "reason": "insufficient_disk_space", **details}
            return
        yield {"status": "ready", **details}
