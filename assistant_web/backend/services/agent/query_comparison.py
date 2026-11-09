"""범위별 원장에서만 비교 수치와 목록을 만든다. 모델 초안의 수치는 사용하지 않는다."""
from copy import deepcopy
from html import escape
from itertools import combinations

from backend.mantis_status import display_mantis_status_label
from backend.query_filters import filter_clause_label
from backend.services.agent.query_answers import render_aggregation
from backend.tools.conditions import FIELD_LABELS
from backend.utils.text import compact_text


def scope_stages(stages: list[dict], scope_id: str) -> list[dict]:
    result = []
    for original in stages:
        step = original.get('meta', {}).get('condition_step') or {}
        if step.get('query_scope_id') != scope_id or step.get('purpose') != 'answer':
            continue
        stage = deepcopy(original)
        meta = stage['meta']
        for key in ('query_result', 'aggregation_result'):
            if 'comparison_' + key in meta:
                meta[key] = meta.pop('comparison_' + key)
        result.append(stage)
    return result


def comparison_result(plan: dict, stages: list[dict]) -> dict:
    scopes = []
    for scope in plan['query_scopes']:
        scoped = scope_stages(stages, scope['id'])
        queries = [s for s in scoped if s.get('meta', {}).get('query_result')]
        aggregates = [s['meta']['aggregation_result'] for s in scoped if s.get('meta', {}).get('aggregation_result')]
        complete = bool(queries and all(s['meta']['query_result'].get('search_complete') is True for s in queries))
        latest = queries[-1]['meta']['query_result'] if queries else {}
        missing = []
        if scope['plan']['operation'] == 'search' and not any(s['meta']['query_result']['operation'] == 'list' for s in queries):
            missing.append('목록')
        if (scope['plan'].get('group_by') or scope['plan'].get('aggregation') or scope['plan']['operation'] == 'aggregate') and not aggregates:
            missing.append('집계')
        scopes.append({'id': scope['id'], 'label': scope['label'], 'complete': complete,
            'total_count': latest.get('total_count') if complete else None,
            'observed_count': latest.get('observed_count'), 'missing': missing,
            'matched_ids': sorted(set(queries[-1]['meta'].get('matched_issue_ids') or [])) if complete else [],
            'snapshot_as_of': latest.get('snapshot_as_of')})
    complete = bool(scopes and all(s['complete'] and not s['missing'] for s in scopes))
    total = sum(s['total_count'] for s in scopes) if complete else None
    union = len({identifier for s in scopes for identifier in s['matched_ids']}) if complete else None
    differences = [{'left': a['id'], 'right': b['id'], 'value': a['total_count'] - b['total_count']}
                   for a, b in combinations(scopes, 2)] if complete else []
    return {'complete': complete, 'scopes': scopes, 'arithmetic_total': total, 'union_count': union,
            'duplicate_memberships': total - union if total is not None and union is not None else None, 'differences': differences}


def _safe(value) -> str:
    return escape(str(value), quote=False).replace('|', '&#124;').replace('\n', ' ')


def _condition_label(clause: dict) -> str:
    displayed = deepcopy(clause)
    if clause['field'] == 'status':
        value = clause.get('value')
        displayed['value'] = ([display_mantis_status_label(v) for v in value] if isinstance(value, list)
                              else display_mantis_status_label(value))
    return filter_clause_label(displayed).replace(clause['field'], FIELD_LABELS.get(clause['field'], clause['field']), 1)


def scope_text(scope: dict, stages: list[dict]) -> tuple[str, list[str]]:
    queries = [s for s in stages if s.get('meta', {}).get('query_result')]
    refs = []
    if queries:
        entry = queries[-1]['meta']['query_result']
        lines = ([f"적용 조건에 맞는 Mantis 이슈는 {entry['total_count']}건입니다."] if entry['search_complete'] else
                 [f"전체 조회 미완료 · 확인한 범위 {entry['observed_count']}건 · 총건수 미확정"])
        listed = next((s for s in reversed(queries) if s['meta']['query_result']['operation'] == 'list'), None)
        if listed:
            lines.append('')
            for position, row in enumerate(listed['issues_list'], 1):
                lines.append(f"{position}. #{row['id']} {_safe(compact_text(str(row.get('summary') or '제목 없음'), 500))}")
                refs.append(f"M#{row['id']}")
    else:
        lines = ['조회 미완료 · 총건수 미확정']
    plan = scope['plan']
    conditions = [_condition_label(c) for c in plan['filter_clauses']]
    conditions.append({'all': '전사 범위', 'team': '팀 범위', 'target': '지정 대상 범위'}.get(
        plan['resolved_constraints'].get('mantis_team_scope'), '확정 담당자 범위'))
    lines.append('적용 조건 · ' + _safe(' / '.join(conditions)))
    aggregates = [s['meta']['aggregation_result'] for s in stages if s.get('meta', {}).get('aggregation_result')]
    if aggregates:
        lines.extend(['', render_aggregation(scope['question'], plan, aggregates[-1])['content']])
    return '\n'.join(lines), refs


def render_comparison(plan: dict, stages: list[dict]) -> tuple[str, list[str], dict]:
    result = comparison_result(plan, stages)
    lines = ['| 범위 | 건수 | 조회 |', '| --- | ---: | --- |']
    for item in result['scopes']:
        number = str(item['total_count']) if item['complete'] else '미확정'
        state = '전체 조회 완료' if item['complete'] else '조회 미완료'
        if item['missing']:
            state += ' · ' + ', '.join(item['missing']) + ' 미완료'
        lines.append(f"| {_safe(item['label'])} | {number} | {state} |")
    lines.append('')
    labels = {s['id']: _safe(s['label']) for s in result['scopes']}
    if result['complete']:
        lines.extend(f"{labels[d['left']]} − {labels[d['right']]}: {d['value']:+d}건" for d in result['differences'])
        lines.extend([f"\n범위별 건수의 단순 합계: {result['arithmetic_total']}건",
                      f"중복 제외 합계: {result['union_count']}건",
                      f"범위 간 중복 집계분: {result['duplicate_memberships']}건"])
    else:
        lines.append('모든 범위의 조회가 끝나지 않아 차이와 합계는 확정하지 않았습니다.')
    refs = []
    for scope in plan['query_scopes']:
        content, cited = scope_text(scope, scope_stages(stages, scope['id']))
        lines.extend(['', f"### {_safe(scope['label'])}", '', content])
        refs.extend(cited)
    return '\n'.join(lines), list(dict.fromkeys(refs)), result


def comparison_outcome(plan: dict, stages: list[dict]) -> str:
    result = comparison_result(plan, stages)
    if result['complete']:
        return 'complete_empty' if result['union_count'] == 0 else 'sufficient_evidence'
    return 'partial_evidence' if any(s['complete'] or s['observed_count'] for s in result['scopes']) else 'search_incomplete'
