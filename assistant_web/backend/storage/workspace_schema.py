"""workspace.sqlite3의 경로와 순차 스키마 소유자. 기존 5개 버전을 유지한다."""
import os
import json
import sqlite3
from pathlib import Path
from backend.settings.paths import RUNTIME_DIR
from backend.db_migrations import run_sqlite_migrations

from backend.storage.conversation_rows import safe_messages, write_rows


def workspace_db_path() -> Path:
    configured = os.getenv("WORKSPACE_DB_PATH", "").strip()
    return Path(configured).expanduser() if configured else RUNTIME_DIR / "workspace.sqlite3"


def _create_workspace_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                user_uid TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                title TEXT NOT NULL,
                messages_json TEXT NOT NULL,
                favorite INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_uid, conversation_id)
            )
            """
        )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_conversations_user_updated "
        "ON conversations(user_uid, favorite DESC, updated_at DESC)"
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS notification_reads (
            user_uid TEXT NOT NULL,
            notification_id TEXT NOT NULL,
            read_at TEXT NOT NULL,
            PRIMARY KEY (user_uid, notification_id)
        )
        """
    )


def _create_team_brief_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_team_briefs (
            week_start TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            generated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS weekly_team_brief_reads (
            user_uid TEXT NOT NULL,
            week_start TEXT NOT NULL,
            read_at TEXT NOT NULL,
            PRIMARY KEY (user_uid, week_start)
        )
        """
    )


def _create_playbook_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS resolution_playbooks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            symptom TEXT NOT NULL,
            cause TEXT NOT NULL DEFAULT '',
            resolution TEXT NOT NULL,
            verification TEXT NOT NULL DEFAULT '',
            tags_json TEXT NOT NULL DEFAULT '[]',
            source_issue_ids_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'published', 'archived')),
            created_by_uid TEXT NOT NULL,
            created_by_name TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT ''
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_resolution_playbooks_status_updated "
        "ON resolution_playbooks(status, updated_at DESC)"
    )


def migrate_messages(db):
    db.execute('''CREATE TABLE IF NOT EXISTS conversation_messages (
        user_uid TEXT NOT NULL, conversation_id TEXT NOT NULL, position INTEGER NOT NULL,
        message_json TEXT NOT NULL, message_bytes INTEGER NOT NULL,
        PRIMARY KEY(user_uid, conversation_id, position))''')
    for user, conversation, payload in db.execute('SELECT user_uid,conversation_id,messages_json FROM conversations').fetchall():
        try:
            values = json.loads(payload)
        except (TypeError, ValueError):
            values = []
        if isinstance(values, list):
            write_rows(db, user, conversation, 0, safe_messages(values))
        db.execute("UPDATE conversations SET messages_json='[]' WHERE user_uid=? AND conversation_id=?", (user, conversation))


def create_notification_schema(db: sqlite3.Connection) -> None:
    db.execute("""CREATE TABLE workspace_observations (
        user_uid TEXT PRIMARY KEY, scope_key TEXT NOT NULL, observed_at TEXT NOT NULL, facts_json TEXT NOT NULL)""")
    db.execute("""CREATE TABLE workspace_events (
        id TEXT PRIMARY KEY, user_uid TEXT NOT NULL, scope_key TEXT NOT NULL,
        issue_id INTEGER NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL)""")
    db.execute("CREATE INDEX workspace_events_user_time ON workspace_events(user_uid, observed_at DESC)")


def init_workspace_db() -> None:
    run_sqlite_migrations(
        workspace_db_path(),
        [_create_workspace_schema, _create_team_brief_schema, _create_playbook_schema, migrate_messages,
         create_notification_schema],
        database_name="workspace",
    )
