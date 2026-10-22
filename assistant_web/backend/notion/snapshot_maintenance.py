"""전체 목록과 달라진 본문만 갱신하여 검색 가능한 저장본을 유지한다."""
import time

from backend.notion.body_index_sync import (
    CrawlLimits, _Budget, _ReadFailure, _request, _page_ancestors, _valid_page, _wait_for_retry_after,
    validate_index_candidate, sync_notion_body_index,
)
from backend.notion.complete_snapshot import coverage, _state
from backend.notion.body_index import page_belongs_to_database


def refresh_page(index, identifier, database_ids, deadline, *, limits=None):
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
                                      limits=limits or CrawlLimits.configured(), catalog_page=json.loads(record["raw_page"]))
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
            try:
                _wait_for_retry_after(_Budget(deadline, 0))
            except _ReadFailure:
                # 대기할 예산이 없으면 미시도 페이지를 오류로 오염시키지 않는다.
                return {**coverage(index, database_ids), 'attempted_page_count': attempted}
            seen.add(identifier)
            refresh_page(index, identifier, database_ids, deadline)
            attempted += 1
    return {**coverage(index, database_ids), 'attempted_page_count': attempted}


def _refresh_catalog_properties(index, database_ids, deadline, refresh_age):
    """DB 목록에 열거되지 않는 자식 속성과 잘린 제목만 보완한다. 본문은 읽지 않는다."""
    import json
    from backend.notion.property_snapshot import pending_properties, read_full_properties
    from backend.notion.complete_snapshot import _time
    allowed = {identifier.replace('-', '').lower() for identifier in database_ids}
    now = time.time()
    with index._database() as db:
        placeholders, params = index._scope(database_ids)
        rows = []
        for row in db.execute(
            f'SELECT page_id, database_id, raw_page, updated_at FROM pages '
            f'WHERE fingerprint=? AND database_id IN ({placeholders})', params):
            raw = json.loads(row['raw_page'])
            child = raw.get('parent', {}).get('type') != 'database_id'
            title_pending = any(prop.get('type') == 'title' for _name, prop in pending_properties(raw))
            if title_pending or (child and not 0 <= now - _time(row['updated_at']) < refresh_age):
                rows.append({**dict(row), 'raw': raw, 'child': child})
    budget = _Budget(deadline, max(1000, len(rows) * 8))
    parents = {}
    refreshed = failures = 0
    for row in rows:
        raw = row['raw']
        if time.monotonic() >= deadline:
            return refreshed, failures + 1
        try:
            if row['child']:
                raw = _request(f"pages/{row['page_id']}", budget)
                if raw.get('archived') or raw.get('in_trash'):
                    index.exclude_page(row['page_id'], 'archived')
                    continue
                parents = _page_ancestors(raw, budget)
                memberships = [identifier for identifier in allowed if page_belongs_to_database(raw, identifier, parents)]
                if not memberships:
                    known_outside = any((obj.get('parent') or {}).get('type') in {'database_id', 'workspace'}
                                        for obj in [raw, *parents.values()])
                    if known_outside:
                        index.exclude_page(row['page_id'], 'outside_scope')
                        continue
                    failures += 1
                    continue
                row['database_id'] = memberships[0]
                if not _valid_page(raw, row['page_id'], row['database_id'], allowed, parents):
                    failures += 1
                    continue
            raw = read_full_properties(raw, budget)
            index.upsert_page(row['database_id'], raw, parent_pages=parents)
            refreshed += 1
        except _ReadFailure as exc:
            if str(exc) in {'http_403', 'http_404'}:
                index.exclude_page(row['page_id'], str(exc))
            else:
                failures += 1
    return refreshed, failures


def refresh_catalog_cache(*, max_seconds=900):
    """앱의 사전 로딩에서 제목·속성 목록을 유지한다. 운영 타이머와 같은 잠금을 쓴다."""
    import fcntl
    import shutil
    from backend.config import RUNTIME_DIR, get_notion_settings
    from backend.notion.body_index import BodyIndex, body_index_path, open_body_index
    from backend.notion.complete_snapshot import _time, catalog_max_age_seconds
    from backend.notion.read_scope import ensure_notion_read_scope
    settings = get_notion_settings()
    if not settings.get('token') or not settings.get('database_ids'):
        return {'status': 'skipped'}
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with (RUNTIME_DIR / 'notion-snapshot.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'already_running'}
        mantis = RUNTIME_DIR / 'mantis_complete.sqlite3'
        size = mantis.stat().st_size if mantis.exists() else 0
        if shutil.disk_usage(RUNTIME_DIR).free < max(256 * 1024**2, size + size // 4 + 160 * 1024**2):
            return {'status': 'insufficient_disk_space'}
        scope, complete = ensure_notion_read_scope(settings['database_ids'], settings['token'])
        if not complete:
            return {'status': 'incomplete'}
        index = open_body_index()
        if index is None:
            if body_index_path().exists():
                return {'status': 'index_unavailable'}
            index = BodyIndex(body_index_path())
        database_ids = list(scope)
        refresh_age = min(3600, catalog_max_age_seconds() / 2)
        now = time.time()
        with index._database() as db:
            placeholders, params = index._scope(database_ids)
            catalogs = list(db.execute(
                f'SELECT * FROM catalogs WHERE fingerprint=? AND database_id IN ({placeholders})', params))
        refresh = len(catalogs) != len(params) - 1 or any(
            not row['complete'] or not 0 <= now - _time(row['updated_at']) < refresh_age for row in catalogs)
        deadline = time.monotonic() + max_seconds
        if refresh:
            sync_notion_body_index(database_ids, max_pages=0, max_seconds=max_seconds, index=index)
        refreshed, failures = _refresh_catalog_properties(index, database_ids, deadline, refresh_age)
        from backend.notion.catalog_search import _catalog_candidates
        if _catalog_candidates(index, database_ids, '') is None:
            failures += 1
        return {'status': 'incomplete' if failures else 'refreshed' if refresh or refreshed else 'fresh',
                'refreshed_property_count': refreshed, 'failed_property_count': failures}
