"""대화 메시지 검증과 SQLite 행 직렬화."""
import json
from backend.schemas import ConversationMessage


def safe_messages(messages):
    safe = []
    for raw in messages:
        try:
            safe.append(ConversationMessage.model_validate(raw).model_dump(exclude_none=True))
        except (TypeError, ValueError):
            continue
    return safe


def write_rows(db, user, conversation, start, messages):
    rows = []
    for index, message in enumerate(messages, start):
        value = json.dumps(message, ensure_ascii=False)
        rows.append((user, conversation, index, value, len(value.encode('utf-8'))))
    db.executemany('INSERT OR REPLACE INTO conversation_messages VALUES (?,?,?,?,?)', rows)
