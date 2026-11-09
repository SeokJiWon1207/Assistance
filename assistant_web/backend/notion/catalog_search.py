"""DB 목록에서 제목 후보를 찾고 원문 API로 현재 문서를 확인한다."""
from backend.notion import crawl_transport
from backend.notion import read_api
from backend.notion import schema as notion_schema
from backend.notion import transport
from datetime import datetime, timezone
import json
import time
from typing import Any

import requests
from fastapi import HTTPException

from backend.utils.text import normalize_for_match
from backend.notion.snapshot_state import _time, catalog_max_age_seconds
from backend.notion.values import notion_title
from backend.notion.property_snapshot import pending_properties, read_full_properties


def _catalog_candidates(index, database_ids, title, *, topic_terms=(), hints_only=False):
    """완료된 DB 목록을 읽는다. 만료된 제목은 원문을 확인할 후보 ID로만 사용한다."""
    with index._database() as db:
        placeholders, params = index._scope(database_ids)
        if not placeholders:
            return None
        catalogs = {row['database_id']: dict(row) for row in db.execute(
            f'SELECT * FROM catalogs WHERE fingerprint=? AND database_id IN ({placeholders})', params)}
        now = time.time()
        if len(catalogs) != len(params) - 1 or any(not row['complete'] for row in catalogs.values()):
            return None
        if not hints_only and any(
                not 0 <= now - _time(row['updated_at']) < catalog_max_age_seconds() for row in catalogs.values()):
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
                if not hints_only and not 0 <= now - _time(row['updated_at']) < catalog_max_age_seconds():
                    return None
                observed_at.append(row['updated_at'])
            # 잘린 제목은 후보 발견 자체를 보장하지 못한다. 다른 속성은 후보를
            # 실시간 재확인할 때 끝 커서까지 읽으므로 무관한 본문을 요구하지 않는다.
            if any(prop.get('type') == 'title' for _name, prop in pending_properties(raw)):
                if hints_only:
                    continue
                return None
            current_title = normalize_for_match(notion_title(raw.get('properties', {})))
            matches = all(term in current_title for term in topic_terms) if topic_terms else current_title == title
            if matches:
                candidates.append((row['database_id'], raw))
        return candidates, {
            'snapshot_as_of': min(observed_at),
            'scanned_count': count, 'excluded_document_count': excluded,
        }


def _current_candidates(index, candidates, read_scope, constraints, budget, *, topic_terms=(), individual=False):
    """직접 DB 문서는 제목 필터로 묶어 읽고, 독립 하위 문서는 개별 확인한다."""
    from backend.settings.notion import NOTION_API_BASE
    from backend.notion.transport import notion_post_json as notion_post_json
    from backend.notion.schema import notion_property_schema as notion_property_schema, notion_title_property as notion_title_property
    from backend.notion.crawl_transport import NotionReadFailure as _ReadFailure
    from backend.notion.crawl_validation import _page_ancestors, _valid_page
    allowed = {identifier.replace('-', '').lower() for identifier in read_scope}
    stored = {str(raw['id']).replace('-', ''): raw for _database_id, raw in candidates}
    direct = [] if individual else list(dict.fromkeys(database_id for database_id, raw in candidates
                                                      if raw.get('parent', {}).get('type') == 'database_id'))
    seen = set()
    incomplete = False

    def verify(database_id, raw, identifier):
        if identifier in seen:
            return
        parents = _page_ancestors(raw, budget)
        if not _valid_page(raw, identifier, database_id, allowed, parents):
            if individual:
                return None
            raise _ReadFailure('catalog_page_unavailable')
        raw = read_full_properties(raw, budget)
        previous = stored.get(identifier, {})
        if any(raw.get(key) != previous.get(key) for key in ('last_edited_time', 'properties', 'parent')):
            index.upsert_page(database_id, raw, parent_pages=parents)
        seen.add(identifier)
        return database_id, raw

    for database_id in direct:
        title_property = notion_schema.notion_title_property(notion_schema.notion_property_schema(database_id))
        if not title_property:
            raise _ReadFailure('catalog_title_schema_unavailable')
        title_filter: dict[str, Any]
        if topic_terms:
            title_filter = {'and': [{'property': title_property, 'title': {'contains': term}} for term in topic_terms]}
        else:
            title_filter = {'property': title_property, 'title': {'equals': constraints['exact_title']}}
        payload = {'page_size': 100, 'filter': title_filter}
        cursors = set()
        for _ in range(100):
            if time.monotonic() >= budget.deadline or budget.requests_left <= 0:
                raise _ReadFailure('catalog_query_limit')
            budget.requests_left -= 1
            response = transport.notion_post_json(
                f'{NOTION_API_BASE}/databases/{database_id}/query', payload, timeout=45, retries=2)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data.get('results'), list):
                raise _ReadFailure('invalid_catalog_response')
            for raw in data['results']:
                candidate = verify(database_id, raw, str(raw.get('id') or '').replace('-', ''))
                if candidate is not None:
                    yield candidate
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
        identifier = str(raw['id']).replace('-', '')
        if identifier in seen or (not topic_terms and raw.get('parent', {}).get('type') == 'database_id'):
            continue
        # DB에 열거되지 않는 하위 문서와 Notion의 contains가 띄어쓰기 때문에 놓친 제목을 확인한다.
        try:
            live = crawl_transport.request_json(f"pages/{raw['id']}", budget)
            if topic_terms and (live.get('archived') or live.get('in_trash')):
                continue
            candidate = verify(database_id, live, identifier)
        except _ReadFailure as exc:
            if topic_terms and str(exc) in {'http_403', 'http_404'}:
                continue
            if individual and time.monotonic() < budget.deadline and budget.requests_left > 0:
                incomplete = True
                continue
            raise
        if candidate is not None:
            yield candidate
    if incomplete:
        raise _ReadFailure('catalog_revalidation_incomplete')


def search_catalog_snapshot(index, database_ids, *, read_scope, constraints, terms, limit, include_blocks,
                            title_customers=(), hints_only=False):
    """목록에서 제목 후보 DB를 찾고 현재 제목·소속·속성을 API로 검증한다."""
    from backend.execution import CURRENT_BUDGET, checkpoint
    from backend.notion.constants import NOTION_RESULT_BODY_MAX_BLOCKS
    from backend.notion.matching import deduplicate_notion_pages_by_id, notion_page_match_score, notion_page_sort_key
    from backend.notion.read_api import fetch_notion_database_title as fetch_notion_database_title, fetch_notion_page_blocks as fetch_notion_page_blocks
    from backend.notion.mapping import normalize_notion_page
    from backend.notion.page_enrichment import notion_page_matches_and_enrich
    from backend.notion.crawl_transport import CrawlBudget as _Budget, NotionReadFailure as _ReadFailure
    from backend.notion.read_api import NOTION_CONTENT_INCOMPLETE

    title = normalize_for_match(constraints.get('exact_title', ''))
    topic_terms = [] if title else [normalize_for_match(term) for term in terms if term.strip()]
    if not title and not topic_terms:
        return None
    if hints_only and not (topic_terms and title_customers):
        return None
    outcome = _catalog_candidates(index, database_ids, title, topic_terms=topic_terms, hints_only=hints_only)
    if outcome is None:
        return None
    candidates, proof = outcome
    def prioritize(rows):
        customers = [normalize_for_match(value) for value in title_customers if value.strip()]

        def priority(item):
            raw = item[1]
            stored_title = normalize_for_match(notion_title(raw.get('properties', {})))
            return any(alias in stored_title for alias in customers), str(raw.get('last_edited_time') or '')

        return sorted(rows, key=priority, reverse=True)
    # 제목이 같은 문서는 제외 상태·담당자를 적용하기 전에 모두 확인한다.
    # 저장 뒤 제외 상태에서 진행 상태로 바뀐 문서도 놓치지 않는다.
    active = CURRENT_BUDGET.get()
    matches = {}
    labels = {}

    def collect(rows, *, individual=False):
        deadline = time.monotonic() + 45
        if active:
            deadline = min(deadline, active.deadline - min(5.0, active.remaining() * 0.2))
        budget = _Budget(deadline, max(100, len(rows) * 8))
        for database_id, raw in _current_candidates(index, rows, read_scope, constraints, budget,
                                                   topic_terms=terms if topic_terms else (), individual=individual):
            checkpoint()
            if topic_terms:
                live_title = normalize_for_match(notion_title(raw.get('properties', {})))
                if not all(term in live_title for term in topic_terms):
                    continue
            if database_id not in labels:
                label = read_api.fetch_notion_database_title(database_id)
                root = read_scope.get(database_id)
                if root and root != database_id:
                    label = f'{read_api.fetch_notion_database_title(root)} / {label}'
                labels[database_id] = label
            page = normalize_notion_page(raw, include_blocks=False,
                database_id=database_id, database_label=labels[database_id])
            if not notion_page_matches_and_enrich(page, constraints):
                continue
            page.update(match_method='notion_catalog_snapshot', matched_terms=terms,
                        match_score=notion_page_match_score(page, terms) + 60)
            matches[str(raw['id']).replace('-', '')] = page

    verification_complete = True
    try:
        collect(prioritize(candidates)[:limit] if hints_only else candidates, individual=hints_only)
    except (_ReadFailure, HTTPException, requests.RequestException):
        if not topic_terms:
            return None
        verification_complete = False
        if title_customers and not hints_only:
            # DB 스키마나 일괄 조회가 막혀도 갱신본에서 찾은 ID는 원문 GET으로 확인할 수 있다.
            remaining = [entry for entry in candidates if str(entry[1]['id']).replace('-', '') not in matches]
            try:
                collect(prioritize(remaining), individual=True)
                verification_complete = True
            except (_ReadFailure, HTTPException, requests.RequestException):
                pass  # 확인을 마친 원문은 이후 실시간 조회에도 전달한다.
    matched = list(matches.values())
    if topic_terms and not matched:
        return None
    reasons = ['title_search_only'] if topic_terms else []
    if not verification_complete:
        reasons.append('catalog_revalidation_incomplete')
    created = (constraints.get('sort') or {}).get('field') == 'created_at' or any(
        clause.get('field') == 'created_at' for clause in constraints.get('common_filter_clauses', []))
    matched.sort(key=lambda page: notion_page_sort_key(page, created_at_filtered=created), reverse=True)
    shown = deduplicate_notion_pages_by_id(matched)[:limit]
    incomplete = []
    if include_blocks:
        for page in shown:
            body = read_api.fetch_notion_page_blocks(page['id'], max_blocks=NOTION_RESULT_BODY_MAX_BLOCKS)
            if body:
                page['content'] = '\n\n'.join((page.get('properties_text', ''), body)).strip()
            if NOTION_CONTENT_INCOMPLETE in body:
                page['content_incomplete'] = True
                incomplete.append(page['id'])
    return {'source': 'notion', 'status': 'ok', 'notion_pages': shown, 'meta': {
        **proof, 'status': 'snapshot', 'tool': 'notion_catalog_title_search' if topic_terms else 'notion_catalog_snapshot',
        'coverage_basis': 'api_properties', 'search_complete': not topic_terms,
        'candidate_count_complete': not topic_terms, 'candidate_count': len(matched),
        'displayed_count': len(shown), 'content_complete': not incomplete if include_blocks else None,
        'content_incomplete_page_ids': incomplete, 'content_incomplete_count': len(incomplete),
        'incomplete_reasons': reasons,
        'title_search_only': bool(topic_terms),
        'result_window_complete': not hints_only and verification_complete and (not topic_terms or len(shown) >= limit),
        'errors': [], 'constraints': constraints, 'terms': terms,
        'body_index_used': False, 'database_ids': database_ids, 'scoped_database_ids': list(read_scope),
        'queriedAt': datetime.now(timezone.utc).isoformat(),
    }}
