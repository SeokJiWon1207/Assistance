"""원본과 함께 게시하는 검색문. JSON 디코딩은 키워드 후보에만 수행한다."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from backend.common import normalize_for_match
from backend.execution import checkpoint

# issue_to_search_text/normalize_for_match의 검색 의미가 바뀌면 함께 올린다.
SEARCH_PROJECTION_VERSION = 2
SEARCH_PAYLOAD_SQL = "json_remove(payload, '$.history', '$.attachments', '$.monitors', '$.relationships')"


def projection_ready(db: sqlite3.Connection, saved: dict[str, Any]) -> bool:
    if saved.get('searchProjectionVersion') != SEARCH_PROJECTION_VERSION:
        return False
    try:
        return db.execute('SELECT count(*) FROM search_projection').fetchone()[0] == saved['count']
    except sqlite3.Error:
        return False


def update_projection(db: sqlite3.Connection, issues: Iterable[dict[str, Any]]) -> None:
    from backend.sources.mantis import issue_to_search_text

    for issue in issues:
        checkpoint()
        db.execute('INSERT OR REPLACE INTO search_projection VALUES (?, ?)',
                   (int(issue['id']), normalize_for_match(issue_to_search_text(issue))))


def build_projection(db: sqlite3.Connection) -> None:
    """게시 전 임시 DB에서 한 행씩 만들고, 원본 변경 시 해당 검색문을 무효화한다."""
    db.execute('CREATE TABLE IF NOT EXISTS search_projection (id INTEGER PRIMARY KEY, normalized_text TEXT NOT NULL)')
    # 큰 TEXT 페이지를 읽지 않고 전체 범위와 누락 여부를 확인한다.
    db.execute('CREATE INDEX IF NOT EXISTS issues_id_cover ON issues(id)')
    db.execute('CREATE INDEX IF NOT EXISTS search_projection_id_cover ON search_projection(id)')
    for name, event, condition in (
        ('insert', 'INSERT', 'id=NEW.id'),
        ('update', 'UPDATE', 'id=OLD.id OR id=NEW.id'),
        ('delete', 'DELETE', 'id=OLD.id'),
    ):
        db.execute(f'CREATE TRIGGER IF NOT EXISTS invalidate_search_{name} AFTER {event} ON issues '
                   f'BEGIN DELETE FROM search_projection WHERE {condition}; END')
    db.execute('DELETE FROM search_projection')
    cursor = db.execute(f'SELECT {SEARCH_PAYLOAD_SQL} FROM issues ORDER BY id DESC')
    try:
        update_projection(db, (json.loads(payload) for (payload,) in cursor))
    finally:
        cursor.close()
