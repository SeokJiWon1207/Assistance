"""전체 목록·수정본과 대조한 API 읽기 범위 저장본만 전수 검색한다."""
from collections import Counter
from datetime import datetime, timezone
import json
from typing import Any

from backend.common import normalize_for_match
from backend.notion.body_index import BodyIndex, BodyIndexError, CONTENT_POLICY_VERSION, open_body_index
from backend.notion.mapping import notion_property_to_text


API_EXCLUSIONS = frozenset({'unsupported_block', 'foreign_synced_block',
                            'synced_source_outside_scope', 'http_403', 'http_404'})
CATALOG_TTL_SECONDS = 86400


def _time(value: str) -> float:
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.timestamp() if result.tzinfo else 0
    except (ValueError, TypeError, AttributeError):
        return 0


def _state(index: BodyIndex, db, database_ids: list[str], page_ids=None, allowed_database_ids=None):
    placeholders, params = index._scope(database_ids)
    if not placeholders:
        return {}, {}, {}, {'complete': False, 'page_count': 0, 'pending_page_ids': []}
    catalogs = {r['database_id']: dict(r) for r in db.execute(
        f'SELECT * FROM catalogs WHERE fingerprint=? AND database_id IN ({placeholders})', params)}
    page_where = f"fingerprint=? AND database_id IN ({placeholders})"
    page_params = list(params)
    if page_ids is not None:
        page_where += " AND page_id IN (" + ",".join("?" for _ in page_ids) + ")"
        page_params.extend(page_ids)
    rows = {r['page_id']: dict(r) for r in db.execute(
        'SELECT page_id, database_id, last_edited_time, indexed_edited_time, indexed_at, '
        'content_complete, needs_refresh, last_error, updated_at, '
        "json_extract(raw_page, '$.parent.type') AS parent_type, "
        f'{index._current_policy_predicate(db)} AS policy_current FROM pages '
        f'WHERE {page_where}', page_params)}
    has_coverage = db.execute("SELECT 1 FROM sqlite_master WHERE name='collection_coverage'").fetchone()
    diagnostics = {r['page_id']: dict(r) for r in db.execute(
        f'SELECT c.* FROM collection_coverage c JOIN pages p USING(fingerprint,page_id) '
        f'WHERE c.fingerprint=? AND p.database_id IN ({placeholders})', params)} if has_coverage else {}
    has_synced = db.execute("SELECT 1 FROM sqlite_master WHERE name='synced_segments'").fetchone()
    synced = {r[0] for r in db.execute('SELECT DISTINCT page_id FROM synced_segments WHERE fingerprint=?',
                                      [index.fingerprint])} if has_synced else set()
    from backend.notion.property_snapshot import pending_properties
    property_pending = set()
    for row in db.execute(f"SELECT page_id, raw_page FROM pages WHERE {page_where}", page_params):
        if pending_properties(json.loads(row['raw_page'])):
            property_pending.add(row['page_id'])
    allowed_sources = set(allowed_database_ids if allowed_database_ids is not None else database_ids)
    allowed_sources = {identifier.replace('-', '') for identifier in allowed_sources}
    invalid_sources = set()
    if has_synced:
        for entry in db.execute('SELECT page_id, data FROM synced_segments WHERE fingerprint=?', [index.fingerprint]):
            source_db = str(json.loads(entry['data']).get('source_database_id') or '').replace('-', '')
            if source_db not in allowed_sources:
                invalid_sources.add(entry['page_id'])
    now = datetime.now(timezone.utc).timestamp()
    has_exclusions = db.execute("SELECT 1 FROM sqlite_master WHERE name='page_exclusions'").fetchone()
    exclusions = {r['page_id']: dict(r) for r in db.execute(
        'SELECT * FROM page_exclusions WHERE fingerprint=?', [index.fingerprint])} if has_exclusions else {}
    catalog_complete = len(catalogs) == len(params) - 1 and all(
        r['complete'] and 0 <= now - _time(r['updated_at']) < CATALOG_TTL_SECONDS for r in catalogs.values())
    pending = []
    types: Counter[str] = Counter()
    excluded_pages = 0
    excluded_documents = 0
    for identifier, row in rows.items():
        catalog_at = _time(catalogs.get(row['database_id'], {}).get('updated_at', ''))
        exclusion = exclusions.get(identifier)
        if exclusion and _time(exclusion['observed_at']) >= catalog_at:
            excluded_documents += 1
            continue
        diagnostic = diagnostics.get(identifier, {})
        data = json.loads(diagnostic.get('data', '{}'))
        if any(ref.get('database_id', '').replace('-', '') not in allowed_sources
               for ref in data.get('verified_synced_references', [])):
            invalid_sources.add(identifier)
        valid_diagnostic = (diagnostic.get('policy') == CONTENT_POLICY_VERSION
                            and diagnostic.get('revision') == row['last_edited_time'])
        reasons = set(str(row['last_error']).split(',')) - {''}
        complete = (row['policy_current'] and row['content_complete'] and not row['needs_refresh']
                    and not reasons and row['indexed_edited_time'] == row['last_edited_time'])
        if valid_diagnostic and reasons <= API_EXCLUSIONS and (
                data.get('supported_content_complete') or data.get('accessible_content_complete')):
            complete = bool(row['policy_current'])
        observed = diagnostic.get('observed_at', '') if valid_diagnostic else row['indexed_at']
        # 같은 분의 변경과 독립 갱신되는 동기화 원본·자식은 목록 시각만으로 확정하지 않는다.
        if _time(observed) <= _time(row['last_edited_time']) + 60:
            complete = False
        if (identifier in synced or data.get('synced_reference_ids')
                or 'foreign_synced_block' in reasons) and _time(observed) < catalog_at:
            complete = False
        if row['parent_type'] in {'page_id', 'block_id'} and _time(row['updated_at']) < catalog_at:
            complete = False
        if identifier in property_pending or identifier in invalid_sources or exclusion:
            complete = False
        if not complete:
            pending.append(identifier)
        elif valid_diagnostic:
            unsupported = data.get('unsupported_elements') or []
            if unsupported or reasons - {'unsupported_block'}:
                excluded_pages += 1
            types.update(str(item.get('block_type') or 'unknown') for item in unsupported)
            if reasons - {'unsupported_block'}:
                types.update(['unavailable_synced_source'])
    info = {'complete': bool(catalog_complete and not pending), 'catalog_complete': bool(catalog_complete),
            'page_count': len(rows), 'pending_page_ids': pending, 'pending_page_count': len(pending),
            'excluded_page_count': excluded_pages, 'excluded_element_count': sum(types.values()),
            'excluded_document_count': excluded_documents,
            'excluded_element_types': dict(types),
            'snapshot_as_of': min((r['updated_at'] for r in catalogs.values()), default='')}
    return rows, diagnostics, catalogs, info


def coverage(index: BodyIndex, database_ids: list[str]) -> dict[str, Any]:
    with index._database() as db:
        return _state(index, db, database_ids)[3]


def _records(index: BodyIndex, db, database_ids: list[str], terms: list[str], diagnostics):
    placeholders, params = index._scope(database_ids)
    if not placeholders:
        return []
    normalized = [normalize_for_match(term) for term in terms if normalize_for_match(term)]
    has_exclusions = db.execute("SELECT 1 FROM sqlite_master WHERE name='page_exclusions'").fetchone()
    excluded = {r[0] for r in db.execute('SELECT page_id FROM page_exclusions WHERE fingerprint=?',
                                        [index.fingerprint])} if has_exclusions else set()
    # 원문은 한 페이지씩 읽는다. 수백 MB의 raw_page를 한꺼번에 적재하지 않는다.
    for row in db.execute(f'SELECT * FROM pages WHERE fingerprint=? AND database_id IN ({placeholders})', params):
        if row['page_id'] in excluded:
            continue
        record = index._record(db, row)
        diagnostic = diagnostics.get(row['page_id'], {})
        data = json.loads(diagnostic.get('data', '{}'))
        if (diagnostic.get('revision') == row['last_edited_time']
                and diagnostic.get('policy') == CONTENT_POLICY_VERSION
                and (data.get('supported_content_complete') or data.get('accessible_content_complete'))):
            # 최근 API 지원 본문이 완전하면 과거 완전본의 삭제된 문구를 섞지 않는다.
            partial = [b for b in record['candidate_segments'] if b.get('snapshot') == 'partial'
                       and b.get('indexed_edited_time') == row['last_edited_time']
                       and b.get('indexed_at') == diagnostic.get('observed_at')]
            current = [b for b in record['segments'] if b.get('indexed_at') == diagnostic.get('observed_at')]
            record['segments'] = (partial or current) + [b for b in record['segments'] if b.get('synced_source_id')]
        body = '\n'.join(b['text'] for b in record['segments'])
        properties = '\n'.join(notion_property_to_text(prop)
                               for prop in record['raw_page'].get('properties', {}).values())
        text = normalize_for_match(properties + '\n' + body)
        if normalized and not any(term in text for term in normalized):
            continue
        yield {**record, 'content': body, 'complete': True, 'snapshot_property_text': properties,
               'synced_reference_ids': data.get('synced_reference_ids', []),
               'verified_synced_references': data.get('verified_synced_references', []),
               'snapshot_observed_at': diagnostic.get('observed_at') or row['indexed_at']}


def records(index: BodyIndex, database_ids: list[str], terms: list[str]) -> list[dict[str, Any]]:
    with index._database() as db:
        _, diagnostics, _, _ = _state(index, db, database_ids)
        return list(_records(index, db, database_ids, terms, diagnostics))


def read_complete(index: BodyIndex, database_ids: list[str], terms: list[str]):
    # 증명과 결과를 같은 SQLite 읽기 트랜잭션에서 확인한다.
    with index._database() as db:
        _, diagnostics, _, info = _state(index, db, database_ids)
        if not info['complete']:
            return None
        candidates = list(_records(index, db, database_ids, terms, diagnostics))
        return {'candidates': candidates, 'coverage': info,
                'candidate_count': len(candidates), 'validation_complete': True, 'incomplete_reasons': []}


def _current_results(index, pages, candidates, allowed):
    """표시 직전에 접근·소속·수정본을 확인한다. 변경되면 실시간 검색으로 돌아간다."""
    import time
    from backend.execution import CURRENT_BUDGET
    from backend.notion.body_index_sync import _Budget, _ReadFailure, _request, _page_ancestors, _valid_page, _synced_source
    records_by_id = {str(row['raw_page']['id']).replace('-', ''): row for row in candidates}
    active = CURRENT_BUDGET.get()
    deadline = min(time.monotonic() + 45, active.deadline - 5) if active else time.monotonic() + 45
    budget = _Budget(deadline, max(100, len(pages) * 8))
    sources_checked = {}
    for page in pages:
        identifier = str(page['id']).replace('-', '')
        record = records_by_id[identifier]
        try:
            raw = _request(f'pages/{identifier}', budget)
            parents = _page_ancestors(raw, budget)
            valid = _valid_page(raw, identifier, record['database_id'], set(allowed), parents)
            source_ids = {s['synced_source_id'] for s in record.get('segments', []) if s.get('synced_source_id')}
            source_ids.update(ref['source_id'] for ref in record.get('verified_synced_references', []))
            for source_id in source_ids:
                if source_id not in sources_checked:
                    sources_checked[source_id] = _synced_source(source_id, set(allowed), budget)[0]
                source = sources_checked[source_id]
                if _time(source.get('last_edited_time', '')) + 60 >= _time(record.get('snapshot_observed_at', '')):
                    valid = False
        except _ReadFailure as exc:
            index.record_failure(identifier, str(exc))
            return False
        if not valid or raw['last_edited_time'] != record['raw_page']['last_edited_time']:
            index.record_failure(identifier, 'snapshot_changed_or_unavailable')
            return False
    return True


def _scope_current(read_scope):
    from backend.config import RUNTIME_DIR
    from backend.notion.read_scope import child_database_search_enabled
    if not child_database_search_enabled():
        return True
    try:
        saved = json.loads((RUNTIME_DIR / 'notion_read_scope.json').read_text())
        age = datetime.now(timezone.utc).timestamp() - _time(saved.get('generated_at', ''))
        return (saved.get('scope_complete') is True and saved.get('read_scope') == read_scope
                and 0 <= age < CATALOG_TTL_SECONDS)
    except (OSError, ValueError):
        return False


def search_complete_snapshot(database_ids, *, terms, read_scope, constraints,
                             pre_body_constraints, body_clauses, keyword_clauses, limit):
    """전수 대조된 저장본만 사용하고, 불확실하면 기존 API 검색에 맡긴다."""
    import sqlite3
    import requests
    from fastapi import HTTPException
    from backend.notion.search import _indexed_body_matches, notion_page_sort_key
    try:
        index = open_body_index()
        if index is None or not _scope_current(read_scope):
            return None
        positive = [str(clause['value']) for clause in body_clauses if clause['operator'] == 'contains']
        discovery_terms = list(dict.fromkeys([*positive, *terms]))
        from itertools import batched
        created = any(c.get('field') == 'created_at' for c in constraints.get('common_filter_clauses', []))
        def rank(p):
            return (bool(terms) and p.get('index_title_match', False),
                    notion_page_sort_key(p, created_at_filtered=created))
        shown: list[dict[str, Any]] = []
        selected_records = {}
        total = 0
        with index._database() as db:
            _, diagnostics, _, proof = _state(index, db, database_ids, allowed_database_ids=list(read_scope))
            if not proof['complete']:
                return None
            # 전수 판정·검색은 같은 읽기 트랜잭션에서 수행한다. 전체 원문을
            # 메모리에 쌓지 않고 표시할 상위 문서만 보존하며 총수는 끝까지 센다.
            for chunk in batched(_records(index, db, database_ids, discovery_terms, diagnostics), 100):
                from backend.execution import checkpoint
                checkpoint()
                indexed = {'candidates': chunk, 'incomplete_reasons': []}
                pages = _indexed_body_matches(indexed, constraints, pre_body_constraints,
                                              body_clauses, keyword_clauses, terms, read_scope)
                if indexed['incomplete_reasons']:
                    return None
                total += len(pages)
                shown = sorted([*shown, *pages], key=rank, reverse=True)[:limit]
                selected_records.update({str(r['raw_page']['id']).replace('-', ''): r for r in chunk})
                keep = {str(p['id']).replace('-', '') for p in shown}
                selected_records = {key: r for key, r in selected_records.items() if key in keep}
        if not _current_results(index, shown, list(selected_records.values()), list(read_scope)):
            return None
        return {'source': 'notion', 'status': 'ok', 'notion_pages': shown, 'meta': {
            'status': 'snapshot', 'tool': 'notion_complete_snapshot',
            'snapshot_as_of': proof['snapshot_as_of'], 'queriedAt': datetime.now(timezone.utc).isoformat(),
            'coverage_basis': 'api_readable', 'search_complete': True, 'candidate_count_complete': True,
            'content_complete': True, 'incomplete_reasons': [], 'displayed_count': len(shown),
            'candidate_count': total, 'scanned_count': proof['page_count'], 'body_index': proof,
            'body_index_used': True, 'constraints': constraints, 'terms': terms,
            'database_ids': database_ids, 'scoped_database_ids': list(read_scope), 'errors': [],
            'excluded_element_count': proof['excluded_element_count'],
            'excluded_document_count': proof['excluded_document_count'],
            'excluded_element_types': proof['excluded_element_types']}}
    except (BodyIndexError, sqlite3.Error, OSError, ValueError, HTTPException, requests.RequestException):
        return None
