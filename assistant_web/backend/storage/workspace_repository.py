"""팀 브리핑 저장과 사용자별 읽음 상태. 생성·판단은 리포트 계층이 담당한다."""
import sqlite3 as sqlite3
from contextlib import closing

from backend.storage.sqlite import open_sqlite
from backend.storage.profiles import WORKSPACE, WORKSPACE_ROWS
from backend.storage.workspace_schema import init_workspace_db, workspace_db_path
from backend.utils.dates import local_timestamp as _now


def read_team_brief(week_start: str, *, refresh: bool = False):
    with closing(open_sqlite(workspace_db_path(), profile=WORKSPACE_ROWS)) as connection, connection:
        return None if refresh else connection.execute(
            "SELECT payload_json FROM weekly_team_briefs WHERE week_start = ?", (week_start,)
        ).fetchone()


def store_team_brief(week_start: str, serialized: str, generated_at: str, *, replace: bool):
    with closing(open_sqlite(workspace_db_path(), profile=WORKSPACE)) as connection, connection:
        if replace:
            connection.execute(
                """INSERT INTO weekly_team_briefs (week_start, payload_json, generated_at)
                   VALUES (?, ?, ?) ON CONFLICT(week_start) DO UPDATE SET
                   payload_json = excluded.payload_json, generated_at = excluded.generated_at""",
                (week_start, serialized, generated_at),
            )
        else:
            connection.execute(
                "INSERT OR IGNORE INTO weekly_team_briefs (week_start, payload_json, generated_at) VALUES (?, ?, ?)",
                (week_start, serialized, generated_at),
            )
        return connection.execute(
            "SELECT payload_json FROM weekly_team_briefs WHERE week_start = ?", (week_start,)
        ).fetchone()


def team_brief_viewed(user_uid: str, week_start: str) -> bool:
    with closing(open_sqlite(workspace_db_path(), profile=WORKSPACE)) as connection:
        return connection.execute(
            "SELECT 1 FROM weekly_team_brief_reads WHERE user_uid = ? AND week_start = ?",
            (user_uid, week_start),
        ).fetchone() is not None


def mark_weekly_team_brief_read(user_uid: str, week_start: str) -> None:
    init_workspace_db()
    with closing(open_sqlite(workspace_db_path(), profile=WORKSPACE)) as connection, connection:
        connection.execute(
            """INSERT INTO weekly_team_brief_reads (user_uid, week_start, read_at)
               VALUES (?, ?, ?) ON CONFLICT(user_uid, week_start) DO UPDATE SET read_at = excluded.read_at""",
            (user_uid, week_start, _now()),
        )
