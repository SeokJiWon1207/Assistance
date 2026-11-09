"""저장본의 목록·속성·본문 완전성과 유효 기간을 읽는다."""
from collections import Counter
from datetime import datetime, timezone
import json
import os
import time
from typing import Any
from backend.notion.body_index import BodyIndex, CONTENT_POLICY_VERSION

API_EXCLUSIONS = frozenset({'unsupported_block', 'foreign_synced_block',
                            'synced_source_outside_scope', 'http_403', 'http_404'})


def catalog_max_age_seconds() -> int:
    """전체 목록이 이보다 오래되면 새 문서·속성 변경을 놓칠 수 있어 전수 검색으로 쓰지 않는다."""
    try:
        return max(600, min(int(os.getenv("NOTION_SNAPSHOT_CATALOG_MAX_AGE_SECONDS", "7200")), 86400))
    except ValueError:
        return 7200


def _time(value: str) -> float:
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.timestamp() if result.tzinfo else 0
    except (ValueError, TypeError, AttributeError):
        return 0


def catalog_freshness(index: BodyIndex, database_ids: list[str]) -> dict[str, Any]:
    """조회 범위의 목록 행만 읽는다. DB 하나라도 만료되면 전체 목록이 만료된다."""
    placeholders, params = index._scope(database_ids)
    with index._database() as db:
        rows = db.execute(
            f'SELECT complete, updated_at FROM catalogs WHERE fingerprint=? AND database_id IN ({placeholders})',
            params,
        ).fetchall() if placeholders else []
    now = time.time()
    max_age = catalog_max_age_seconds()
    timestamps = [_time(row['updated_at']) for row in rows]
    oldest = min(timestamps, default=0)
    database_count = len(params) - 1
    complete = (bool(rows) and len(rows) == database_count and all(row['complete'] for row in rows)
                and all(0 < stamp <= now for stamp in timestamps))
    status = 'incomplete'
    if complete:
        status = 'fresh' if now - oldest < max_age else 'stale'
    return {
        'status': status, 'databaseCount': database_count, 'maxAgeSeconds': max_age,
        'oldestUpdatedAt': datetime.fromtimestamp(oldest, timezone.utc).isoformat() if oldest > 0 else None,
        'ageSeconds': int(now - oldest) if 0 < oldest <= now else None,
    }


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
    catalog_max_age = catalog_max_age_seconds()
    catalog_complete = len(catalogs) == len(params) - 1 and all(
        r['complete'] and 0 <= now - _time(r['updated_at']) < catalog_max_age for r in catalogs.values())
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
