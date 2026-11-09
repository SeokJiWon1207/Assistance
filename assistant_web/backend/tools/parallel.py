"""서버가 독립성을 확인한 인접 읽기 호출만 최대 2개 실행한다."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import perf_counter
from typing import Callable

from pydantic import ValidationError

from backend.execution import submit_with_context, wait_for_result
from backend.tools.call_state import begin_call
from backend.tools.query_conditions import QueryArguments
from backend.tools.registry import ToolContext, ToolRegistry, _signature


_QUERIES = {'count_mantis_issues', 'list_mantis_issues', 'aggregate_mantis_issues'}
_DETAILS = {'get_mantis_issue_detail', 'get_mantis_issue_details'}
_NOTION = {'search_notion_pages', 'search_notion_body', 'get_notion_page_content', 'list_notion_page_links',
           'count_notion_pages', 'list_notion_pages'}


@dataclass(frozen=True)
class Invocation:
    call_id: str
    name: str
    arguments: dict | None
    parse_error: str = ''


@dataclass
class Executed:
    result: dict
    elapsed_ms: int
    group: int
    group_size: int


def _footprint(registry: ToolRegistry, call: Invocation, context: ToolContext) -> tuple[str, set[int], str] | None:
    """권한·참조 의존성을 보수적으로 판정한다. 실제 인자/근거 검증은 실행기에서 다시 수행한다."""
    spec = registry.specs.get(call.name)
    if (call.name not in _QUERIES | _DETAILS | _NOTION or spec is None or not spec.available(context)
            or call.arguments is None or call.parse_error):
        return None
    try:
        parsed = spec.arguments.model_validate(call.arguments)
    except ValidationError:
        return None
    contract = (context.query_scopes.get(parsed.query_scope_id or '') if isinstance(parsed, QueryArguments) else None
                ) or context.query_contracts.get(spec.source)
    if isinstance(parsed, QueryArguments) and parsed.condition_ref:
        contract = context.condition_refs.get(parsed.condition_ref)
        if contract is None or not contract.accepted:
            return None
    if contract is None or contract.population_limit or getattr(parsed, 'population_limit', None):
        return None
    # 다른 호출에서 갱신할 수 있는 근거·집계의 필드를 읽는 조사 단계는 순차 실행한다.
    if any(item.evidence_ref for item in getattr(parsed, 'filters', None) or []):
        return None
    ids: set[int] = set()
    family = 'notion' if call.name in _NOTION else 'mantis'
    if call.name in _DETAILS:
        ids = set(getattr(parsed, 'issue_ids', []) or [getattr(parsed, 'issue_id', 0)])
        known = context.confirmed_issue_ids | context.ledger.referenced_issue_ids() | {
            int(row['id']) for row in context.ledger.issue_rows()}
        if not ids or not ids.issubset(known):
            return None
    if family == 'notion':
        # 순번·가이드 복구는 최신 이슈 원문과 공유 상태를 읽으므로 따로 실행한다.
        if context.related_ordinal is not None or context.investigative_notion:
            return None
        page_ref = getattr(parsed, 'page_ref', None)
        if page_ref is not None and context.ledger.page_id_for_ref(page_ref) is None:
            return None
    return family, ids, _signature(call.name, parsed.model_dump())


def independent(registry: ToolRegistry, first: Invocation, second: Invocation, context: ToolContext) -> bool:
    left, right = _footprint(registry, first, context), _footprint(registry, second, context)
    if left is None or right is None or first.call_id == second.call_id:
        return False
    if left[2] == right[2] or left[1] & right[1]:
        return False
    # 두 Notion 작업의 N 참조 번호/본문 무효화/가이드 재탐색은 아직 공유 자원이다.
    return not (left[0] == right[0] == 'notion')


def execute_calls(registry: ToolRegistry, calls: list[Invocation], context: ToolContext, *, concurrency: int,
                  on_start: Callable[[Invocation], None] | None = None,
                  on_result: Callable[[Invocation, Executed], None] | None = None) -> list[Executed]:
    results: list[Executed] = []
    index = group = 0
    while index < len(calls):
        group += 1
        wave = calls[index:index + 1]
        if concurrency >= 2 and index + 1 < len(calls) and independent(registry, calls[index], calls[index + 1], context):
            wave.append(calls[index + 1])
        if on_start:
            for call in wave:
                on_start(call)
        if len(wave) == 1:
            call = wave[0]; started = perf_counter()
            result = registry.execute(call.name, call.arguments, call.parse_error, context.for_call(call.call_id))
            results.append(Executed(result, round((perf_counter() - started) * 1000), group, 1))
        else:
            pending = [begin_call(context.for_call(call.call_id)) for call in wave]

            def execute(pair):
                call, work = pair
                started = perf_counter()
                result = registry.run_pending(call.name, call.arguments, call.parse_error, work)
                return result, round((perf_counter() - started) * 1000)

            # 문맥(전송 정책·취소·마감·요청 저장본·관측기)을 복사하고 요청이 작업자 수명을 소유한다.
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='agent-read') as pool:
                futures = [submit_with_context(pool, execute, pair) for pair in zip(wave, pending, strict=True)]
                completed = [wait_for_result(future) for future in futures]
            published = registry.publish_pending([(call.name, work, result) for call, work, (result, _) in zip(wave, pending, completed, strict=True)])
            for result, (_, elapsed) in zip(published, completed, strict=True):
                results.append(Executed(result, elapsed, group, 2))
        if on_result:
            for call, execution in zip(wave, results[-len(wave):], strict=True):
                on_result(call, execution)
        index += len(wave)
    return results
