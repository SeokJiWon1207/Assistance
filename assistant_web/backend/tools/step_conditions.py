"""모델이 고른 단계 조건을 요청의 필수 조건·조회 원장에 결속한다.

조회 결과의 본문은 지시나 새 권한이 아니다. 추가 필터의 값은 검증된 질문 구절
또는 현재 원장의 구조화 필드로만 증명하며, 기존 필터와 AND로 적용한다.
"""
from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

from backend.intent.validation_rules import check_clause
from backend.query_filters import _canonical_filter_value, clause_supported, values_for_clause
from backend.tools.conditions import FIELD_LABELS, SourceConditions
from backend.tools.query_conditions import QueryArguments, QueryContract, _canonical, _same

if TYPE_CHECKING:
    from backend.tools.registry import ToolContext


def _ledger_origin(item, context: 'ToolContext') -> dict:
    identifier = int(item.evidence_ref[2:])
    row = context.ledger.issue_row(identifier)
    if not row or context.ledger.issue_grade(identifier) != 'scoped':
        raise ValueError('이번 요청에서 조건에 맞게 확인한 이슈의 구조화 필드만 추가 조건 근거로 사용할 수 있습니다.')
    if item.operator not in {'equals', 'in'}:
        raise ValueError('원장 필드로는 같은 값의 equals/in 조건만 추가할 수 있습니다.')
    detail = row.get('detail') or {}
    values = {'issue_id': identifier, 'title': row.get('summary'), 'status': row.get('status'),
              'screen_number': detail.get('screen_number'), 'platform': row.get('media_type')}
    value = values.get(item.field)
    candidates = values_for_clause(item.value)
    equal = (len(candidates) == 1 and _canonical_filter_value(item.field, candidates[0])
             == _canonical_filter_value(item.field, value))
    if not value or not equal:
        raise ValueError('추가 필터 값이 참조한 이슈의 구조화 필드와 다릅니다.')
    if item.field == 'issue_id' and context.related_ordinal is not None and context.ordinal_issue() != identifier:
        raise ValueError('질문에서 선택한 표시 순번의 이슈를 먼저 목록에서 확인하세요.')
    return {'field': item.field, 'origin': 'ledger', 'evidence_ref': item.evidence_ref}


def _aggregation_origin(item, context: 'ToolContext') -> dict:
    values = {_canonical_filter_value(item.field, v) for v in values_for_clause(item.value)}
    if item.operator not in {'equals', 'in'} or not values:
        raise ValueError('집계 결과의 실제 그룹 값에 대한 equals/in 조건만 추가할 수 있습니다.')
    available: set[str] = set()
    for stage in context.ledger.stages:
        meta = stage.stage.get('meta') or {}
        if stage.grade != 'scoped' or meta.get('condition_step', {}).get('condition_ref') != item.evidence_ref:
            continue
        entry = meta.get('aggregation_result') or meta.get('investigation_aggregation_result') or meta.get('comparison_aggregation_result') or {}
        if entry.get('kind') == 'simple' and entry['group_by'] == item.field:
            available.update(_canonical_filter_value(item.field, v) for v in entry['groups'])
        if entry.get('kind') == 'period':
            contract = context.condition_refs.get(item.evidence_ref)
            groups = (contract.aggregation or {}).get('group_by') if contract else []
            if groups and item.field in groups:
                available.update(_canonical_filter_value(item.field, row['group'][groups.index(item.field)])
                                 for row in entry['result']['rows'] if not row.get('is_other'))
    if not values.issubset(available):
        raise ValueError('이번 요청에서 실제 반환한 집계의 해당 그룹 값만 추가 조건으로 사용할 수 있습니다.')
    return {'field': item.field, 'origin': 'aggregation', 'evidence_ref': item.evidence_ref}


def _extra_origin(item, context: 'ToolContext', source: str) -> dict:
    if item.evidence_ref and not item.evidence and source == 'mantis':
        if item.evidence_ref.startswith('Q'):
            return _aggregation_origin(item, context)
        return _ledger_origin(item, context)
    if item.evidence_ref or not item.evidence or item.operator not in {'equals', 'in', 'contains', 'gte', 'lte', 'between'}:
        raise ValueError('추가 조건에는 질문 구절 또는 현재 원장의 같은 필드 근거가 필요합니다.')
    # 기존 질문 값·별칭·상대 날짜 검증을 재사용한다. 임의의 증명 문자열은 허용하지 않는다.
    check_clause(item.model_dump(), context.question, source)
    return {'field': item.field, 'origin': 'question', 'evidence': item.evidence}


def bind_step(context: 'ToolContext', source: str, args: QueryArguments, *,
              explicit_required: bool, list_limit: int | None) -> tuple[QueryContract, SourceConditions, dict]:
    root = context.query_contracts[source]
    if args.condition_ref:
        contract = context.condition_refs.get(args.condition_ref)
        if contract is None and root.ref == args.condition_ref:
            contract = root
        if (contract is None or contract.conditions.source != source or not contract.accepted
                or contract.scope_id != root.scope_id
                or args.purpose is not None and args.purpose != contract.purpose):
            raise ValueError('같은 요청·원천·용도에서 검증된 조건 참조만 사용할 수 있습니다.')
        bound, validation = contract.bind(args, explicit_required=explicit_required,
            list_limit=list_limit if contract.purpose == 'answer' else None)
        if contract.purpose == 'investigate' and list_limit is not None:
            contract = replace(contract, list_limit=list_limit)
        return contract, bound, validation
    if args.purpose != 'investigate':
        bound, validation = root.bind(args, explicit_required=explicit_required, list_limit=list_limit)
        context.condition_refs[root.ref] = root
        return root, bound, validation
    if args.filters is None or source == 'mantis' and args.scope is None:
        raise ValueError('추가 조사 단계에는 필수 조건을 포함한 전체 filters와 scope를 지정하세요.')
    proposed = [_canonical(item.model_dump()) for item in args.filters]
    if any(not clause_supported(source, c) for c in proposed):
        raise ValueError('이 원천에서 지원하지 않는 추가 조건입니다.')
    if not all(any(_same(c, candidate) for candidate in proposed) for c in root.filters):
        raise ValueError('추가 조사에서도 고객사·미해결·기간 등 질문의 필수 조건을 빠뜨릴 수 없습니다.')
    if args.scope is not None and args.scope != root.scope:
        raise ValueError('인증·감사로 확정한 조회 범위를 바꿀 수 없습니다.')
    extras = [item for item in args.filters if not any(_same(item.model_dump(), c) for c in root.filters)]
    origins = [*deepcopy(root.origins), *(_extra_origin(item, context, source) for item in extras)]
    filters = [*deepcopy(root.filters), *(_canonical(item.model_dump()) for item in extras)]
    # 기존 파생 키(상태·팀·고객사 등)는 보존하고 추가 절을 AND로 적용한다.
    conditions = replace(root.conditions, constraints={**deepcopy(root.conditions.constraints), 'filter_clauses': filters},
        applied=tuple(dict.fromkeys((*root.conditions.applied, *(FIELD_LABELS[c['field']] for c in filters)))))
    contract = QueryContract(conditions, filters, origins, root.scope,
        args.sort.model_dump() if args.sort else deepcopy(root.sort), list_limit or root.list_limit,
        aggregation=deepcopy(root.aggregation), purpose='investigate', parent_ref=root.ref, scope_id=root.scope_id,
        population_limit=root.population_limit, report=deepcopy(root.report), terms=list(root.terms))
    bound, validation = contract.bind(args, explicit_required=explicit_required)
    context.condition_refs[contract.ref] = contract
    return contract, bound, validation


def step_view(contract: QueryContract, conditions: SourceConditions) -> dict:
    return {'condition_ref': contract.ref, 'parent_ref': contract.parent_ref, 'source': conditions.source,
            'purpose': contract.purpose, 'filters': deepcopy(conditions.constraints.get('filter_clauses') or []),
            'scope': contract.scope, 'sort': deepcopy(contract.sort), 'origins': deepcopy(contract.origins),
            'evidence_grade': conditions.grade, 'unapplied_conditions': list(conditions.unapplied),
            **({'population_limit': contract.population_limit} if contract.population_limit else {}),
            **({'report': deepcopy(contract.report)} if contract.report else {}),
            **({'query_scope_id': contract.scope_id} if contract.scope_id else {})}


def read_target_matches(args: QueryArguments, conditions: SourceConditions) -> bool:
    """상세 ID도 단계 필터에 포함되어야 한다. 본문 캐시보다 먼저 검사한다."""
    targets = getattr(args, 'issue_ids', None) or ([args.issue_id] if hasattr(args, 'issue_id') else [])
    for clause in conditions.constraints.get('filter_clauses') or []:
        if clause['field'] == 'issue_id' and clause['operator'] in {'equals', 'in'}:
            allowed = {int(v.lstrip('#')) for v in values_for_clause(clause.get('value'))}
            if not set(targets).issubset(allowed):
                return False
    return True
