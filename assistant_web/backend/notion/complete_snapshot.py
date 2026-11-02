"""전체 목록·수정본과 대조한 API 읽기 범위 저장본만 전수 검색한다."""
from backend.notion.snapshot_state import (
    API_EXCLUSIONS as API_EXCLUSIONS,
    catalog_max_age_seconds as catalog_max_age_seconds,
    _time as _time,
    _state as _state,
    coverage as coverage,
)
from backend.notion import crawl_transport
from collections import Counter as Counter
from datetime import datetime, timezone
import json
import os
import threading
import time
from typing import Any

from backend.utils.text import normalize_for_match
from backend.notion.body_index import BodyIndex, BodyIndexError, CONTENT_POLICY_VERSION, open_body_index
from backend.notion.values import notion_property_to_text


# 요청 안에서 다시 읽을 보류 페이지 수와 시간. 넘으면 기존처럼 실시간 검색에 맡긴다.
def inline_refresh_max_pages() -> int:
    try:
        return max(0, min(int(os.getenv("NOTION_SNAPSHOT_INLINE_REFRESH_MAX_PAGES", "5")), 20))
    except ValueError:
        return 5


def inline_refresh_seconds() -> float:
    try:
        return max(1.0, min(float(os.getenv("NOTION_SNAPSHOT_INLINE_REFRESH_SECONDS", "10")), 30.0))
    except ValueError:
        return 10.0


# 다시 읽지 못한 페이지는 잠시 건너뛰어 같은 실패 비용을 매 요청 반복하지 않는다.
INLINE_REFRESH_COOLDOWN_SECONDS = 600
_inline_refresh_failures: dict[str, float] = {}
_inline_refresh_failures_lock = threading.Lock()
_STALE = object()


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
    """표시 직전에 접근·소속·수정본을 확인한다. 변경된 페이지는 보류로 표시하고 False를 돌려준다."""
    import time
    from backend.execution import CURRENT_BUDGET
    from backend.notion.crawl_transport import CrawlBudget as _Budget, NotionReadFailure as _ReadFailure
    from backend.notion.crawl_validation import _page_ancestors, _valid_page, _synced_source
    records_by_id = {str(row['raw_page']['id']).replace('-', ''): row for row in candidates}
    active = CURRENT_BUDGET.get()
    deadline = min(time.monotonic() + 45, active.deadline - 5) if active else time.monotonic() + 45
    budget = _Budget(deadline, max(100, len(pages) * 8))
    sources_checked = {}
    for page in pages:
        identifier = str(page['id']).replace('-', '')
        record = records_by_id[identifier]
        try:
            raw = crawl_transport.request_json(f'pages/{identifier}', budget)
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
    from backend.notion.read_scope import child_database_search_enabled, _read_scope_max_age
    if not child_database_search_enabled():
        return True
    try:
        saved = json.loads((RUNTIME_DIR / 'notion_read_scope.json').read_text())
        age = datetime.now(timezone.utc).timestamp() - _time(saved.get('generated_at', ''))
        return (saved.get('scope_complete') is True and saved.get('read_scope') == read_scope
                and 0 <= age <= _read_scope_max_age())
    except (OSError, ValueError):
        return False


def _refresh_pending_inline(index, database_ids, allowed):
    """보류 페이지가 몇 건뿐이면 요청 안에서 다시 읽어 저장본 전체를 실시간 검색으로 넘기지 않는다.

    다시 읽은 페이지 수를 돌려준다. 하나라도 확정하지 못하면 None이다."""
    import fcntl
    from backend.config import RUNTIME_DIR
    from backend.execution import CURRENT_BUDGET
    from backend.notion.body_index_sync import CrawlLimits
    from backend.notion.snapshot_maintenance import refresh_page
    with index._database() as db:
        proof = _state(index, db, database_ids, allowed_database_ids=allowed)[3]
    pending = proof['pending_page_ids']
    if not proof['catalog_complete'] or not pending or len(pending) > inline_refresh_max_pages():
        return None
    started = time.monotonic()
    with _inline_refresh_failures_lock:
        if any(started - _inline_refresh_failures.get(identifier, float('-inf')) < INLINE_REFRESH_COOLDOWN_SECONDS
               for identifier in pending):
            return None
    deadline = started + inline_refresh_seconds()
    active = CURRENT_BUDGET.get()
    if active is not None:
        # 실패하면 실시간 검색을 이어가야 하므로 요청 예산을 모두 쓰지 않는다.
        deadline = min(deadline, active.deadline - 10)
    # 매시간 갱신과 같은 잠금을 써서 저장본을 동시에 고치지 않는다. 사용 중이면 기다리지 않는다.
    with (RUNTIME_DIR / 'notion-snapshot.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None
        for identifier in pending:
            remaining = deadline - time.monotonic()
            limits = CrawlLimits(max_requests=60, max_seconds=max(1.0, remaining))
            if remaining <= 0 or not refresh_page(index, identifier, allowed, deadline, limits=limits):
                with _inline_refresh_failures_lock:
                    _inline_refresh_failures[identifier] = time.monotonic()
                return None
    return len(pending)


def search_complete_snapshot(database_ids, *, terms, read_scope, constraints,
                             pre_body_constraints, body_clauses, keyword_clauses, limit, include_blocks=True):
    """전수 대조된 저장본만 사용하고, 불확실하면 기존 API 검색에 맡긴다.

    보류·변경 페이지가 몇 건뿐이면 그 페이지만 다시 읽고 한 번 더 검색한다."""
    import sqlite3
    import requests
    from fastapi import HTTPException
    try:
        index = open_body_index()
        if index is None or not _scope_current(read_scope):
            return None
        # 목록이 만료·누락되면 본문 재수집으로 복구할 수 없다. 원문 전체를
        # 두 번 검사하기 전에 작은 목록 테이블만 읽고 실시간 검색에 맡긴다.
        with index._database() as db:
            placeholders, params = index._scope(database_ids)
            if not placeholders:
                return None
            catalogs = list(db.execute(
                f'SELECT complete, updated_at FROM catalogs '
                f'WHERE fingerprint=? AND database_id IN ({placeholders})', params))
            now = time.time()
            max_age = catalog_max_age_seconds()
            if len(catalogs) != len(params) - 1 or any(
                not row['complete'] or not 0 <= now - _time(row['updated_at']) < max_age
                for row in catalogs
            ):
                return None
        if constraints.get('metadata_list'):
            from backend.notion.recent_catalog import search_recent_catalog
            return search_recent_catalog(index, database_ids, read_scope=read_scope,
                                         constraints=constraints, limit=limit)
        blocks_enabled = os.getenv('NOTION_FETCH_PAGE_BLOCKS', 'true').lower() not in {'0', 'false', 'no'}
        if constraints.get('exact_title') and not body_clauses and not keyword_clauses:
            from backend.notion.catalog_search import search_catalog_snapshot
            catalog_result = search_catalog_snapshot(index, database_ids, read_scope=read_scope,
                constraints=constraints, terms=terms, limit=limit, include_blocks=include_blocks and blocks_enabled)
            if catalog_result is not None:
                return catalog_result
        if not blocks_enabled:
            return None
        refreshed = 0
        for attempt in range(2):
            outcome = _search_snapshot_once(
                index, database_ids, terms=terms, read_scope=read_scope, constraints=constraints,
                pre_body_constraints=pre_body_constraints, body_clauses=body_clauses,
                keyword_clauses=keyword_clauses, limit=limit)
            if outcome is not _STALE:
                if outcome is not None and refreshed:
                    outcome['meta']['inline_refreshed_page_count'] = refreshed
                return outcome
            healed = None if attempt else _refresh_pending_inline(index, database_ids, list(read_scope))
            if not healed:
                return None
            refreshed = healed
        return None
    except (BodyIndexError, sqlite3.Error, OSError, ValueError, HTTPException, requests.RequestException):
        return None


def _search_snapshot_once(index, database_ids, *, terms, read_scope, constraints,
                          pre_body_constraints, body_clauses, keyword_clauses, limit):
    """완전한 저장본으로 한 번 검색한다. 보류·변경 페이지가 있으면 _STALE이다."""
    from backend.notion.body_index_search import _indexed_body_matches
    from backend.notion.matching import notion_page_sort_key
    positive = [str(clause['value']) for clause in body_clauses if clause['operator'] == 'contains']
    discovery_terms = list(dict.fromkeys([*positive, *terms]))
    from itertools import batched
    created = (constraints.get('sort') or {}).get('field') == 'created_at' or any(
        c.get('field') == 'created_at' for c in constraints.get('common_filter_clauses', []))
    def rank(p):
        return (bool(terms) and p.get('index_title_match', False),
                notion_page_sort_key(p, created_at_filtered=created))
    shown: list[dict[str, Any]] = []
    selected_records = {}
    total = 0
    with index._database() as db:
        _, diagnostics, _, proof = _state(index, db, database_ids, allowed_database_ids=list(read_scope))
        if not proof['complete']:
            return _STALE
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
        return _STALE
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
