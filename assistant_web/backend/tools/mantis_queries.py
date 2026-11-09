"""서버가 확정한 동일한 Mantis 조회 범위에서 건수와 정렬된 표시 항목을 읽는다."""
from copy import deepcopy
import json
from typing import Any, Literal

from pydantic import Field

from backend.query_filters import normalize_filter_clauses
from backend.sources.mantis import execute_mantis_snapshot_search
from backend.tools.registry import ToolContext, ToolSpec, error_result
from backend.tools.query_conditions import QueryArguments


class CountIssuesArgs(QueryArguments):
    pass


class ListIssuesArgs(CountIssuesArgs):
    limit: int = Field(default=5, ge=1, le=200, description="질문에서 요청한 표시 건수")


def query_constraints(context: ToolContext) -> dict:
    constraints = deepcopy(context.conditions['mantis'].constraints)
    contract = context.query_contracts.get('mantis')
    spec = contract.aggregation if contract else None
    if spec and spec.get('period'):
        # 건수·목록의 날짜 기준은 등록일이다. 공수 집계와 혼합하지 않는다.
        if 'issue_count' not in spec['metrics']:
            raise ValueError('공수 전용 집계에서는 건수·목록 대신 집계 도구를 사용하세요.')
        from backend.intent.aggregation_spec import period_clause
        constraints['filter_clauses'] = [*constraints.get('filter_clauses', []), period_clause(spec, 'issue_count')]
    if context.mantis_query_terms:
        constraints['filter_clauses'] = normalize_filter_clauses([*constraints.get('filter_clauses', []),
            *({'field': 'keyword', 'operator': 'contains', 'value': term} for term in context.mantis_query_terms)])
    constraints.setdefault('sort', {'field': 'created_at', 'direction': 'desc'})
    return constraints


def query_snapshot(context: ToolContext, constraints: dict) -> dict:
    contract = context.query_contracts.get('mantis')
    spec = contract.aggregation if contract else None
    limit = max(20, contract.list_limit if contract else 20)
    population = contract.population_limit if contract else None
    key = json.dumps({'constraints': constraints, 'limit': limit, 'aggregation': spec, 'population': population}, sort_keys=True, ensure_ascii=False)
    stage = context.ledger.mantis_query_snapshots.get(key)
    if stage is None:
        options = {}
        if spec and spec.get('period') is None and spec['group_by']:
            from backend.reports.aggregate import count_group_key
            options['group_key'] = count_group_key(spec['group_by'][0])
        if population:
            from backend.tools.population import query_population
            stage = query_population(constraints, population, limit, options.get('group_key'))
        else:
            stage = execute_mantis_snapshot_search(limit, constraints, **options)
        if stage.get('status') == 'ok':
            context.ledger.mantis_query_snapshots[key] = stage
    return stage


def _query(kind: Literal["count", "list"], context: ToolContext, limit: int) -> dict[str, Any]:
    tool = f"{kind}_mantis_issues"
    conditions = context.conditions["mantis"]
    if conditions.grade != "scoped":
        return error_result(tool, "conditions_mismatch", "모든 이슈 조건을 적용할 수 있는 조회 범위가 필요합니다.")
    try:
        constraints = query_constraints(context)
    except ValueError as exc:
        return error_result(tool, 'condition_mismatch', str(exc))
    ordering = constraints.get("sort") or {"field": "created_at", "direction": "desc"}
    constraints["sort"] = ordering
    stage = query_snapshot(context, constraints)
    if stage.get('status') != 'ok':
        return error_result(tool, 'tool_error', 'Mantis 저장본을 조회하지 못했습니다.', retryable=True)
    meta = stage["meta"]
    complete = meta.get("search_complete") is True
    observed = meta["found_count"]
    rows = stage["issues_list"][:limit] if kind == "list" else []
    result = {"operation": kind, "total_count": observed if complete else None, "observed_count": observed,
              "search_complete": complete, "ordered_ids": [row["id"] for row in rows], "sort": ordering,
              "snapshot_as_of": meta.get("fetchedAt") or meta.get("snapshot_as_of"),
              "team_scope": constraints.get("mantis_team_scope"), "team_label": constraints.get("mantis_team_label"),
              "applied_filters": deepcopy(constraints.get("filter_clauses") or [])}
    if meta.get('population_limit'):
        result['population_limit'] = meta['population_limit']
        result['population_basis'] = meta['population_basis']
    context.ledger.add("mantis", {"source": "mantis_snapshot", "status": "ok", "issues_list": rows,
        "meta": {"operation": kind, "total_count": result["total_count"], "found_count": observed,
                 "displayed_count": len(rows), "search_complete": complete,
                 "matched_issue_ids": meta.get("matched_issue_ids", []),
                 "ordered_matched_ids": (meta.get("ordered_matched_ids") or [])[:len(rows)],
                 "fetchedAt": result["snapshot_as_of"], "query_result": result}},
        grade="scoped", unapplied=(), call_id=context.call_id)
    detail = _selected_detail(context) if kind == 'list' else {}
    return {"ok": True, "tool": tool, "untrusted_content": True, "evidence_grade": "scoped", **result, **detail,
            "results": [{"ref": f"M#{r['id']}", "id": r["id"], "title": r.get("summary"),
                         "created_at": r.get("created_at"), "status": r.get("status_label") or r.get("status")}
                        for r in rows]}


def _selected_detail(context: ToolContext) -> dict:
    """현재 표시 순번의 상세 인자만 안내한다. 실행 시 기존 단계 검증을 다시 거친다."""
    target = context.ordinal_issue()
    contract = context.query_contracts.get('mantis')
    if not context.structured_conditions or target is None or contract is None:
        return {}
    args = {'issue_id': target, 'purpose': 'investigate', 'scope': contract.scope,
            'filters': [*deepcopy(contract.filters),
                        {'field': 'issue_id', 'operator': 'equals', 'value': str(target), 'evidence_ref': f'M#{target}'}],
            **({'population_limit': contract.population_limit} if contract.population_limit else {}),
            **({'query_scope_id': contract.scope_id} if contract.scope_id else {})}
    return {'selected_issue_detail': {'tool': 'get_mantis_issue_detail', 'arguments': args}}


def count_mantis_issues(args: CountIssuesArgs, context: ToolContext) -> dict[str, Any]:
    return _query("count", context, 0)


def list_mantis_issues(args: ListIssuesArgs, context: ToolContext) -> dict[str, Any]:
    return _query("list", context, args.limit)


SPECS = [
    ToolSpec("count_mantis_issues", "확정된 고객사·기간·상태 조건에 맞는 전체 이슈 건수를 센다. "
             "조회가 완전할 때만 total_count가 있다.", CountIssuesArgs, "mantis", count_mantis_issues),
    ToolSpec("list_mantis_issues", "같은 조건과 저장 시각에서 이슈를 정렬해 표시한다. "
             "순번은 ordered_ids 기준이며 기본 정렬은 최근 등록순이다.", ListIssuesArgs, "mantis", list_mantis_issues),
]
