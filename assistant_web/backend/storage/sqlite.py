"""연결 생성만 소유한다. 트랜잭션과 연결 수명은 호출자가 관리한다.

open_sqlite는 BEGIN/commit/rollback, schema 변경, 디렉터리 생성, chmod를
하지 않는다. 호출자는 기존 closing(connection), with connection, 명시적
BEGIN IMMEDIATE와 finally/close 경계를 유지한다. 프로필 적용 실패 때만
아직 호출자에게 전달되지 않은 연결을 닫고 원래 예외를 다시 던진다.
"""
from dataclasses import dataclass
from pathlib import Path
import sqlite3
from typing import Any, Callable, Literal


@dataclass(frozen=True)
class ConnectionProfile:
    timeout: float = 5.0
    uri: bool = False
    row_factory: Callable[[sqlite3.Cursor, tuple[Any, ...]], Any] | None = None
    isolation_level: Literal['', 'DEFERRED', 'IMMEDIATE', 'EXCLUSIVE'] | None = ''
    autocommit: bool | Literal[-1] = sqlite3.LEGACY_TRANSACTION_CONTROL
    check_same_thread: bool = True
    pragmas: tuple[str, ...] = ()


def open_sqlite(path: Path | str, *, profile: ConnectionProfile) -> sqlite3.Connection:
    """기존 경로/URI를 그대로 열고 해당 DB의 연결 옵션만 적용한다."""
    connection = sqlite3.connect(
        path, timeout=profile.timeout, uri=profile.uri,
        # 현재 typeshed는 Python 3.12의 빈 isolation_level과 -1 상수를 누락한다.
        # 실제 연결의 isolation_level/autocommit은 storage 계약 테스트로 검증한다.
        isolation_level=profile.isolation_level,  # type: ignore[arg-type]
        autocommit=profile.autocommit,  # type: ignore[arg-type]
        check_same_thread=profile.check_same_thread,
    )
    try:
        connection.row_factory = profile.row_factory
        for statement in profile.pragmas:
            connection.execute(statement)
    except BaseException:
        connection.close()
        raise
    return connection
