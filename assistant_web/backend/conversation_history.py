"""사용자별 대화 메시지를 순번별로 보존하고 작은 페이지·부분 쓰기로 제공한다."""
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from backend.common import clamp_int
from backend.schemas import ConversationMessage


class ConversationConflictError(ValueError):
    """기존 메시지를 덮기 전에 서버 버전과 순번 경계를 확인한다."""


class ConversationDeletedError(ValueError):
    """삭제된 서버 기록을 오래된 기기의 저장 요청으로 되살리지 않는다."""


class ConversationQuotaError(ValueError):
    """용량을 초과하면 기존 기록을 자르지 않고 쓰기를 거절한다."""


def safe_messages(messages):
    safe = []
    for raw in messages:
        try:
            safe.append(ConversationMessage.model_validate(raw).model_dump(exclude_none=True))
        except (TypeError, ValueError):
            continue
    return safe


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


def write_rows(db, user, conversation, start, messages):
    rows = []
    for index, message in enumerate(messages, start):
        value = json.dumps(message, ensure_ascii=False)
        rows.append((user, conversation, index, value, len(value.encode('utf-8'))))
    db.executemany('INSERT OR REPLACE INTO conversation_messages VALUES (?,?,?,?,?)', rows)


def hydrate_legacy(db, user, conversation):
    row = db.execute('SELECT messages_json FROM conversations WHERE user_uid=? AND conversation_id=?', (user, conversation)).fetchone()
    if not row or row[0] == '[]':
        return
    try:
        values = json.loads(row[0])
    except (TypeError, ValueError):
        values = []
    if isinstance(values, list):
        write_rows(db, user, conversation, 0, safe_messages(values))
    db.execute("UPDATE conversations SET messages_json='[]' WHERE user_uid=? AND conversation_id=?", (user, conversation))


def read_page(db, user, conversation, *, before=None, limit=60):
    row = db.execute('SELECT conversation_id,title,favorite,created_at,updated_at FROM conversations '
                     'WHERE user_uid=? AND conversation_id=?', (user, conversation)).fetchone()
    if row is None:
        return None
    hydrate_legacy(db, user, conversation)
    count = db.execute('SELECT COUNT(*) FROM conversation_messages WHERE user_uid=? AND conversation_id=?', (user, conversation)).fetchone()[0]
    end = count if before is None else max(0, min(int(before), count))
    start = max(0, end - max(1, min(int(limit), 80)))
    messages = [json.loads(value[0]) for value in db.execute(
        'SELECT message_json FROM conversation_messages WHERE user_uid=? AND conversation_id=? '
        'AND position>=? AND position<? ORDER BY position', (user, conversation, start, end))]
    return {'id':row[0], 'title':row[1], 'favorite':bool(row[2]), 'createdAt':row[3], 'updatedAt':row[4],
            'messages':messages, 'messageOffset':start, 'messageCount':count, 'hasEarlier':start > 0}


def get_conversation(path: Path, user, conversation, *, before=None, limit=60):
    with closing(sqlite3.connect(path)) as db, db:
        return read_page(db, user, conversation, before=before, limit=limit)


def list_conversations(path: Path, user, limit=40):
    return conversation_listing(path, user, limit)['conversations']


def conversation_listing(path: Path, user, limit=40):
    with closing(sqlite3.connect(path)) as db, db:
        # 메시지 페이지 밖의 대화도 같은 스냅샷에서 확인해야 삭제와 미조회 상태를 구분할 수 있다.
        db.execute('BEGIN')
        ids = db.execute('SELECT conversation_id FROM conversations WHERE user_uid=? '
                         'ORDER BY favorite DESC,updated_at DESC', (user,)).fetchall()
        return {'conversations': [read_page(db,user,row[0]) for row in ids[:max(1,min(int(limit),100))]],
                'conversationIds': [row[0] for row in ids]}


def save_conversation(path: Path, user, conversation, title, messages, favorite, created_at='',
                      expected_updated_at=None, *, start_index=None, truncate=True):
    values = safe_messages(messages)
    title = str(title or '새 대화').strip()[:120] or '새 대화'
    stamp = datetime.now(timezone.utc).isoformat(timespec='microseconds')
    limit_count = clamp_int(os.getenv('CONVERSATION_MAX_PER_USER','200'),1,5000) or 200
    limit_total = clamp_int(os.getenv('CONVERSATION_MAX_TOTAL_BYTES_PER_USER','50000000'),1000000,1000000000) or 50000000
    # 1MB는 전송 요청의 제한이다. 대화 전체는 사용자별 보관 용량 안에서 여러 요청으로 저장한다.
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        old = db.execute('SELECT updated_at,created_at FROM conversations WHERE user_uid=? AND conversation_id=?', (user,conversation)).fetchone()
        if old is None and expected_updated_at:
            raise ConversationDeletedError('다른 창이나 기기에서 삭제된 대화입니다.')
        if expected_updated_at is not None and expected_updated_at != (old[0] if old else ''):
            raise ConversationConflictError('다른 기기에서 대화가 변경되었습니다. 현재 기록을 보존한 뒤 다시 동기화해 주세요.')
        count = db.execute('SELECT COUNT(*) FROM conversations WHERE user_uid=?',(user,)).fetchone()[0]
        if old is None and count >= limit_count:
            raise ConversationQuotaError('사용자별 대화 저장 개수 제한을 초과했습니다.')
        if old:
            hydrate_legacy(db,user,conversation)
        previous_count = db.execute('SELECT COUNT(*) FROM conversation_messages WHERE user_uid=? AND conversation_id=?',(user,conversation)).fetchone()[0]
        if start_index is None and previous_count > 60 and len(values) < previous_count:
            raise ConversationConflictError('이전 버전의 짧은 대화로 전체 기록을 덮을 수 없습니다. 새로고침 후 다시 저장해 주세요.')
        start = 0 if start_index is None else int(start_index)
        if start < 0 or start > previous_count:
            raise ConversationConflictError('저장할 메시지 순번이 서버 기록과 일치하지 않습니다. 기록을 다시 불러와 주세요.')
        db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?) ON CONFLICT(user_uid,conversation_id) '
                   'DO UPDATE SET title=excluded.title,messages_json=excluded.messages_json,favorite=excluded.favorite,updated_at=excluded.updated_at',
                   (user,conversation,title,'[]',int(bool(favorite)),old[1] if old else str(created_at or stamp)[:40],stamp))
        end = start + len(values)
        if truncate:
            db.execute('DELETE FROM conversation_messages WHERE user_uid=? AND conversation_id=? AND position>=?',(user,conversation,start))
        else:
            db.execute('DELETE FROM conversation_messages WHERE user_uid=? AND conversation_id=? AND position>=? AND position<?',(user,conversation,start,end))
        write_rows(db,user,conversation,start,values)
        total = db.execute('SELECT COALESCE(SUM(message_bytes),0) FROM conversation_messages WHERE user_uid=?',(user,)).fetchone()[0]
        total += db.execute('SELECT COALESCE(SUM(length(CAST(title AS BLOB))),0) FROM conversations WHERE user_uid=?',(user,)).fetchone()[0]
        if total > limit_total:
            raise ConversationQuotaError('사용자별 대화 저장 용량 제한을 초과했습니다. 기존 기록은 보존했습니다.')
        return read_page(db,user,conversation)


def delete_conversation(path: Path, user, conversation):
    with closing(sqlite3.connect(path)) as db, db:
        deleted = db.execute('DELETE FROM conversations WHERE user_uid=? AND conversation_id=?',(user,conversation)).rowcount
        if deleted:
            db.execute('DELETE FROM conversation_messages WHERE user_uid=? AND conversation_id=?',(user,conversation))
        return bool(deleted)


def delete_conversations(path: Path, user: str, ids: list[str], *, all_conversations: bool = False) -> list[str]:
    if not user or (all_conversations and ids) or (not all_conversations and not ids):
        raise ValueError('삭제할 대화 범위가 올바르지 않습니다.')
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('BEGIN IMMEDIATE')
        parameters: list[str] = [user]
        condition = 'user_uid=?'
        if not all_conversations:
            unique = list(dict.fromkeys(ids))
            condition += ' AND conversation_id IN (' + ','.join('?' for _ in unique) + ')'
            parameters.extend(unique)
        deleted = [row[0] for row in db.execute('SELECT conversation_id FROM conversations WHERE ' + condition,
                                               parameters)]
        db.execute('DELETE FROM conversation_messages WHERE ' + condition, parameters)
        db.execute('DELETE FROM conversations WHERE ' + condition, parameters)
        return sorted(deleted)
