"""Small, sequential SQLite migration runner shared by application databases."""

from __future__ import annotations

import sqlite3 as sqlite3
import os
from contextlib import closing
from pathlib import Path
from typing import Callable, Sequence

from backend.storage.sqlite import open_sqlite
from backend.storage.profiles import MIGRATIONS


Migration = Callable[[sqlite3.Connection], None]


def run_sqlite_migrations(
    database_path: Path,
    migrations: Sequence[Migration],
    *,
    database_name: str,
) -> None:
    """Apply every migration after ``PRAGMA user_version`` in one transaction each."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(open_sqlite(database_path, profile=MIGRATIONS)) as connection:
        latest_version = len(migrations)
        while int(connection.execute("PRAGMA user_version").fetchone()[0]) != latest_version:
            # 버전 조회도 쓰기 잠금 안에서 수행해 동시 시작의 중복 DDL을 막는다.
            with connection:
                connection.execute("BEGIN IMMEDIATE")
                current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if current_version > latest_version:
                    raise RuntimeError(
                        f"{database_name} DB version {current_version} is newer than supported version {latest_version}"
                    )
                if current_version == latest_version:
                    break
                migrations[current_version](connection)
                connection.execute(f"PRAGMA user_version = {current_version + 1}")
    try:
        os.chmod(database_path, 0o600)
    except OSError:
        pass
