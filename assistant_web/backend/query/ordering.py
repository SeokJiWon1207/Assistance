"""표시 날짜와 별개로 원본 시각을 보존하는 이슈 정렬 키."""
from datetime import datetime, timezone


def issue_order_key(issue, field):
    if field == 'issue_id':
        return (int(issue.get('id') or 0), int(issue.get('id') or 0))
    value = str((issue.get('_sort_values') or {}).get(field) or issue.get(field) or '')
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        stamp = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed
        timestamp = stamp.timestamp()
    except ValueError:
        timestamp = float('-inf')
    return (timestamp, int(issue.get('id') or 0))
