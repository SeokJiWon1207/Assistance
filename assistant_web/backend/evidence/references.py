"""원문 출처 라벨과 시간 정렬 계약."""
import html
import re
from typing import Any

_FIELD_LABELS = {
    'summary': '제목',
    'description': '본문',
    'additional_information': '추가 정보',
    'steps_to_reproduce': '재현 단계',
    '사유 및 원인': '사유 및 원인',
    '조치 및 개선': '조치 및 개선',
}
_DEFAULT_LABEL = '원문'
SOURCE_LABEL_PATTERN = '|'.join([*map(re.escape, _FIELD_LABELS.values()), _DEFAULT_LABEL, r'노트(?:\s+\d+)?'])


def _label(value: Any) -> str:
    text = html.escape(" ".join(str(value).split()))
    return re.sub(r"([\\`*_{}\[\]()|~])", r"\\\1", text)


def _order(row):
    return str(row.get('created_at') or ''), int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0


def source_label(row: dict[str, Any]) -> str:
    if row.get('note_id') is not None:
        return f"노트 {_label(row['note_id'])}"
    field = str(row.get('field') or '')
    if field.startswith('note:'):
        return '노트'
    return _FIELD_LABELS.get(field, _DEFAULT_LABEL)


def _reference(row):
    date = str(row.get('created_at') or '')[:10]
    return f"(#{row['issue_id']} · {source_label(row)}" + (f' · {_label(date)}' if date else '') + ')'
