"""공수 원문의 필드 계약. 조회·계산·검증에서 같은 날짜와 기입자를 사용한다."""
import re
from datetime import date
from typing import Any

from backend.common import format_mantis_custom_date

EFFORT_FIELD_LABELS = {'1문의공수': '문의', '2서버공수': '서버', '3PC공수': 'PC', '4QC공수': 'QC'}
EFFORT_DATE_FIELD = '고객사전달일'


def parse_effort_field_value(value: Any) -> list[tuple[str, float]]:
    return [(name.strip(), float(amount)) for name, amount in re.findall(
        r'([가-힣A-Za-z]{2,20})\s*\(\s*([0-9]+(?:\.[0-9]+)?)\s*\)', str(value or ''))]


def delivery_date(value: Any) -> str:
    normalized = format_mantis_custom_date(value)
    try:
        return date.fromisoformat(normalized).isoformat() if re.fullmatch(r'\d{4}-\d{2}-\d{2}', normalized) else ''
    except ValueError:
        return ''


def issue_delivery_date(issue: dict) -> str:
    for item in issue.get('custom_fields') or []:
        if (item.get('field') or {}).get('name') == EFFORT_DATE_FIELD:
            return delivery_date(item.get('value'))
    return delivery_date((issue.get('date_fields') or {}).get(EFFORT_DATE_FIELD))


def issue_effort_fields(issue: dict) -> dict:
    if 'custom_fields' in issue:
        return {item['field']['name']: item.get('value') for item in issue.get('custom_fields') or []
                if (item.get('field') or {}).get('name') in EFFORT_FIELD_LABELS}
    return {k: v for k, v in (issue.get('effort_fields') or {}).items() if k in EFFORT_FIELD_LABELS}


def issue_effort_people(issue: dict) -> list[str]:
    return list(dict.fromkeys(name for value in issue_effort_fields(issue).values()
                             for name, _ in parse_effort_field_value(value)))


def document_effort_fields(document: str) -> dict[str, str]:
    section = re.search(r'\[공수\]\s*\n(.*?)(?=\n\[|\Z)', document, re.S)
    return {name: value for name, value in re.findall(r'^\s*-\s*([^:\n]+):\s*([^\n]*)', section.group(1), re.M)
            if name in EFFORT_FIELD_LABELS} if section else {}
