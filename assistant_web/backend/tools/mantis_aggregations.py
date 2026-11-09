"""검증된 집계 명세를 기존 전체 저장본 엔진으로 실행하는 읽기 전용 도구."""
from copy import deepcopy
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.reports.aggregation import execute_aggregation
from backend.tools.mantis_queries import query_constraints, query_snapshot
from backend.tools.query_conditions import QueryArguments
from backend.tools.registry import ToolContext, ToolSpec, error_result


class AggregatePeriod(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True)
    start: str = Field(alias='from', pattern=r'^\d{4}-\d{2}-\d{2}$')
    to: str = Field(pattern=r'^\d{4}-\d{2}-\d{2}$')


class AggregateIssuesArgs(QueryArguments):
    group_by: list[Literal['customer', 'platform', 'status', 'owner', 'issue_type', 'month']] = Field(max_length=3)
    metrics: list[Literal['issue_count', 'effort']] = Field(default=['issue_count'], min_length=1, max_length=2)
    period: AggregatePeriod | None = None
    comparison: Literal['previous_month'] | None = None
    top_n: int | None = Field(default=None, ge=1, le=20, strict=True)


def aggregate_mantis_issues(args: AggregateIssuesArgs, context: ToolContext) -> dict:
    tool = 'aggregate_mantis_issues'
    contract = context.query_contracts.get('mantis')
    proposed = args.model_dump(by_alias=True, include={'group_by', 'metrics', 'period', 'comparison', 'top_n'})
    if (contract is None or contract.aggregation is None or context.conditions['mantis'].grade != 'scoped'
            or proposed != contract.aggregation_arguments()):
        return error_result(tool, 'condition_mismatch', '검증된 집계 그룹·지표·기간·비교·상위 개수를 그대로 사용하세요.')
    spec = deepcopy(contract.aggregation)
    constraints = deepcopy(context.conditions['mantis'].constraints)
    if spec.get('period') is not None:
        if contract.population_limit:
            return error_result(tool, 'condition_mismatch', '기간별 집계 엔진은 최근 N건 모집단을 지원하지 않습니다. 모집단 조건을 생략해 전체를 집계할 수 없습니다.')
        # 기간별 등록 건수/공수는 기존 엔진의 서로 다른 날짜 기준을 유지한다.
        if context.mantis_query_terms:
            from backend.query_filters import normalize_filter_clauses
            constraints['filter_clauses'] = normalize_filter_clauses([*constraints.get('filter_clauses', []),
                *({'field': 'keyword', 'operator': 'contains', 'value': t} for t in context.mantis_query_terms)])
        try:
            result = execute_aggregation({'aggregation': spec, 'resolved_constraints': constraints})
        except (LookupError, ValueError):
            return error_result(tool, 'search_incomplete', '전체 저장본을 확인하지 못해 집계를 확정하지 않았습니다.', retryable=True)
        if result.get('source', {}).get('search_complete') is not True:
            return error_result(tool, 'search_incomplete', '집계 원천 조회가 미완료입니다.', retryable=True)
        entry = {'kind': 'period', 'result': result, 'applied_filters': constraints.get('filter_clauses', [])}
        output = {'totals': result['totals'], 'results': result['rows'], 'date_basis': result['date_basis'],
                  'effective_period': result['effective_period'], 'snapshot_as_of': result['source'].get('fetchedAt')}
    else:
        stage = query_snapshot(context, query_constraints(context))
        meta = stage.get('meta') or {}
        if stage.get('status') != 'ok' or meta.get('search_complete') is not True:
            return error_result(tool, 'search_incomplete', '전체 조회가 미완료여서 그룹별 건수를 확정하지 않았습니다.', retryable=True)
        groups = meta.get('group_counts')
        if not isinstance(groups, dict) or sum(groups.values()) != meta.get('found_count'):
            return error_result(tool, 'tool_error', '전체 건수와 그룹별 합계를 대조하지 못했습니다.')
        entry = {'kind': 'simple', 'group_by': spec['group_by'][0], 'groups': groups,
                 'total_count': meta['found_count'], 'snapshot_as_of': meta.get('fetchedAt'),
                 'matched_issue_ids': meta.get('matched_issue_ids', []), 'matched_groups': meta.get('matched_groups', {}),
                 'applied_filters': query_constraints(context).get('filter_clauses', [])}
        output = {'totals': {'issue_count': meta['found_count']},
                  'results': [{'group': [group], 'values': {'issue_count': value}} for group, value in sorted(groups.items())],
                  'snapshot_as_of': meta.get('fetchedAt')}
    context.ledger.add('mantis', {'source': 'mantis_aggregation', 'status': 'ok',
        'meta': {'search_complete': True, 'aggregation_result': entry}},
        grade='scoped', unapplied=(), call_id=context.call_id)
    return {'ok': True, 'tool': tool, 'untrusted_content': True, 'evidence_grade': 'scoped',
            'search_complete': True, 'aggregation': proposed, 'applied_filters': entry['applied_filters'], **output}


SPECS = [ToolSpec('aggregate_mantis_issues',
    '전체 저장본에서 확정한 그룹·기간·지표를 집계한다. query_contracts.aggregation의 인자를 보존한다. '
    '등록 건수는 등록일, 공수는 고객사전달일 기준이다. 부분 조회로 전체 집계를 만들지 않는다.',
    AggregateIssuesArgs, 'mantis', aggregate_mantis_issues)]
