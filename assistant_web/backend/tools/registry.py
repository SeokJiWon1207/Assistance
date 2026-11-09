"""에이전트가 호출하는 읽기 전용 도구의 등록·인자 검증·실행 계약.

권한·확정 조건·사용 원천은 모델 인자가 아니라 서버가 만든 ToolContext로만 정한다.
인자는 pydantic(직접 의존성)으로 검증하며 정의되지 않은 인자는 거부한다. 모든 실패는
구조화된 오류로 돌려주어 호출 ID와 결과의 대응을 깨지 않는다.
"""

import json
import re
import sqlite3
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Type

import requests
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from backend.execution import CURRENT_BUDGET, StepTimeout, checkpoint, run_with_deadline
from backend.tools.call_state import CallOwner, PendingCall, ToolStateConflict, begin_call, publish_group, restore, snapshot
from backend.tools.conditions import SourceConditions
from backend.tools.ledger import EvidenceLedger
from backend.tools.redaction import redact
from backend.tools.query_conditions import QueryArguments, QueryContract
from backend.tools.step_conditions import bind_step, read_target_matches, step_view


class ToolBudgetExhausted(RuntimeError):
    """요청 공통 도구 실행 한도에 도달했다."""


@dataclass
class ToolContext:
    question: str
    retrieval_text: str
    enabled_sources: Dict[str, bool]
    conditions: Dict[str, SourceConditions]
    confirmed_issue_ids: frozenset[int]
    ledger: EvidenceLedger
    charge_tool: Callable[[], None]
    step_seconds: Callable[[float], float]
    tool_timeout_seconds: float
    max_result_chars: int
    signatures: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    call_id: str = ""
    related_ordinal: Optional[int] = None
    mantis_query_terms: tuple[str, ...] = ()
    structured_conditions: bool = False
    query_contracts: Dict[str, QueryContract] = field(default_factory=dict)
    query_scopes: Dict[str, QueryContract] = field(default_factory=dict)
    condition_refs: Dict[str, QueryContract] = field(default_factory=dict)
    notion_read_cache: Dict[str, dict] = field(default_factory=dict)
    investigative_notion: bool = False
    report_chat: Optional[Callable[..., dict]] = None
    local_state: Dict[str, Any] = field(default_factory=dict)
    _call_owner: CallOwner = field(default_factory=CallOwner, repr=False)
    _root: Optional['ToolContext'] = field(default=None, repr=False)

    def for_call(self, call_id: str) -> 'ToolContext':
        """공용 문맥의 호출 ID를 덮어쓰지 않는다. 작업 사본은 실행 진입 때 만든다."""
        return replace(self, call_id=call_id, _root=self._root or self)

    def ordinal_issue(self) -> Optional[int]:
        listed = next((r for r in reversed(self.ledger.query_results()) if r["operation"] == "list"), None)
        ids = listed["ordered_ids"] if listed else []
        position = self.related_ordinal
        if position == -1 and ids:
            return ids[-1]
        if position is not None and 1 <= position <= len(ids):
            return ids[position - 1]
        return None

    def has_related_guide(self) -> bool:
        target = self.ordinal_issue()
        return target is not None and any(
            page["issue_id"] == target and page.get("guide") and page.get("excerpt")
            for page in self.ledger.related_pages.values())


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    arguments: Type[BaseModel]
    source: str
    run: Callable[[Any, ToolContext], Dict[str, Any]]
    needs: tuple[str, ...] = ()
    prepare_conditions: Optional[Callable[[Any, ToolContext], SourceConditions]] = None

    def available(self, context: ToolContext) -> bool:
        return all(context.enabled_sources.get(name) for name in (self.source, *self.needs))

    def definition(self) -> Dict[str, Any]:
        schema = self.arguments.model_json_schema()
        schema.pop("title", None)
        for value in (schema.get("properties") or {}).values():
            value.pop("title", None)
        return {"type": "function", "function": {"name": self.name, "description": self.description,
                                                 "parameters": schema}}


def error_result(tool: str, code: str, message: str, *, retryable: bool = False) -> Dict[str, Any]:
    return {"ok": False, "tool": tool, "error": {"code": code, "message": message, "retryable": retryable}}


def _signature(name: str, arguments: Dict[str, Any]) -> str:
    normalized = {key: (re.sub(r"\s+", " ", value).strip().casefold() if isinstance(value, str) else value)
                  for key, value in sorted(arguments.items()) if key != "limit" or name == "list_mantis_issues"}
    return name + ":" + json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def fit_result(result: Dict[str, Any], limit: int) -> str:
    """결과별 크기 한도. 넘치면 뒤쪽 항목부터 빼고 잘림을 표시한다."""
    result = redact(result)
    text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(text) > limit and result.get('condition_step'):
        # 원본 단계 증명은 서버 원장에 보존한다. 모델에는 참조·용도·적용 조건을 남기고 중복 증명만 줄인다.
        result.get('argument_validation', {}).pop('origins', None)
        step = result['condition_step']
        for key in ('origins', 'parent_ref', 'sort', 'condition_ref'):
            step.pop(key, None)
        if result.get('applied_filters') is not None:
            step.pop('filters', None)
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    items = result.get("results")
    while len(text) > limit and isinstance(items, list) and len(items) > 1:
        items.pop()
        result["truncated"] = True
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    while len(text) > limit and isinstance(items, list) and items:
        fields = [(item, key) for item in items if isinstance(item, dict) for key in ('content', 'snippet')
                  if isinstance(item.get(key), str) and len(item[key]) > 80]
        if not fields:
            break
        item, key = max(fields, key=lambda pair: len(pair[0][pair[1]]))
        item[key] = item[key][:max(80, len(item[key]) // 2)]
        if key == 'content' and type(item.get('content_offset')) is int and 'next_offset' in item:
            # 최종 전송 한도에서 다시 줄었어도 다음 읽기가 빠진 구간을 건너뛰지 않는다.
            item['next_offset'] = item['content_offset'] + len(item[key])
        result['truncated'] = True
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if len(text) > limit:
        result = {key: value for key, value in result.items() if key != "results"}
        result.update(truncated=True, results=[], note="결과가 한도를 넘어 본문을 생략했습니다. 범위를 좁혀 다시 조회하세요.")
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return text


class ToolRegistry:
    def __init__(self, specs: List[ToolSpec]):
        self.specs = {spec.name: spec for spec in specs}

    def definitions(self, context: ToolContext) -> List[Dict[str, Any]]:
        definitions = []
        for spec in self.specs.values():
            if not spec.available(context):
                continue
            definition = spec.definition()
            properties = definition['function']['parameters'].get('properties', {})
            if 'query_scope_id' in properties:
                scopes = [key for key, contract in context.query_scopes.items() if contract.conditions.source == spec.source]
                if scopes:
                    properties['query_scope_id']['anyOf'] = [{'type': 'string', 'enum': scopes}, {'type': 'null'}]
                else:
                    # 비교가 없는 단건·일반 조회에 모델이 S1을 만들어 붙이지 않도록 한다.
                    properties.pop('query_scope_id')
            definitions.append(definition)
        return definitions

    def execute(self, name: str, arguments: Optional[Dict[str, Any]], parse_error: str,
                context: ToolContext) -> Dict[str, Any]:
        pending = begin_call(context)
        result = self._execute(name, arguments, parse_error, pending.context)
        checkpoint()
        try:
            pending.publish()
        except ToolStateConflict:
            return error_result(name, 'state_conflict', '조회 중 확정 상태가 바뀌어 결과를 반영하지 않았습니다. 최신 결과를 보고 다시 조회하세요.',
                                retryable=True)
        return result

    def run_pending(self, name: str, arguments: Optional[Dict[str, Any]], parse_error: str,
                    pending: PendingCall) -> Dict[str, Any]:
        """공통 검증·예산 계약으로 실행하되 결과 게시는 요청 조정자에게 맡긴다."""
        result = self._execute(name, arguments, parse_error, pending.context)
        checkpoint()
        return result

    def publish_pending(self, calls: list[tuple[str, PendingCall, dict]]) -> list[dict]:
        """검증된 병렬 결과의 게시와 충돌 오류를 순차 실행과 같은 관측 경계로 노출한다."""
        accepted = publish_group([pending for _, pending, _ in calls])
        return [result if ok else error_result(name, 'state_conflict',
                '동시 조회의 변경이 충돌해 이 결과를 반영하지 않았습니다. 최신 결과를 보고 다시 조회하세요.', retryable=True)
                for (name, _, result), ok in zip(calls, accepted, strict=True)]

    def _execute(self, name: str, arguments: Optional[Dict[str, Any]], parse_error: str,
                 context: ToolContext) -> Dict[str, Any]:
        spec = self.specs.get(name)
        if spec is None:
            return error_result(name, "unknown_tool", "등록되지 않은 도구입니다.")
        if not spec.available(context):
            return error_result(name, "source_disabled", "사용자가 끈 원천의 도구라 실행하지 않았습니다.")
        if parse_error or arguments is None:
            return error_result(name, parse_error or "argument_parse_error", "도구 인자를 JSON 객체로 해석하지 못했습니다.")
        try:
            parsed = spec.arguments.model_validate(arguments)
        except ValidationError as exc:
            fields = ", ".join(sorted({".".join(map(str, error["loc"])) for error in exc.errors()}))[:200]
            return error_result(name, "invalid_arguments", f"인자 형식이 맞지 않습니다: {fields}")
        invocation = context
        validation = None
        contract = context.query_contracts.get(spec.source)
        if isinstance(parsed, QueryArguments):
            if context.query_scopes:
                ref_contract = context.condition_refs.get(parsed.condition_ref or '')
                scope_id = parsed.query_scope_id or (ref_contract.scope_id if ref_contract else None)
                contract = context.query_scopes.get(scope_id or '')
                if (contract is None or contract.conditions.source != spec.source
                        or ref_contract is not None and ref_contract.scope_id != scope_id):
                    return error_result(name, 'condition_mismatch', '비교 범위의 query_scope_id 또는 같은 범위의 condition_ref를 지정하세요.')
                invocation = replace(context, conditions={**context.conditions, spec.source: contract.conditions},
                    query_contracts={**context.query_contracts, spec.source: contract},
                    confirmed_issue_ids=frozenset(contract.conditions.constraints.get('issue_ids') or []))
            elif parsed.query_scope_id is not None:
                return error_result(name, 'condition_mismatch',
                    '현재 질문에는 해당 비교 범위가 없습니다. query_scope_id를 삭제하고 나머지 검증된 조건을 유지해 다시 호출하세요.')
            if parsed.purpose != 'investigate' and any(f.evidence or f.evidence_ref for f in parsed.filters or []):
                return error_result(name, 'invalid_arguments', '추가 조건의 근거는 investigate 단계에서만 지정하세요.')
            supplied = any(value is not None for value in (parsed.filters, parsed.scope, parsed.sort, parsed.condition_ref, parsed.purpose, parsed.population_limit))
            if contract is None and (context.structured_conditions or supplied):
                return error_result(name, 'condition_context_unavailable', '검증된 원천별 조회 조건이 필요합니다.')
            if contract is not None:
                try:
                    contract, conditions, validation = bind_step(invocation, spec.source, parsed,
                        explicit_required=name in {'count_mantis_issues', 'list_mantis_issues', 'aggregate_mantis_issues',
                                                   'count_notion_pages', 'list_notion_pages', 'build_issue_report'},
                        list_limit=getattr(parsed, 'limit', None) if name in {'list_mantis_issues', 'list_notion_pages'} else None)
                    if not read_target_matches(parsed, conditions):
                        raise ValueError('상세 조회 번호가 이번 단계의 이슈 조건과 다릅니다.')
                    if contract.population_limit:
                        if name == 'search_mantis_issues':
                            raise ValueError('최근 N건 모집단은 count/list_mantis_issues로 먼저 확인하세요. 일반 검색으로 모집단을 넓힐 수 없습니다.')
                        targets = getattr(parsed, 'issue_ids', None) or ([parsed.issue_id] if hasattr(parsed, 'issue_id') else [])
                        allowed = {identifier for stage in context.ledger.mantis_query_snapshots.values()
                                   if stage.get('meta', {}).get('population_limit') == contract.population_limit
                                   for identifier in stage['meta'].get('matched_issue_ids', [])}
                        if not set(targets).issubset(allowed):
                            raise ValueError('최근 N건 모집단 안에서 조회한 이슈만 상세를 읽을 수 있습니다.')
                except ValueError as exc:
                    return error_result(name, 'condition_mismatch', str(exc))
                invocation = replace(invocation, conditions={**invocation.conditions, spec.source: conditions},
                    query_contracts={**invocation.query_contracts, spec.source: contract})
        if spec.prepare_conditions is not None and contract is not None:
            try:
                conditions = spec.prepare_conditions(parsed, invocation)
            except ValueError as exc:
                return error_result(name, 'condition_mismatch', str(exc))
            if conditions != invocation.conditions[spec.source]:
                from uuid import uuid4
                matching = next((c for c in context.condition_refs.values() if c.conditions == conditions and c.accepted), None)
                filters = conditions.constraints.get('filter_clauses') or []
                contract = matching or replace(contract, conditions=conditions, filters=filters,
                    origins=[o for o in contract.origins if o['field'] in {f['field'] for f in filters}],
                    ref='Q' + uuid4().hex[:12], parent_ref=contract.ref, purpose='investigate', accepted=True)
                context.condition_refs[contract.ref] = contract
                invocation = replace(invocation, conditions={**invocation.conditions, spec.source: conditions},
                    query_contracts={**invocation.query_contracts, spec.source: contract})
            validation = validation or {'status': 'pass', 'mode': 'page_reference', 'origins': contract.origins}
        step = step_view(contract, invocation.conditions[spec.source]) if validation is not None and contract else None
        signature = _signature(name, parsed.model_dump())
        previous = context.signatures.get(signature)
        if previous is not None and not ((previous.get("error") or {}).get("retryable") and not previous.get("retried")):
            return error_result(name, "duplicate_call", "같은 인자로 이미 실행한 도구입니다. 이전 결과를 사용하세요.")
        try:
            context.charge_tool()
        except ToolBudgetExhausted:
            return error_result(name, "call_limit", "도구 실행 한도에 도달해 실행하지 않았습니다.")
        before_run = snapshot(context)
        try:
            first_stage = len(context.ledger.stages)
            seconds = context.step_seconds(context.tool_timeout_seconds)
            parent = CURRENT_BUDGET.get()
            if name == 'build_issue_report' and parent is not None:
                # 보고서는 조회+사실 선택+초안+독립 검토를 포함한다. 요청의 최종 예약을 뺀
                # 조사 예산 안에서 실행하며 내부 모델 호출마다 더 짧은 단계 마감을 적용한다.
                seconds = context.step_seconds(parent.remaining())
            if name in {"search_notion_pages", "search_notion_body"} and parent is not None:
                # 최종 답변 예약분을 뺀 조사 시간에서 다음 원문 읽기 한 번의 시간을 남긴다.
                investigation = context.step_seconds(parent.remaining())
                seconds = max(seconds, investigation - context.tool_timeout_seconds)
            if name in {"get_notion_page_content", "list_notion_page_links"} and (context.related_ordinal is not None or context.investigative_notion) and parent is not None:
                seconds = context.step_seconds(parent.remaining())
            result = run_with_deadline(seconds, lambda: spec.run(parsed, invocation))
        except StepTimeout:
            restore(context, before_run)
            result = error_result(name, "tool_timeout", "도구 제한 시간을 넘었습니다.", retryable=True)
        except (HTTPException, requests.RequestException, OSError, sqlite3.Error):
            restore(context, before_run)
            # 외부 오류 문구에는 접속 정보나 원문이 섞일 수 있어 코드만 돌려준다.
            result = error_result(name, "tool_error", "원천 조회에 실패했습니다.", retryable=True)
        error_code = (result.get('error') or {}).get('code')
        if name in {'get_notion_page_content', 'list_notion_page_links'} and error_code in {'tool_timeout', 'tool_error'}:
            identifier = context.ledger.page_id_for_ref(getattr(parsed, 'page_ref', ''))
            if identifier:
                context.ledger.invalidate_notion_body(identifier, 'time_limit' if error_code == 'tool_timeout' else 'request_failed')
                context.notion_read_cache.pop(identifier.replace('-', ''), None)
            parent = CURRENT_BUDGET.get()
            remaining = context.step_seconds(parent.remaining() if parent else context.tool_timeout_seconds)
            if error_code == 'tool_timeout' and remaining <= 5:
                result = error_result(name, 'retrieval_budget_exhausted',
                    '남은 조회 시간이 없어 본문을 끝까지 읽지 못했습니다. 확인한 범위와 미완료 문서를 구분하세요.')
                result['error']['reason'] = 'time_limit'
        if validation is not None and contract is not None:
            result.update(condition_ref=contract.ref, argument_validation=validation)
        if step is not None:
            result['condition_step'] = step
            context.ledger.record_step(step, first_stage, result, name, context.call_id)
        # 출처 확인 실패는 원장이 늘면 달라지므로 중복 판정에 쓰지 않는다.
        if (result.get("error") or {}).get("code") != "not_in_scope":
            context.signatures[signature] = {**result, "retried": previous is not None}
        return result
