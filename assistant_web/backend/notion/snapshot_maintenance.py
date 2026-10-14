"""전체 목록과 달라진 본문만 갱신하여 검색 가능한 저장본을 유지한다."""
import time

from backend.notion.body_index_sync import (
    CrawlLimits, _Budget, _ReadFailure, _request, _page_ancestors, _valid_page,
    validate_index_candidate, sync_notion_body_index,
)
from backend.notion.complete_snapshot import coverage, _state
from backend.notion.body_index import page_belongs_to_database


def refresh_page(index, identifier, database_ids, deadline):
    with index._database() as db:
        row = db.execute('SELECT page_id, database_id, raw_page FROM pages WHERE fingerprint=? AND page_id=?',
                         [index.fingerprint, identifier]).fetchone()
        if row is None:
            return False
        record = dict(row)
    # 자식 목록은 DB API에 열거되지 않는다. 현재 부모 체인과 수정본을
    # 대조한 뒤, 이미 완전한 동일 본문이면 재사용할 수 있다.
    import json
    parent_type = json.loads(record['raw_page']).get('parent', {}).get('type')
    if parent_type in {'page_id', 'block_id'}:
        try:
            budget = _Budget(min(deadline, time.monotonic() + 30), 100)
            raw = _request(f'pages/{identifier}', budget)
            if raw.get('archived') or raw.get('in_trash'):
                index.exclude_page(identifier, 'archived')
                return True
            parents = _page_ancestors(raw, budget)
            memberships = [key for key in database_ids if page_belongs_to_database(raw, key, parents)]
            if memberships:
                record['database_id'] = memberships[0]
            if not _valid_page(raw, identifier, record['database_id'], set(database_ids), parents):
                known_outside = any(
                    (obj.get('parent') or {}).get('type') in {'database_id', 'workspace'}
                    for obj in [raw, *parents.values()])
                if not memberships and known_outside:
                    index.exclude_page(identifier, 'outside_scope')
                    return True
                index.record_failure(identifier, 'page_unavailable_or_moved')
                return False
            index.upsert_page(record['database_id'], raw, parent_pages=parents)
            record['raw_page'] = json.dumps(raw)
            with index._database() as db:
                pending = _state(index, db, database_ids, page_ids=[identifier])[3]['pending_page_ids']
            if identifier not in pending:
                return True
        except _ReadFailure as exc:
            if str(exc) in {'http_403', 'http_404'}:
                index.exclude_page(identifier, str(exc))
                return True
            index.record_failure(identifier, str(exc))
            return False
    result = validate_index_candidate(record, database_ids, index=index, deadline=deadline,
                                      limits=CrawlLimits.configured(), catalog_page=json.loads(record["raw_page"]))
    return bool(result and result.get('accessible_content_complete'))


def refresh_snapshot(index, database_ids, *, max_seconds=3600, max_pages=1000, refresh_catalog=True):
    deadline = time.monotonic() + max_seconds
    if refresh_catalog:
        sync_notion_body_index(database_ids, max_pages=0, max_seconds=max_seconds, index=index)
    attempted = 0
    seen = set()
    while time.monotonic() < deadline and attempted < max_pages:
        proof = coverage(index, database_ids)
        if not proof['catalog_complete']:
            break
        pending = [pid for pid in proof['pending_page_ids'] if pid not in seen]
        if not pending:
            break
        for identifier in pending:
            if time.monotonic() >= deadline or attempted >= max_pages:
                break
            seen.add(identifier)
            refresh_page(index, identifier, database_ids, deadline)
            attempted += 1
    return {**coverage(index, database_ids), 'attempted_page_count': attempted}
