"""의미 검색·페이지 탐색 후보를 현재 API의 독립된 문서 근거로 확인한다."""
import os
import time
from typing import Any

from backend.execution import CURRENT_BUDGET, checkpoint
from backend.notion.body_index import open_body_index
from backend.notion.body_index_sync import CrawlLimits, validate_index_candidate
from backend.notion.discovery import resolve_notion_settings
from backend.notion.page_links import normalize_scoped_page, page_record, page_links
from backend.notion.page_enrichment import notion_page_matches_and_enrich
from backend.notion.page_ownership import segment_belongs_to_page
from backend.notion.read_scope import resolve_notion_read_scope
from backend.notion.semantic_index import semantic_candidates
from backend.notion.retrieval_candidates import hybrid_candidates
from backend.notion.crawl_transport import CrawlBudget, NotionReadFailure
from backend.notion.property_snapshot import read_full_properties
from backend.tools.redaction import redact_text


class CurrentPageUnavailable(RuntimeError):
    """현재 접근을 일시적으로 재확인하지 못했다. 부재·권한 철회의 증거가 아니다."""

    def __init__(self, reason: str, *, retryable: bool | None = None):
        super().__init__(reason)
        self.retryable = reason not in {'time_limit', 'request_limit'} if retryable is None else retryable


def read_candidate_page(record: dict, scope: dict, constraints: dict, budget: CrawlBudget) -> dict | None:
    """검색은 현재 메타데이터만 확인한다. 저장된 본문은 후보 선택 힌트로만 분리한다."""
    from backend.notion.filter_conditions import merge_common_notion_constraints
    current = page_record(record['page_id'], scope, budget)
    if current is None:
        return None
    raw = read_full_properties(current['raw_page'], budget)
    filters = merge_common_notion_constraints({}, constraints)
    page = normalize_scoped_page(raw, current['database_id'], scope, filters)
    if not notion_page_matches_and_enrich(page, filters):
        return None
    page.update(content='', content_incomplete=True, match_method='notion_hybrid_candidate',
        _candidate_only=True,
        _candidate_text=redact_text(str(record.get('text') or '')),
        _candidate_body_guide=bool(record.get('body_guide_hint')),
        _candidate_terms=record.get('matched_terms') or [], _verified_page_links=page_links(raw))
    return page


def read_current_page(record: dict, scope: dict, constraints: dict, *, deadline: float) -> dict | None:
    from backend.notion.filter_conditions import merge_common_notion_constraints
    if os.getenv('NOTION_FETCH_PAGE_BLOCKS', 'true').lower() in {'false', '0', 'no', 'off'}:
        return None
    try:
        current = validate_index_candidate(record, list(scope), index=open_body_index(), deadline=deadline,
            limits=CrawlLimits(max_blocks=400, max_depth=12, max_requests=60, max_seconds=90), raise_transient=True)
    except NotionReadFailure as exc:
        raise CurrentPageUnavailable(str(exc), retryable=exc.retryable) from exc
    if current is None:
        return None
    identifier = str(current['raw_page']['id'])
    segments = [s for s in current['segments'] if s.get('text') and segment_belongs_to_page(s, identifier)]
    filters = merge_common_notion_constraints({}, constraints)
    page = normalize_scoped_page(current['raw_page'], current['database_id'], scope, filters)
    page['content'] = '\n'.join(s['text'] for s in segments)
    # 자식은 별도 탐색 대상으로 남는다. 그 문서의 본문을 부모의 완료 판정에 섞지 않는다.
    reasons = set(current.get('incomplete_reasons') or [])
    incomplete = reasons - {'unsupported_child_pages'}
    complete = current.get('accessible_content_complete') is True or (bool(reasons) and not incomplete)
    page.update(content_incomplete=not complete, content_incomplete_reasons=sorted(reasons) if not complete else [],
                match_method='notion_semantic_body',
                _verified_body=True, _owned_segments=segments, _verified_page_links=current.get('page_links') or [])
    if not notion_page_matches_and_enrich(page, filters):
        return None
    # 벡터 저장본의 문장을 그대로 반환하지 않고 현재 본문에 실재하는 구간만 쓴다.
    excerpt = str(record.get('text') or '').partition('\n')[2].strip()
    if excerpt and excerpt in redact_text(page['content']):
        page['semantic_excerpt'] = excerpt
    elif record.get('matched_terms'):
        body = redact_text(page['content'])
        offsets = [body.lower().find(term.lower()) for term in record['matched_terms'] if term]
        positions = [pos for pos in offsets if pos >= 0]
        if positions:
            start = max(0, min(positions) - 120)
            page['semantic_excerpt'] = body[start:start + 1200]
    return page


def search_notion_body(query: str, limit: int, constraints: dict, *, read_cache: dict | None = None,
                      title_only: bool = False) -> dict:
    meta: dict[str, Any] = {'tool': 'notion_semantic_body', 'coverage': 'top_k', 'search_complete': False,
            'candidate_count_complete': False, 'checked_count': 0, 'incomplete_reasons': ['semantic_top_k']}
    result: dict[str, Any] = {'source': 'notion', 'status': 'ok', 'notion_pages': [], 'meta': meta}
    if os.getenv('NOTION_FETCH_PAGE_BLOCKS', 'true').lower() in {'false', '0', 'no', 'off'}:
        meta['incomplete_reasons'].append('body_disabled')
        return result
    settings = resolve_notion_settings()
    scope, complete = resolve_notion_read_scope(settings['database_ids'], settings['token'])
    if not complete:
        meta['incomplete_reasons'].append('scope_incomplete')
        return result
    candidates = [] if title_only else semantic_candidates(query, list(scope), limit=60, constraints=constraints)
    if candidates is None:
        meta['incomplete_reasons'].append('semantic_index_unavailable')
    candidates = hybrid_candidates(query, list(scope), semantic=candidates, constraints=constraints)
    if title_only:
        candidates = [r for r in candidates if r.get('title_hit')]
        meta.update(tool='notion_catalog_candidates', incomplete_reasons=['title_candidates_only'])
    meta['candidate_count'] = len(candidates)
    active = CURRENT_BUDGET.get()
    deadline = min(time.monotonic() + 90, active.deadline - 2 if active else float('inf'))
    budget = CrawlBudget(deadline, 60)
    pages = []
    for candidate in candidates:
        checkpoint()
        if time.monotonic() >= deadline or meta['checked_count'] >= 10:
            meta['incomplete_reasons'].append('semantic_validation_limit')
            break
        if candidate['score'] < .25 and not candidate.get('lexical_hits'):
            continue
        meta['checked_count'] += 1
        identifier = candidate['page_id'].replace('-', '')
        page = (read_cache or {}).get(identifier)
        if page is not None:
            from backend.notion.filter_conditions import merge_common_notion_constraints
            if not notion_page_matches_and_enrich(page, merge_common_notion_constraints({}, constraints)):
                continue
            meta['reused_count'] = meta.get('reused_count', 0) + 1
        else:
            try:
                page = read_candidate_page(candidate, scope, constraints, budget)
            except (CurrentPageUnavailable, NotionReadFailure) as exc:
                meta['incomplete_reasons'].append(str(exc))
                if str(exc) in {'http_429', 'http_529', 'time_limit'}:
                    break
                continue
            if page is not None and read_cache is not None:
                read_cache[identifier] = page
        if page is not None:
            page['semantic_score'] = round(candidate['score'], 5)
            pages.append(page)
        if len(pages) >= limit:
            break
    result['notion_pages'] = pages
    return result
