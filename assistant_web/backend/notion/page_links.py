"""페이지에서 관측한 링크만 탐색한다. 링크 자체는 권한·조건 통과의 증거가 아니다."""
import re
from urllib.parse import unquote, urlsplit

from backend.execution import checkpoint
from backend.notion import crawl_transport
from backend.notion.crawl_transport import CrawlBudget, NotionReadFailure
from backend.notion.crawl_validation import _page_ancestors, _valid_page
from backend.notion.mapping import normalize_notion_page
from backend.notion.page_enrichment import notion_page_matches_and_enrich
from backend.notion.page_ownership import _id, page_belongs_to_database


def page_links(obj: dict) -> list[dict]:
    found: dict[tuple[str, str], dict] = {}

    def add(identifier, kind):
        identifier = _id(identifier)
        if re.fullmatch(r'[0-9a-f]{32}', identifier):
            found.setdefault((identifier, kind), {'page_id': identifier, 'kind': kind})

    def url_link(value):
        if not isinstance(value, str):
            return
        try:
            url = urlsplit(value)
            host = url.hostname or ''
        except ValueError:
            return
        if url.scheme not in {'https', 'http'} or not any(host == d or host.endswith('.' + d) for d in ('notion.so', 'notion.site', 'notion.com')):
            return
        # fragment는 블록 ID다. 페이지는 경로 끝의 UUID에서만 찾는다.
        tail = unquote(url.path).rstrip('/').rsplit('/', 1)[-1]
        matched = re.search(r'([0-9a-fA-F]{32}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$', tail)
        if matched:
            add(matched[1], 'link')

    if obj.get('type') == 'child_page':
        add(obj.get('id'), 'child')
    if obj.get('type') == 'link_to_page':
        add((obj.get('link_to_page') or {}).get('page_id'), 'link')
    for prop in (obj.get('properties') or {}).values():
        if prop.get('type') == 'relation':
            for item in prop.get('relation') or []:
                add(item.get('id'), 'relation')

    def rich(value):
        if isinstance(value, list):
            for item in value:
                rich(item)
        elif isinstance(value, dict):
            mention = value.get('mention') or {}
            if mention.get('type') == 'page':
                add((mention.get('page') or {}).get('id'), 'mention')
            for key in ('href', 'url'):
                url_link(value.get(key))
            for key, item in value.items():
                if key not in {'parent', 'mention', 'url', 'href'}:
                    rich(item)
    rich(obj.get(str(obj.get('type')), {}))
    rich(obj.get('properties', {}))
    return list(found.values())


def page_record(identifier: str, scope: dict, budget: CrawlBudget) -> dict | None:
    raw = crawl_transport.request_json(f'pages/{identifier}', budget)
    parents = _page_ancestors(raw, budget)
    database = next((db for db in scope if page_belongs_to_database(raw, db, parents)), '')
    if not database or not _valid_page(raw, identifier, database, set(scope), parents):
        return None
    return {'page_id': _id(identifier), 'database_id': database, 'raw_page': raw}


def normalize_scoped_page(raw: dict, database: str, scope: dict, filters: dict) -> dict:
    """DB 조건이 있으면 검증된 소속의 실제 표시 이름으로 대조한다."""
    label = ''
    if filters.get('database_label') or any(c.get('field') == 'database' for c in filters.get('common_filter_clauses', [])):
        from backend.notion.read_api import fetch_notion_database_title
        label = fetch_notion_database_title(database)
        root = scope.get(database)
        if root and root != database:
            label = f'{fetch_notion_database_title(root)} / {label}'
    return normalize_notion_page(raw, include_blocks=False, database_id=database, database_label=label)


def follow_page_links(source: dict, scope: dict, constraints: dict, *, deadline: float, limit=5,
                      seen: set[str] | None = None) -> tuple[list[dict], dict]:
    from backend.notion.filter_conditions import merge_common_notion_constraints
    seen = {_id(value) for value in (seen or set())} | {_id(source['id'])}
    links = list({item['page_id']: item for item in source.get('_verified_page_links') or []}.values())
    result = []
    checked = 0
    unavailable = 0
    reasons: set[str] = set()
    budget = CrawlBudget(deadline, 60)
    filters = merge_common_notion_constraints({}, constraints)
    for link in links[:20]:
        checkpoint()
        identifier = _id(link['page_id'])
        if identifier in seen:
            continue
        seen.add(identifier)
        checked += 1
        try:
            record = page_record(identifier, scope, budget)
        except NotionReadFailure as exc:
            unavailable += 1
            reasons.add(str(exc))
            continue
        if not record:
            unavailable += 1
            reasons.add('unverified_scope')
            continue
        page = normalize_scoped_page(record['raw_page'], record['database_id'], scope, filters)
        if not notion_page_matches_and_enrich(page, filters):
            continue
        page.update(content='', content_incomplete=True, match_method='notion_page_navigation', _candidate_only=True,
                    navigation={'from_page_id': source['id'], 'kind': link['kind']})
        result.append(page)
        if len(result) >= limit:
            break
    return result, {'coverage': 'page_links', 'search_complete': False, 'candidate_count_complete': False,
                    'candidate_count': len(links), 'checked_count': checked,
                    'unavailable_link_count': unavailable, 'incomplete_reasons': sorted(reasons)}
