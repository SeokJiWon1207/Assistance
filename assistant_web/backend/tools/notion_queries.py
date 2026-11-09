"""Notion 전체 건수·목록·현재 속성. 의미 검색 후보 수와 분리한다."""
from copy import deepcopy
from dataclasses import asdict
import json
from typing import Literal

from pydantic import Field

from backend.notion import search
from backend.notion.crawl_transport import CrawlBudget, NotionReadFailure
from backend.notion.semantic_search import read_candidate_page
from backend.tools.notion_tools import NotionPageArgs, _deadline, _page_conditions, _read_step_conditions, _revoke, notion_scope
from backend.tools.query_conditions import QueryArguments
from backend.tools.registry import ToolContext, ToolSpec, error_result


class ListPagesArgs(QueryArguments):
    limit: int = Field(default=5, ge=1, le=200)


class PropertiesArgs(NotionPageArgs):
    fields: list[Literal['title', 'status', 'owner', 'due_date', 'deployment_date',
                         'created_time', 'last_edited_time', 'database_label', 'properties_text']] = Field(
        default=['status', 'owner', 'due_date', 'deployment_date'], min_length=1, max_length=9)


def complete_count(stage: dict) -> bool:
    meta = stage.get('meta') or {}
    return bool(stage.get('status') == 'ok' and meta.get('search_complete') is True
                and meta.get('candidate_count_complete') is not False
                and meta.get('coverage') not in {'top_k', 'snapshot_matches', 'saved_filter'}
                and not meta.get('errors') and not meta.get('incomplete_reasons')
                and type(meta.get('candidate_count')) is int and meta['candidate_count'] >= 0)


def query_pages(args: QueryArguments, context: ToolContext, operation: str) -> dict:
    tool = 'count_notion_pages' if operation == 'count' else 'list_notion_pages'
    conditions = context.conditions['notion']
    contract = context.query_contracts.get('notion')
    if not contract or conditions.grade != 'scoped':
        return error_result(tool, 'condition_mismatch', '모든 Notion 조건을 적용한 계약이 필요합니다. 참고 검색 후보를 전체 건수로 셀 수 없습니다.')
    constraints = {**deepcopy(conditions.constraints), 'sort': deepcopy(contract.sort)}
    key = json.dumps([constraints, contract.terms, contract.list_limit], sort_keys=True, ensure_ascii=False)
    cached = context.ledger.notion_query_snapshots.get(key)
    if cached is None:
        cached = search.execute_notion_search(' '.join(contract.terms), contract.list_limit, constraints,
            planned_terms=contract.terms, include_blocks=False, derive_text_constraints=False, require_complete=True)
        if complete_count(cached):
            context.ledger.notion_query_snapshots[key] = deepcopy(cached)
    stage = deepcopy(cached)
    meta = stage.setdefault('meta', {})
    complete = complete_count(stage)
    meta['search_complete'] = complete
    observed = meta.get('candidate_count', len(stage.get('notion_pages') or []))
    result = {'operation': operation, 'total_count': observed if complete else None, 'observed_count': observed,
              'search_complete': complete, 'result_window_complete': meta.get('result_window_complete') is True,
              'ordered_page_ids': [p['id'] for p in stage.get('notion_pages') or []],
              'applied_filters': deepcopy(contract.filters), 'sort': deepcopy(contract.sort),
              'terms': list(contract.terms), 'snapshot_as_of': meta.get('snapshot_as_of')}
    meta['notion_query_result'] = deepcopy(result)
    for page in stage.get('notion_pages') or []:
        page['_read_conditions'] = asdict(conditions)
    context.ledger.add('notion', stage, grade=conditions.grade, unapplied=conditions.unapplied, call_id=context.call_id)
    model_result = {key: value for key, value in result.items() if key != 'ordered_page_ids'}
    return {'ok': True, 'tool': tool, 'untrusted_content': True, 'evidence_grade': conditions.grade, **model_result,
            'results': [{'ref': context.ledger.notion_refs[p['id']], 'title': p.get('title'),
                         'status': p.get('status'), 'owner': p.get('owner')} for p in stage.get('notion_pages') or []],
            'note': '전체 건수는 total_count가 있을 때만 확정합니다. 본문은 별도 읽기 도구로 확인하세요.'}


def get_properties(args: PropertiesArgs, context: ToolContext) -> dict:
    tool = 'get_notion_page_properties'
    identifier = context.ledger.page_id_for_ref(args.page_ref)
    page = context.ledger.notion_page(identifier) if identifier else None
    if not identifier or not page:
        return error_result(tool, 'not_in_scope', '이번 요청에서 확인한 페이지 ref만 조회할 수 있습니다.')
    conditions = _page_conditions(page, context)
    scope = notion_scope()
    if not scope:
        return error_result(tool, 'source_temporarily_unavailable', '현재 읽기 범위를 확인하지 못했습니다.', retryable=True)
    try:
        current = read_candidate_page({'page_id': identifier}, scope, conditions.constraints, CrawlBudget(_deadline(), 60))
    except NotionReadFailure as exc:
        return error_result(tool, 'source_temporarily_unavailable', '현재 속성을 끝까지 확인하지 못했습니다.', retryable=exc.retryable)
    if current is None:
        _revoke(context, identifier)
        return error_result(tool, 'not_in_scope', '현재 페이지 접근·소속·조건을 확인하지 못했습니다.')
    current.update(_verified_properties=True, _read_conditions=asdict(conditions))
    # 출처/후속 질문도 첫 검색 당시 속성 대신 현재 확인한 값을 사용한다.
    if page.get('_verified_body') and page.get('last_edited_time') != current.get('last_edited_time'):
        context.ledger.invalidate_notion_body(identifier, 'page_changed')
        context.notion_read_cache.pop(identifier.replace('-', ''), None)
    body_keys = {'content', 'content_incomplete', '_candidate_only', '_candidate_text', '_candidate_body_guide', '_candidate_terms'}
    for item in context.ledger.stages:
        for existing in item.stage.get('notion_pages') or []:
            if existing.get('id') == identifier:
                existing.update({key: value for key, value in current.items() if key not in body_keys})
    values = {name: current.get(name) or '' for name in dict.fromkeys(args.fields)}
    result = {'page_ref': args.page_ref, 'page_id': identifier, 'title': current.get('title'), 'values': values,
              'properties_complete': True}
    context.ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': [current],
        'meta': {'property_result': result}}, grade=conditions.grade, unapplied=conditions.unapplied, call_id=context.call_id)
    value_limit = max(80, (context.max_result_chars - 2000) // len(values))
    model_values = {name: str(value)[:value_limit] for name, value in values.items()}
    return {'ok': True, 'tool': tool, 'untrusted_content': True, 'evidence_grade': conditions.grade, **result,
            'values': model_values, 'truncated': model_values != values}


SPECS = [
    ToolSpec('count_notion_pages', '확정 조건의 Notion 전체 항목 수. 전체 조회 미완료면 total_count=null이다.',
             QueryArguments, 'notion', lambda args, ctx: query_pages(args, ctx, 'count')),
    ToolSpec('list_notion_pages', '확정 조건의 Notion 목록과 표시 순서. 표시 개수와 전체 건수를 구분한다.',
             ListPagesArgs, 'notion', lambda args, ctx: query_pages(args, ctx, 'list')),
    ToolSpec('get_notion_page_properties', '검색한 페이지의 현재 상태·담당자·기한·배포일 등의 속성을 읽는다. 본문은 별도 도구로 읽는다.',
             PropertiesArgs, 'notion', get_properties, prepare_conditions=_read_step_conditions),
]
