"""완전한 최신 DB 목록으로 제목을 찾고 후보의 현재 속성을 재확인한다."""
from datetime import datetime, timezone
import json
import time

from backend.common import normalize_for_match
from backend.notion.complete_snapshot import _time, catalog_max_age_seconds
from backend.notion.mapping import notion_title
from backend.notion.property_snapshot import pending_properties, read_full_properties


def _catalog_candidates(index, database_ids, title):
    """본문 완전성과 별개로 목록·제목·하위 문서 속성의 최신성을 증명한다."""
    with index._database() as db:
        placeholders, params = index._scope(database_ids)
        if not placeholders:
            return None
        catalogs = {row['database_id']: dict(row) for row in db.execute(
            f'SELECT * FROM catalogs WHERE fingerprint=? AND database_id IN ({placeholders})', params)}
        now = time.time()
        if len(catalogs) != len(params) - 1 or any(
            not row['complete'] or not 0 <= now - _time(row['updated_at']) < catalog_max_age_seconds()
            for row in catalogs.values()
        ):
            return None
        exclusions = {row['page_id']: row['observed_at'] for row in db.execute(
            'SELECT page_id, observed_at FROM page_exclusions WHERE fingerprint=?', [index.fingerprint])}
        candidates = []
        count = excluded = 0
        observed_at = [row['updated_at'] for row in catalogs.values()]
        for row in db.execute(
            f'SELECT page_id, database_id, raw_page, updated_at FROM pages '
            f'WHERE fingerprint=? AND database_id IN ({placeholders})', params
        ):
            count += 1
            catalog_at = _time(catalogs[row['database_id']]['updated_at'])
            if _time(exclusions.get(row['page_id'], '')) >= catalog_at:
                excluded += 1
                continue
            raw = json.loads(row['raw_page'])
            if raw.get('parent', {}).get('type') != 'database_id':
                if not 0 <= now - _time(row['updated_at']) < catalog_max_age_seconds():
                    return None
                observed_at.append(row['updated_at'])
            # 잘린 제목은 후보 발견 자체를 보장하지 못한다. 다른 속성은 후보를
            # 실시간 재확인할 때 끝 커서까지 읽으므로 무관한 본문을 요구하지 않는다.
            if any(prop.get('type') == 'title' for _name, prop in pending_properties(raw)):
                return None
            if normalize_for_match(notion_title(raw.get('properties', {}))) == title:
                candidates.append((row['database_id'], raw))
        return candidates, {
            'snapshot_as_of': min(observed_at),
            'scanned_count': count, 'excluded_document_count': excluded,
        }


def _current_candidates(index, candidates, read_scope, constraints, budget):
    """직접 DB 문서는 제목 필터로 묶어 읽고, 독립 하위 문서는 개별 확인한다."""
    from backend.notion import search
    from backend.notion.body_index_sync import _ReadFailure, _page_ancestors, _request, _valid_page
    allowed = {identifier.replace('-', '').lower() for identifier in read_scope}
    stored = {str(raw['id']).replace('-', ''): raw for _database_id, raw in candidates}
    direct = list(dict.fromkeys(database_id for database_id, raw in candidates
                                if raw.get('parent', {}).get('type') == 'database_id'))
    current = []
    seen = set()

    def append(database_id, raw):
        identifier = str(raw.get('id') or '').replace('-', '')
        if identifier in seen:
            return
        parents = _page_ancestors(raw, budget)
        if not _valid_page(raw, identifier, database_id, allowed, parents):
            raise _ReadFailure('catalog_page_unavailable')
        raw = read_full_properties(raw, budget)
        previous = stored.get(identifier, {})
        if any(raw.get(key) != previous.get(key) for key in ('last_edited_time', 'properties', 'parent')):
            index.upsert_page(database_id, raw, parent_pages=parents)
        current.append((database_id, raw))
        seen.add(identifier)

    for database_id in direct:
        title_property = search.notion_title_property(search.notion_property_schema(database_id))
        if not title_property:
            raise _ReadFailure('catalog_title_schema_unavailable')
        payload = {'page_size': 100, 'filter': {
            'property': title_property, 'title': {'equals': constraints['exact_title']}}}
        cursors = set()
        for _ in range(100):
            if time.monotonic() >= budget.deadline or budget.requests_left <= 0:
                raise _ReadFailure('catalog_query_limit')
            budget.requests_left -= 1
            response = search.notion_post_json(
                f'{search.NOTION_API_BASE}/databases/{database_id}/query', payload, timeout=45, retries=2)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data.get('results'), list):
                raise _ReadFailure('invalid_catalog_response')
            for raw in data['results']:
                append(database_id, raw)
            if not data.get('has_more'):
                break
            cursor = data.get('next_cursor')
            if not cursor or cursor in cursors:
                raise _ReadFailure('invalid_catalog_cursor')
            cursors.add(cursor)
            payload['start_cursor'] = cursor
        else:
            raise _ReadFailure('catalog_query_limit')
    for database_id, raw in candidates:
        if raw.get('parent', {}).get('type') != 'database_id':
            append(database_id, _request(f"pages/{raw['id']}", budget))
    return current


def search_catalog_snapshot(index, database_ids, *, read_scope, constraints, terms, limit, include_blocks):
    """정확 제목·속성 조회만 처리한다. 검증 실패는 기존 실시간 조회에 맡긴다."""
    from backend.execution import CURRENT_BUDGET, checkpoint
    from backend.notion import search
    from backend.notion.body_index_sync import _Budget, _ReadFailure
    from backend.notion.client import NOTION_CONTENT_INCOMPLETE

    title = normalize_for_match(constraints.get('exact_title', ''))
    if not title:
        return None
    outcome = _catalog_candidates(index, database_ids, title)
    if outcome is None:
        return None
    candidates, proof = outcome
    # 제목이 같은 문서는 제외 상태·담당자를 적용하기 전에 모두 확인한다.
    # 저장 뒤 제외 상태에서 진행 상태로 바뀐 문서도 놓치지 않는다.
    active = CURRENT_BUDGET.get()
    deadline = min(time.monotonic() + 45, active.deadline - 5) if active else time.monotonic() + 45
    budget = _Budget(deadline, max(100, len(candidates) * 8))
    matched = []
    labels = {}
    try:
        for database_id, raw in _current_candidates(index, candidates, read_scope, constraints, budget):
            checkpoint()
            if database_id not in labels:
                label = search.fetch_notion_database_title(database_id)
                root = read_scope.get(database_id)
                if root and root != database_id:
                    label = f'{search.fetch_notion_database_title(root)} / {label}'
                labels[database_id] = label
            page = search.normalize_notion_page(raw, include_blocks=False,
                database_id=database_id, database_label=labels[database_id])
            if not search.notion_page_matches_and_enrich(page, constraints):
                continue
            page.update(match_method='notion_catalog_snapshot', matched_terms=terms,
                        match_score=search.notion_page_match_score(page, terms) + 60)
            matched.append(page)
    except _ReadFailure:
        return None
    created = (constraints.get('sort') or {}).get('field') == 'created_at' or any(
        clause.get('field') == 'created_at' for clause in constraints.get('common_filter_clauses', []))
    matched.sort(key=lambda page: search.notion_page_sort_key(page, created_at_filtered=created), reverse=True)
    shown = search.deduplicate_notion_pages_by_id(matched)[:limit]
    incomplete = []
    if include_blocks:
        for page in shown:
            body = search.fetch_notion_page_blocks(page['id'], max_blocks=search.NOTION_RESULT_BODY_MAX_BLOCKS)
            if body:
                page['content'] = '\n\n'.join((page.get('properties_text', ''), body)).strip()
            if NOTION_CONTENT_INCOMPLETE in body:
                page['content_incomplete'] = True
                incomplete.append(page['id'])
    return {'source': 'notion', 'status': 'ok', 'notion_pages': shown, 'meta': {
        **proof, 'status': 'snapshot', 'tool': 'notion_catalog_snapshot',
        'coverage_basis': 'api_properties', 'search_complete': True,
        'candidate_count_complete': True, 'candidate_count': len(matched),
        'displayed_count': len(shown), 'content_complete': not incomplete if include_blocks else None,
        'content_incomplete_page_ids': incomplete, 'content_incomplete_count': len(incomplete),
        'incomplete_reasons': [], 'errors': [], 'constraints': constraints, 'terms': terms,
        'body_index_used': False, 'database_ids': database_ids, 'scoped_database_ids': list(read_scope),
        'queriedAt': datetime.now(timezone.utc).isoformat(),
    }}
