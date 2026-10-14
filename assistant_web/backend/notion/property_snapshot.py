"""페이지 응답에서 잘릴 수 있는 속성 항목도 끝 커서까지 수집한다."""
from urllib.parse import quote, unquote
from typing import Any


def pending_properties(raw):
    if raw.get('_body_index_full_property_revision') == raw.get('last_edited_time'):
        return []
    return [(name, prop) for name, prop in raw.get('properties', {}).items()
            if prop.get('has_more') or (prop.get('type') in {'title', 'rich_text'}
                                       and len(prop.get(prop['type']) or []) >= 25)]


def read_full_properties(raw, budget):
    from backend.notion.body_index_sync import _request, _ReadFailure
    properties = dict(raw.get('properties', {}))
    for name, prop in pending_properties(raw):
        identifier = prop.get('id')
        kind = prop.get('type')
        if not identifier or kind not in {'title', 'rich_text', 'relation', 'people'}:
            raise _ReadFailure('unsupported_property_pagination')
        cursor = ''
        seen = set()
        values: list[Any] = []
        while True:
            data = _request(f"pages/{raw['id']}/properties/{quote(unquote(identifier), safe='')}", budget,
                            payload={'page_size': 100, **({'start_cursor': cursor} if cursor else {})})
            rows = data.get('results')
            if not isinstance(rows, list) or any(not isinstance(row, dict) or kind not in row for row in rows):
                raise _ReadFailure('invalid_property_response')
            values.extend(row[kind] for row in rows)
            if not data.get('has_more'):
                break
            cursor = str(data.get('next_cursor') or '')
            if not cursor or cursor in seen:
                raise _ReadFailure('invalid_property_cursor')
            seen.add(cursor)
        properties[name] = {**prop, kind: values, 'has_more': False}
    return {**raw, 'properties': properties,
            '_body_index_full_property_revision': raw['last_edited_time']}
