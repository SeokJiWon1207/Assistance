"""최근 등록 목록을 본문 색인의 완전성과 분리해 속성만으로 조회한다."""
from datetime import datetime, timezone
import json
import logging
import time

from backend.execution import CURRENT_BUDGET, checkpoint
from backend.notion.complete_snapshot import _time, catalog_max_age_seconds
from backend.notion.property_snapshot import read_full_properties
from backend.notion.property_roles import REGISTRATION_NAMES, role_properties


LOGGER = logging.getLogger(__name__)


def _recent_candidates(index, database_ids, read_scope, constraints, limit):
    from backend.notion import search

    with index._database() as db:
        placeholders, params = index._scope(database_ids)
        if not placeholders:
            return None
        catalogs = {row['database_id']: dict(row) for row in db.execute(
            f'SELECT * FROM catalogs WHERE fingerprint=? AND database_id IN ({placeholders})', params)}
        now = time.time()
        max_age = catalog_max_age_seconds()
        if len(catalogs) != len(params) - 1 or any(
            not row['complete'] or not 0 <= now - _time(row['updated_at']) < max_age
            for row in catalogs.values()
        ):
            return None
        exclusions = {row['page_id']: row['observed_at'] for row in db.execute(
            'SELECT page_id, observed_at FROM page_exclusions WHERE fingerprint=?', [index.fingerprint])}
        shown = []
        matched = scanned = excluded = 0
        snapshot_as_of = min(row['updated_at'] for row in catalogs.values())
        labels = {}
        # 대량 본문·블록을 읽거나 전체 문서 객체를 보관하지 않는다.
        rows = db.execute(f'SELECT page_id, database_id, raw_page, updated_at FROM pages '
                          f'WHERE fingerprint=? AND database_id IN ({placeholders})', params)
        for row in rows:
            checkpoint()
            scanned += 1
            database_id = row['database_id']
            if _time(exclusions.get(row['page_id'], '')) >= _time(catalogs[database_id]['updated_at']):
                excluded += 1
                continue
            raw = json.loads(row['raw_page'])
            if raw.get('archived') or raw.get('in_trash'):
                excluded += 1
                continue
            if raw.get('parent', {}).get('type') != 'database_id':
                if not 0 <= now - _time(row['updated_at']) < max_age:
                    return None
                snapshot_as_of = min(snapshot_as_of, row['updated_at'])
            label = ''
            if constraints.get('database_label'):
                if database_id not in labels:
                    labels[database_id] = _database_label(database_id, read_scope)
                label = labels[database_id]
            # 정렬·기간 필터에는 등록일만 필요하다. 긴 속성/제목과 관계형
            # 항목을 모든 문서에서 표시 문자열로 변환하지 않는다.
            properties = raw.get('properties', {})
            date_properties = {name: properties[name] for name in role_properties(properties, REGISTRATION_NAMES, {'date'})}
            page = search.normalize_notion_page({**raw, 'properties': date_properties}, include_blocks=False,
                                               database_id=database_id, database_label=label)
            if not search.notion_page_matches_constraints(page, constraints):
                continue
            matched += 1
            shown.append(page)
            shown.sort(key=lambda item: search.notion_page_sort_key(item, created_at_filtered=True), reverse=True)
            del shown[limit:]
        return shown, {
            'snapshot_as_of': snapshot_as_of, 'scanned_count': scanned,
            'candidate_count': matched, 'excluded_document_count': excluded,
        }


def _database_label(database_id, read_scope):
    from backend.notion import search

    label = search.fetch_notion_database_title(database_id)
    root = read_scope.get(database_id)
    if root and root != database_id:
        label = f'{search.fetch_notion_database_title(root)} / {label}'
    return label


def search_recent_catalog(index, database_ids, *, read_scope, constraints, limit):
    """검증된 목록 시점의 상위 N건을 고르고 현재 접근·정렬 속성을 재확인한다."""
    from backend.notion import search
    from backend.notion.body_index_sync import _Budget, _ReadFailure, _page_ancestors, _request, _valid_page

    outcome = _recent_candidates(index, database_ids, read_scope, constraints, max(1, limit))
    if outcome is None:
        return None
    candidates, proof = outcome
    active = CURRENT_BUDGET.get()
    # 화면의 20건 설정도 검증할 수 있도록 반환 후보 수에 맞춘다.
    # 고정 20초는 정상 API 지연과 공유 요청 간격만으로도 전수 검색으로 넘어갔다.
    verification_seconds = min(60, max(20, len(candidates) * 2))
    deadline = time.monotonic() + verification_seconds
    if active:
        deadline = min(deadline, active.deadline - 5)
    budget = _Budget(deadline, max(20, len(candidates) * 8))
    allowed = {identifier.replace('-', '').lower() for identifier in read_scope}
    pages = []
    labels = {}
    try:
        for candidate in candidates:
            checkpoint()
            identifier = candidate['id'].replace('-', '').lower()
            database_id = candidate['database_id']
            raw = _request(f'pages/{identifier}', budget)
            parents = _page_ancestors(raw, budget)
            if not _valid_page(raw, identifier, database_id, allowed, parents):
                return None
            raw = read_full_properties(raw, budget)
            if database_id not in labels:
                labels[database_id] = _database_label(database_id, read_scope)
            page = search.normalize_notion_page(raw, include_blocks=False,
                                               database_id=database_id, database_label=labels[database_id])
            if (not search.notion_page_matches_constraints(page, constraints)
                    or search.notion_page_sort_key(page, created_at_filtered=True)
                    != search.notion_page_sort_key(candidate, created_at_filtered=True)):
                # 등록일이 바뀌면 기존 상위 N건을 그대로 확정하지 않는다.
                return None
            page.update(match_method='notion_recent_catalog', matched_terms=[], match_score=0)
            pages.append(page)
    except _ReadFailure as exc:
        LOGGER.warning('Notion 최근 목록 재검증 중단: reason=%s candidates=%d verified=%d',
                       str(exc), len(candidates), len(pages))
        return None
    return {'source': 'notion', 'status': 'ok', 'notion_pages': pages, 'meta': {
        **proof, 'status': 'snapshot', 'tool': 'notion_recent_catalog',
        'coverage_basis': 'api_properties', 'search_complete': True,
        'candidate_count_complete': True, 'result_window_complete': True,
        'displayed_count': len(pages), 'content_complete': None,
        'incomplete_reasons': [], 'errors': [], 'constraints': constraints, 'terms': [],
        'body_index_used': False, 'database_ids': database_ids, 'scoped_database_ids': list(read_scope),
        'queriedAt': datetime.now(timezone.utc).isoformat(),
    }}
