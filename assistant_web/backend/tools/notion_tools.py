"""Notion 문서 검색·본문 조회 도구. 기존 읽기 범위와 승인된 클라이언트만 사용한다."""

import json
import re
import time
from typing import Any, Dict, Literal
from copy import deepcopy
from dataclasses import asdict
from backend.tools.ledger import proves_empty

from pydantic import BaseModel, ConfigDict, Field

from backend.notion import read_api, related_guides
from backend.notion.constants import NOTION_RESULT_BODY_MAX_BLOCKS
from backend.notion.matching import notion_page_matches_common_filters
from backend.notion.page_ownership import segment_belongs_to_page
from backend.notion.page_links import follow_page_links
from backend.notion.semantic_search import CurrentPageUnavailable, read_current_page, search_notion_body as execute_notion_body_search
from backend.notion.search import build_notion_terms, execute_notion_search
from backend.tools.redaction import redact_text
from backend.tools.registry import ToolContext, ToolSpec, error_result
from backend.tools.query_conditions import QueryArguments
from backend.tools.conditions import SourceConditions
from backend.utils.text import compact_text, normalize_for_match


class SearchNotionArgs(QueryArguments):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=200,
                       description="Notion 문서를 찾을 핵심어. 기술명·기능·장애명·문서 주제 위주")
    limit: int = Field(default=5, ge=1, le=5, description="받을 문서 수")
    reference_scope: Literal['confirmed', 'common'] = Field(default='confirmed',
        description='confirmed는 확정 조건. common은 선택된 이슈 기능의 공통 가이드를 고객사 조건 없이 참고 등급으로 찾는다. 이슈 목록 조건은 유지된다.')


class NotionPageArgs(BaseModel):
    model_config = ConfigDict(extra="forbid")
    page_ref: str = Field(pattern=r"^N\d{1,3}$", description="현재 Notion 검색·페이지 탐색 결과의 ref(예: N1)")


class NotionContentArgs(NotionPageArgs):
    offset: int | None = Field(default=None, ge=0, strict=True,
        description='본문의 이어 읽기 시작 위치. 생략하면 관련 구간, 0이면 처음, 계속 읽을 때는 반환된 next_offset을 사용한다.')


class NotionLinksArgs(NotionPageArgs):
    limit: int = Field(default=5, ge=1, le=5)


def notion_scope() -> dict:
    from backend.notion.discovery import resolve_notion_settings
    from backend.notion.read_scope import resolve_notion_read_scope
    settings = resolve_notion_settings()
    scope, complete = resolve_notion_read_scope(settings['database_ids'], settings['token'])
    return scope if complete else {}


def _deadline() -> float:
    from backend.execution import CURRENT_BUDGET
    active = CURRENT_BUDGET.get()
    return min(time.monotonic() + 90, active.deadline - 1 if active else float('inf'))


def _revoke(context: ToolContext, page_id: str) -> None:
    for item in context.ledger.stages:
        if item.family != 'notion':
            continue
        item.stage['notion_pages'] = [p for p in item.stage.get('notion_pages', []) if str(p.get('id')) != page_id]
        item.stage.setdefault('meta', {}).update(search_complete=False, incomplete_reasons=['page_access_revoked'])
    context.ledger.related_pages.pop(page_id, None)


def _page_view(context: ToolContext, page: Dict[str, Any]) -> Dict[str, Any]:
    page_id = str(page.get("id") or "")
    return {"ref": context.ledger.notion_refs.get(page_id), "title": page.get("title", "제목 없음"),
            "database": page.get("database_label", "Notion DB"), "status": page.get("status", ""),
            "updated_at": page.get("last_edited_time", ""),
            "evidence_grade": context.ledger.page_grade(page_id),
            "snippet": compact_text(str(page.get('semantic_excerpt') or page.get("content") or page.get("properties_text") or ""), 300),
            "has_page_links": bool(page.get('_verified_page_links')),
            "body_checked": bool(page.get('_verified_body')),
            "candidate_only": not bool(page.get('_verified_body')),
            "procedure_candidate": bool(page.get('_candidate_body_guide')) and not bool(page.get('_verified_body')),
            **({'candidate_hint': compact_text(page['_candidate_text'], 450),
                'hint_note': '저장본의 검색 힌트입니다. 현재 본문을 읽기 전에는 사실 근거로 쓰지 마세요.'}
                if page.get('_candidate_text') and not page.get('_verified_body') else {}),
            "body_empty": bool(page.get('_verified_body')) and not str(page.get('content') or '').strip(),
            "content_complete": not page.get("content_incomplete") and bool(page.get("content") or page.get('_verified_body')),
            **({'content_incomplete_reasons': page['content_incomplete_reasons']}
               if page.get('content_incomplete_reasons') else {}),
            **({'applied_conditions': list(page['_read_conditions']['applied']),
                'unapplied_conditions': list(page['_read_conditions']['unapplied'])} if page.get('_read_conditions') else {})}


def _search_conditions(args: SearchNotionArgs, context: ToolContext) -> SourceConditions:
    conditions = context.conditions['notion']
    if args.reference_scope == 'confirmed':
        return conditions
    target = context.ordinal_issue()
    if not context.structured_conditions or target is None or conditions.planned:
        raise ValueError('공통 참고자료는 먼저 조회한 이슈 목록의 선택된 항목에 대해서만 찾을 수 있습니다.')
    issue = context.ledger.issue_row(target) or {}
    terms = related_guides.topic_terms(str(issue.get('summary') or ''), build_notion_terms(str(issue.get('summary') or ''), {}))
    if not any(normalize_for_match(t) in normalize_for_match(args.query) for t in terms):
        raise ValueError('공통 가이드 검색어에 선택한 이슈의 구체적인 기능명을 포함하세요.')
    constraints = deepcopy(conditions.constraints)
    constraints['filter_clauses'] = [c for c in constraints.get('filter_clauses', []) if c['field'] != 'customer']
    for key in ('customer_aliases', 'customer_key', 'customer_exact', 'excluded_customer_aliases'):
        constraints.pop(key, None)
    return SourceConditions('notion', constraints, tuple(x for x in conditions.applied if x != '고객사'),
        tuple(dict.fromkeys((*conditions.unapplied, '고객사'))), False)


def _page_conditions(page: dict, context: ToolContext) -> SourceConditions:
    saved = page.get('_read_conditions')
    return SourceConditions(**saved) if saved else context.conditions['notion']


def _read_step_conditions(args: NotionPageArgs, context: ToolContext) -> SourceConditions:
    identifier = context.ledger.page_id_for_ref(args.page_ref)
    page = context.ledger.notion_page(identifier) if identifier else None
    return _page_conditions(page, context) if page else context.conditions['notion']


def _temporarily_unavailable(context: ToolContext, identifier: str, tool: str, error: CurrentPageUnavailable) -> dict:
    # ref는 재시도에 남기되 현재 확인하지 못한 이전 본문은 근거에서 내린다.
    context.ledger.invalidate_notion_body(identifier, str(error))
    context.notion_read_cache.pop(identifier.replace('-', ''), None)
    result = error_result(tool,
        'source_temporarily_unavailable' if error.retryable else 'retrieval_budget_exhausted',
        ('일시 오류로 현재 본문을 확인하지 못했습니다. 남은 예산 안에서 같은 ref로 재시도할 수 있습니다.'
         if error.retryable else '남은 조회 예산으로 본문 읽기 또는 요청 제한 대기를 끝내지 못했습니다. '
         '이번 요청에서 즉시 재시도하지 말고 확인한 범위와 미완료 문서를 구분하세요.'), retryable=error.retryable)
    result['error']['reason'] = str(error)
    return result


def search_notion_body(args: SearchNotionArgs, context: ToolContext) -> Dict[str, Any]:
    try:
        conditions = _search_conditions(args, context)
    except ValueError as exc:
        return error_result('search_notion_body', 'not_in_scope', str(exc))
    target = context.ordinal_issue()
    if context.related_ordinal is not None and target is None:
        return error_result('search_notion_body', 'not_in_scope', '먼저 이슈 목록에서 요청한 순번을 확인하세요.')
    issue = context.ledger.issue_row(target) if target is not None else None
    terms = build_notion_terms(str((issue or {}).get('summary') or args.query), {})[:10]
    stage = execute_notion_body_search(args.query, args.limit, conditions.constraints, read_cache=context.notion_read_cache)
    feature_terms = related_guides.topic_terms(str((issue or {}).get('summary') or args.query), terms)
    for page in stage.get('notion_pages') or []:
        page['_read_conditions'] = asdict(conditions)
        if issue and page.get('_candidate_body_guide'):
            hint = normalize_for_match(str(page.get('_candidate_text') or ''))
            page['_candidate_body_guide'] = any(normalize_for_match(t) in hint for t in feature_terms)
    context.ledger.add('notion', stage, grade=conditions.grade, unapplied=conditions.unapplied, call_id=context.call_id)
    pages = stage.get('notion_pages') or []
    if target is not None:
        for page in pages:
            context.ledger.related_pages.setdefault(str(page['id']), {'issue_id': target, 'terms': terms})
    views = [_page_view(context, page) for page in pages]
    unread = [p['ref'] for p in views if p['procedure_candidate']]
    return {'ok': True, 'tool': 'search_notion_body', 'untrusted_content': True, 'evidence_grade': conditions.grade,
            'applied_conditions': list(conditions.applied), 'unapplied_conditions': list(conditions.unapplied),
            'results': views, 'search_complete': False, 'unread_procedure_refs': unread,
            'retrieval': stage.get('meta') or {}, 'note': '현재 접근·조건을 확인한 본문 검색 후보입니다. 선택한 문서를 get_notion_page_content로 읽으세요. 빈 결과는 전체 부재가 아닙니다.'}


def list_notion_page_links(args: NotionLinksArgs, context: ToolContext) -> Dict[str, Any]:
    identifier = context.ledger.page_id_for_ref(args.page_ref)
    page = context.ledger.notion_page(identifier) if identifier else None
    if not identifier or page is None:
        return error_result('list_notion_page_links', 'not_in_scope', '현재 요청에서 확인한 페이지의 ref만 탐색할 수 있습니다.')
    depth = page.get('_navigation_depth', 0)
    if depth >= 3:
        return error_result('list_notion_page_links', 'navigation_limit', '페이지 연결 탐색 깊이 한도에 도달했습니다.')
    conditions = _page_conditions(page, context)
    scope = notion_scope()
    # 후보의 현재 속성에서 이미 확인한 링크는 본문 전체를 먼저 수집하지 않고 따라간다.
    # 대상의 접근·소속·조건은 follow_page_links가 별도로 검증한다.
    if not page.get('_verified_body') and not page.get('_verified_page_links'):
        try:
            current = read_current_page({'page_id': identifier, 'database_id': page.get('database_id')},
                                        scope, conditions.constraints, deadline=_deadline())
        except CurrentPageUnavailable as exc:
            return _temporarily_unavailable(context, identifier, 'list_notion_page_links', exc)
        if current is None:
            _revoke(context, identifier)
            return error_result('list_notion_page_links', 'not_in_scope', '페이지의 현재 접근·소속·조건을 확인하지 못했습니다.')
        page.update(current)
        page.pop('_body_read_failure', None)
    pages, meta = follow_page_links(page, scope, conditions.constraints, deadline=_deadline(), limit=args.limit,
                                    seen=set(context.ledger.notion_refs))
    linked = context.ledger.related_pages.get(identifier)
    for child in pages:
        child['_navigation_depth'] = depth + 1
        child['_read_conditions'] = asdict(conditions)
        if linked:
            context.ledger.related_pages.setdefault(str(child['id']), {'issue_id': linked['issue_id'], 'terms': linked['terms']})
    context.ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': pages, 'meta': meta},
                       grade=conditions.grade, unapplied=conditions.unapplied, call_id=context.call_id)
    return {'ok': True, 'tool': 'list_notion_page_links', 'untrusted_content': True, 'evidence_grade': conditions.grade,
            'results': [{**_page_view(context, p), 'link': p.get('navigation')} for p in pages],
            'search_complete': False, 'retrieval': meta}


def search_notion_pages(args: SearchNotionArgs, context: ToolContext) -> Dict[str, Any]:
    try:
        conditions = _search_conditions(args, context)
    except ValueError as exc:
        return error_result('search_notion_pages', 'not_in_scope', str(exc))
    terms = build_notion_terms(args.query, {})[:6] or [args.query]
    target = context.ordinal_issue()
    if context.related_ordinal is not None and target is None:
        return error_result('search_notion_pages', 'not_in_scope', '먼저 이슈 목록에서 요청한 순번을 확인하세요.')
    exact = any(c['field'] == 'title' and c['operator'] == 'equals' for c in conditions.constraints.get('filter_clauses', []))
    if context.structured_conditions and context.investigative_notion and not exact:
        stage = execute_notion_body_search(args.query, args.limit, conditions.constraints,
            read_cache=context.notion_read_cache, title_only=True)
    elif context.related_ordinal is not None:
        if target is None:
            return error_result("search_notion_pages", "not_in_scope",
                                "먼저 이슈 목록을 조회하고 요청한 순번이 실제로 있는지 확인하세요.")
        issue = context.ledger.issue_row(target) or {}
        subject = normalize_for_match(" ".join(str(issue.get(k) or "") for k in ("summary", "preview")))
        keywords = re.findall(r"[\w-]+", args.query)
        terms = [term for term in keywords if len(term) >= 2 and normalize_for_match(term) in subject
                 and term.lower() not in {"가이드", "문서", "관련", "mts", "hts", "이슈", "문의", "요청", "확인", "현상"}]
        if not terms:
            return error_result("search_notion_pages", "ungrounded_topic", "선택한 이슈에 나온 구체적인 기능명으로 검색하세요.")
        stage = execute_notion_search(" ".join(terms), args.limit, conditions.constraints, planned_terms=terms[:6],
                                      include_blocks=False, derive_text_constraints=False, prefer_catalog_titles=True)
    elif conditions.planned:
        # 계획한 원천은 기존 통합 경로와 같은 질문 문장·확정 조건을 쓰고 검색어만 바꾼다.
        stage = execute_notion_search(context.retrieval_text, args.limit, conditions.constraints,
                                      planned_terms=terms, include_blocks=False,
                                      derive_text_constraints=not context.structured_conditions)
    else:
        stage = execute_notion_search(args.query, args.limit, conditions.constraints, planned_terms=terms,
                                      include_blocks=False, derive_text_constraints=False)
        # 교차 원천에서 직접 근거로 쓸 문서는 반환된 속성도 대응 조건과 대조한다.
        pages = [p for p in stage.get("notion_pages") or []
                 if notion_page_matches_common_filters(p, conditions.constraints["filter_clauses"])]
        stage = {**stage, "notion_pages": pages, "meta": {**(stage.get("meta") or {}), "displayed_count": len(pages)}}
    for page in stage.get('notion_pages') or []:
        page['_read_conditions'] = asdict(conditions)
    context.ledger.add("notion", stage, grade=conditions.grade, unapplied=conditions.unapplied,
                       call_id=context.call_id)
    pages = list(stage.get("notion_pages") or [])
    if target is not None:
        for page in pages:
            context.ledger.related_pages.setdefault(str(page["id"]), {"issue_id": target, "terms": terms})
    failed = stage.get("status") in {"error", "partial_error", "skipped"}
    return {"ok": True, "tool": "search_notion_pages", "untrusted_content": True, "evidence_grade": conditions.grade,
            "applied_conditions": list(conditions.applied), "unapplied_conditions": list(conditions.unapplied),
            "results": [_page_view(context, page) for page in pages],
            "search_complete": (stage.get("meta") or {}).get("search_complete") is True and not failed,
            "source_error": failed,
            "note": ("이번 검색 조건 안에서 전체 조회를 마쳤고 일치 항목이 0건입니다."
                     if conditions.grade == "scoped" and proves_empty(stage) else
                     "조회한 범위에서 해당 사실을 확정할 수 없습니다." if not pages or failed else "")}


def _related_guide(context: ToolContext, page: dict, linked: dict, text: str, ref: str) -> dict | None:
    ledger = context.ledger
    page_id = str(page["id"])
    target = context.ordinal_issue()
    if target is None or target != linked["issue_id"]:
        return None
    issue = ledger.issue_row(target) or {}
    terms = related_guides.topic_terms(str(issue.get("summary") or ""), linked["terms"])
    excerpt, is_guide = related_guides.guide_passage(str(page.get("title") or ""), text, terms)
    complete = not page.get("content_incomplete")
    linked.update(title=page.get("title") or "제목 없음", ref=ref, body_checked=True,
                  content_complete=complete, excerpt=redact_text(excerpt), guide=complete and is_guide)
    if context.structured_conditions:
        # 새 경로에서는 모델이 의미 검색·페이지 탐색의 다음 단계를 선택한다.
        return None
    if context.has_related_guide() or target in ledger.guide_retries:
        return None
    conditions = context.conditions["notion"]
    excluded = {p for p, item in ledger.related_pages.items() if item.get("body_checked")}
    known = {str(p["id"]): str(p.get("database_id") or "") for p in ledger.notion_pages()
             if (ledger.related_pages.get(str(p["id"])) or {}).get("issue_id") == target}
    ledger.guide_retries[target] = {"sourcePageId": page_id, "outcome": "started"}
    guide, passage, report = related_guides.find_alternate_guide(terms, conditions.constraints, excluded, known)
    ledger.guide_retries[target].update(report)
    if guide is None:
        return None
    identifier = str(guide["id"])
    previous = ledger.notion_page(identifier)
    if previous is not None:
        previous.update(guide)
    ledger.add("notion", {"source": "notion", "status": "ok", "notion_pages": [guide],
                          "meta": {"tool": "notion_related_guide_search", "search_complete": False}},
               grade=conditions.grade, unapplied=conditions.unapplied, call_id=context.call_id)
    passage = redact_text(passage)
    ledger.related_pages[identifier] = {"issue_id": target, "terms": terms, "title": guide["title"],
                                        "ref": ledger.notion_refs[identifier], "body_checked": True,
                                        "content_complete": True, "excerpt": passage, "guide": True}
    return {"ref": ledger.notion_refs[identifier], "title": guide["title"], "excerpt": passage,
            "evidence_grade": conditions.grade, "content_complete": True}


def get_notion_page_content(args: NotionContentArgs, context: ToolContext) -> Dict[str, Any]:
    page_id = context.ledger.page_id_for_ref(args.page_ref)
    page = context.ledger.notion_page(page_id) if page_id else None
    if not page_id or page is None:
        # 본문 조회 함수는 읽기 범위를 다시 확인하지 않으므로 이번 검색 결과의 문서만 연다.
        return error_result("get_notion_page_content", "not_in_scope", "이번 요청의 Notion 검색 결과에 없는 문서입니다.")
    linked = context.ledger.related_pages.get(page_id)
    if context.structured_conditions:
        if not page.get('_verified_body'):
            try:
                current = read_current_page({'page_id': page_id, 'database_id': page.get('database_id'),
                    'text': page.get('_candidate_text'), 'matched_terms': page.get('_candidate_terms')},
                    notion_scope(), _page_conditions(page, context).constraints, deadline=_deadline())
            except CurrentPageUnavailable as exc:
                return _temporarily_unavailable(context, page_id, 'get_notion_page_content', exc)
            if current is None:
                _revoke(context, page_id)
                return error_result('get_notion_page_content', 'not_in_scope', '문서의 현재 접근·소속·조건을 확인하지 못했습니다.')
            page.update(current)
            page.pop('_body_read_failure', None)
            context.notion_read_cache[page_id.replace('-', '')] = page
        text = redact_text(str(page.get('content') or ''))
        if linked:
            _related_guide(context, page, linked, text, args.page_ref)
        limit = max(200, context.max_result_chars - 800)
        excerpt = redact_text(str(page.get('semantic_excerpt') or ''))
        offset = args.offset if args.offset is not None else max(0, text.find(excerpt) - min(200, limit // 4)) if excerpt else 0
        if offset > len(text):
            return error_result('get_notion_page_content', 'invalid_offset', '현재 본문 길이를 벗어난 위치입니다.')
        content = text[offset:offset + limit]
        result: Dict[str, Any] = {'ok': True, 'tool': 'get_notion_page_content', 'untrusted_content': True,
                'evidence_grade': context.ledger.page_grade(page_id),
                'results': [{**_page_view(context, page), 'content': content, 'content_offset': offset,
                             'next_offset': offset + len(content) if offset + len(content) < len(text) else None}],
                **({'related_guide_verified': bool(linked.get('guide')),
                    'guide_note': '' if linked.get('guide') else '이 본문에서 대상 기능의 사용 가이드는 확인되지 않았습니다.'} if linked else {}),
                'truncated': offset > 0 or offset + len(content) < len(text)}
        # JSON 이스케이프까지 포함해 맞춘다. 긴 코드 블록 때문에 본문 전체가 탈락하지 않게 한다.
        while len(encoded := json.dumps(result, ensure_ascii=False, separators=(',', ':'))) > context.max_result_chars and len(content) > 100:
            content = content[:max(100, len(content) - (len(encoded) - context.max_result_chars) - 20)]
            result['results'][0]['content'] = content
            result['results'][0]['next_offset'] = offset + len(content)
            result['truncated'] = True
        return result
    if args.offset is None and linked and linked.get("guide") and linked.get("content_complete") and linked.get("excerpt"):
        return {"ok": True, "tool": "get_notion_page_content", "untrusted_content": True,
                "evidence_grade": context.ledger.page_grade(page_id), "reused": True,
                "results": [{**_page_view(context, page), "snippet": linked["excerpt"], "content": linked["excerpt"],
                             "content_complete": True}], "truncated": True}
    reused = bool(linked is None and page.get("content") and not page.get("content_incomplete")
                  and page.get("match_method") in {"notion_body_index", "notion_body_scan"})
    if linked:
        fetched = read_api.fetch_notion_page_content(page_id, max_blocks=NOTION_RESULT_BODY_MAX_BLOCKS)
        text = "\n".join(s["text"] for s in fetched["segments"] if segment_belongs_to_page(s, page_id))
        if text:
            page["content"] = text
        page["content_incomplete"] = read_api.NOTION_CONTENT_INCOMPLETE in fetched["text"] or not text
    elif not reused:
        text = read_api.fetch_notion_page_blocks(page_id, max_blocks=NOTION_RESULT_BODY_MAX_BLOCKS)
        if text:
            page["content"] = "\n\n".join(part for part in (page.get("properties_text", ""), text) if part).strip()
        page["content_incomplete"] = read_api.NOTION_CONTENT_INCOMPLETE in text or not text
    else:
        text = str(page.get("content") or "")
    alternate = _related_guide(context, page, linked, text, args.page_ref) if linked else None
    limit = max(500, context.max_result_chars - 800)
    content = redact_text(text if linked else str(page.get("content") or ""))
    offset = args.offset or 0
    if offset > len(content):
        return error_result('get_notion_page_content', 'invalid_offset', '현재 본문 길이를 벗어난 위치입니다.')
    return {"ok": True, "tool": "get_notion_page_content", "untrusted_content": True,
            "evidence_grade": context.ledger.page_grade(page_id), "reused": reused,
            "results": [{**_page_view(context, page), "content": content[offset:offset + limit], 'content_offset': offset,
                         'next_offset': offset + limit if offset + limit < len(content) else None}],
            **({"related_guide": alternate} if alternate else {}),
            "truncated": offset > 0 or offset + limit < len(content)}


SPECS = [
    ToolSpec('search_notion_body',
             'Notion 본문을 의미로 검색한다. 제목에 없는 설명·사용 절차·가이드 질문에 우선 사용한다. '
             '현재 권한·속성을 확인한 상위 후보이며 본문은 get_notion_page_content로 읽는다. 전체 건수나 부재를 증명하지 않는다.',
             SearchNotionArgs, 'notion', search_notion_body, prepare_conditions=_search_conditions),
    ToolSpec('list_notion_page_links',
             '현재 요청에서 찾은 문서의 하위 페이지·링크·DB 관계를 탐색한다. '
             '반환된 문서는 별도 ref이며 get_notion_page_content로 본문을 읽는다.',
             NotionLinksArgs, 'notion', list_notion_page_links, prepare_conditions=_read_step_conditions),
    ToolSpec("search_notion_pages",
             "사내 Notion 문서(공지·회의록·기술 문서·업무 DB)를 검색한다. 읽기 범위와 확정 조건은 서버가 적용한다.",
             SearchNotionArgs, "notion", search_notion_pages, prepare_conditions=_search_conditions),
    ToolSpec("get_notion_page_content",
             "현재 Notion 검색·페이지 탐색 결과 문서 한 건의 본문을 읽는다.",
             NotionContentArgs, "notion", get_notion_page_content, prepare_conditions=_read_step_conditions),
]
