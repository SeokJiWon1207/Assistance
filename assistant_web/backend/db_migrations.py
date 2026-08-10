"""Small, sequential SQLite migration runner shared by application databases."""

from __future__ import annotations

import sqlite3
import os
from contextlib import closing
from pathlib import Path
from typing import Callable, Sequence


Migration = Callable[[sqlite3.Connection], None]


def run_sqlite_migrations(
    database_path: Path,
    migrations: Sequence[Migration],
    *,
    database_name: str,
) -> None:
    """Apply every migration after ``PRAGMA user_version`` in one transaction each."""
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database_path)) as connection:
        connection.execute("PRAGMA busy_timeout = 5000")
        current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        latest_version = len(migrations)
        if current_version > latest_version:
            raise RuntimeError(
                f"{database_name} DB version {current_version} is newer than supported "
                f"version {latest_version}"
            )

        for target_version in range(current_version + 1, latest_version + 1):
            with connection:
                migrations[target_version - 1](connection)
                connection.execute(f"PRAGMA user_version = {target_version}")
    try:
        os.chmod(database_path, 0o600)
    except OSError:
        pass
