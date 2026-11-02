"""원문 출처 라벨과 시간 정렬 계약."""
import html
import re
from typing import Any

def _label(value: Any) -> str:
    text = html.escape(" ".join(str(value).split()))
    return re.sub(r"([\\`*_{}\[\]()|~])", r"\\\1", text)
def _order(row):
    return str(row.get('created_at') or ''), int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0


def _reference(row):
    kind = f"노트 {_label(row['note_id'])}" if row.get('note_id') is not None else f"필드 {_label(row['field'])}"
    date = str(row.get('created_at') or '')[:10]
    return f"(#{row['issue_id']} · {kind}" + (f' · {_label(date)}' if date else '') + ')'
