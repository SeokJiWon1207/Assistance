"""기존 응답을 보존하는 집계 shadow 비교와 명시적 primary 전환."""
from copy import deepcopy
import os
import sqlite3
from typing import Any

from backend.intent.aggregation_spec import period_clause
from backend.reports.aggregation import execute_aggregation, group_digest
from backend.sources.mantis_store import with_store_scope

LABELS = {'customer': '고객사', 'platform': '매체', 'status': '상태', 'owner': '담당자',
          'issue_type': '이슈 유형', 'month': '월', 'issue_count': '등록 건수', 'effort': '공수'}


def legacy_plan(plan):
    spec = plan['aggregation']
    metrics, groups = spec['metrics'], spec['group_by']
    count = metrics == ['issue_count'] and len(groups) <= 1 and 'issue_type' not in groups
    effort = metrics == ['effort'] and not groups
    if not count and not effort:
        return None
    result = deepcopy(plan)
    result.pop('aggregation', None)
    clause = period_clause(spec, metrics[0])
    c = result['resolved_constraints']
    c.setdefault('filter_clauses', []).append(clause)
    # 명시적인 추가 날짜 필터와의 AND는 filter_clauses에 그대로 남긴다.
    c[clause['field']] = {k: spec['period'][k] for k in ('from', 'to')}
    result['filter_clauses'] = deepcopy(c['filter_clauses'])
    result.update(operation='count' if count else 'report', group_by=groups[0] if groups else None,
                  limit=None, report_kind=None if count else 'effort')
    if effort:
        result['effort'] = {'type': 'total'}
    return result


def execute_legacy(question, plan):
    if plan['operation'] == 'count':
        from backend.reports.aggregate import build_mantis_count_report
        return build_mantis_count_report(question, plan)
    from backend.reports.effort import build_effort_report_from_unified_plan
    return build_effort_report_from_unified_plan(question, plan)


def compare_legacy(legacy, result, spec):
    stages = (legacy.get('retrieval') or {}).get('stages', [])
    stage: dict[str, Any] = next((s for s in stages if 'total_count' in s.get('meta', {}) or 'total_effort' in s.get('meta', {})), {})
    meta = stage.get('meta') or {}
    if (stage.get('status') != 'ok' or meta.get('search_complete') is not True or
            not meta.get('fetchedAt') or meta['fetchedAt'] != result['source'].get('fetchedAt')):
        return {'status': 'not_comparable', 'reason': '완전성 또는 동일 저장본 수집 시각을 확인하지 못했습니다.'}
    metric = spec['metrics'][0]
    totals = {metric: meta.get('total_count' if metric == 'issue_count' else 'total_effort')}
    matched = totals[metric] is not None and abs(totals[metric] - result['totals'][metric]) < 0.0001
    if metric == 'issue_count' and spec['group_by']:
        matched = matched and 'group_counts' in meta and group_digest(meta['group_counts']) == result['group_digest']
    return {'status': 'matched' if matched else 'mismatch', 'legacy_totals': totals,
            'candidate_totals': result['totals'], 'scope': 'totals_and_groups' if spec['group_by'] else 'totals',
            'fetchedAt': meta['fetchedAt'], 'elapsed_ms': result['elapsed_ms']}


def cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ').replace('<', '&lt;').replace('>', '&gt;')


def number(value):
    return f'{value:g}'


def render_report(question, plan, result):
    spec = plan['aggregation']
    headers = [LABELS[d] for d in result['dimensions']]
    if result['periods']:
        headers.append('월')
    if not headers:
        headers.append('범위')
    headers.extend(LABELS[m] for m in spec['metrics'])
    if spec['comparison']:
        headers.extend(f'{LABELS[m]} 전월 대비' for m in spec['metrics'])
    period = result['effective_period']
    lines = [f"집계 기간: {period['from']} ~ {period['to']}",
             '날짜 기준: ' + ', '.join('등록 건수=등록일' if m == 'issue_count' else '공수=고객사전달일' for m in spec['metrics']),
             f"전체 저장본 수집 시각: {result['source'].get('fetchedAt', '미상')}",
             '합계: ' + ', '.join(f'{LABELS[m]} {number(v)}' for m, v in result['totals'].items()), '',
             '| ' + ' | '.join(headers) + ' |', '| ' + ' | '.join('---' for _ in headers) + ' |']
    for row in result['rows']:
        values = [cell(v) for v in row['group']]
        if row['period']:
            values.append(row['period'])
        if not values:
            values.append('전체')
        values.extend(number(row['values'][m]) for m in spec['metrics'])
        if spec['comparison']:
            for metric in spec['metrics']:
                c = row['comparisons'][metric]
                label = ('신규 발생' if c['state'] == 'new' else '부분 월 비교 제외' if c['state'] == 'partial_period'
                         else f"{c['delta']:+g} ({c['percentage']:+.1f}%)")
                values.append(label)
        lines.append('| ' + ' | '.join(values) + ' |')
    if result['other_group_count']:
        lines.append(f"상위 {spec['top_n']}개 그룹과 나머지 {result['other_group_count']}개 그룹의 합계(기타)를 모든 월에 동일하게 적용했습니다.")
    for metric, missing in result['missing_dates'].items():
        if missing:
            lines.append(f'{LABELS[metric]} 기준 날짜가 없는 {missing}건은 해당 지표에서 제외했습니다.')
    return {'content': '\n'.join(lines), 'sources': [], 'aggregation': result,
            'retrieval': {'query': question, 'query_plan': plan, 'intent_plan': plan,
                          'aggregation_result': result,
                          'constraints': plan['resolved_constraints'], 'stages': [
                              {'source': 'mantis_aggregation', 'status': 'ok', 'meta': {
                                  **result['source'], 'filter_audit': result['filter_audit'],
                                  'totals': result['totals'], 'date_basis': result['date_basis']}}]},
            'raw': {'mode': 'validated_aggregation'}}


def unavailable(question, plan, reason, audit):
    return {'content': '요청한 집계를 확정하지 못했습니다. ' + reason, 'sources': [],
            'retrieval': {'query': question, 'query_plan': plan, 'intent_plan': plan,
                          'constraints': plan['resolved_constraints'], 'aggregation_shadow': audit,
                          'stages': [{'source': 'mantis_aggregation', 'status': 'error', 'reason': reason}]},
            'raw': {'mode': 'aggregation_unavailable'}}


@with_store_scope
def run_aggregation_report(question, plan, model, api_key):
    mode = os.getenv('MANTIS_AGGREGATION_MODE', 'shadow').lower().strip()
    if mode not in {'off', 'shadow', 'primary'}:
        mode = 'shadow'
    compatible = legacy_plan(plan)
    legacy = execute_legacy(question, compatible) if compatible and mode != 'primary' else None
    if mode == 'off':
        return legacy or unavailable(question, plan, '복합 집계 기능이 비활성화되어 있습니다.', {'status': 'disabled'})
    try:
        result = execute_aggregation(plan)
    except (LookupError, OSError, sqlite3.Error, ValueError) as exc:
        audit = {'status': 'unavailable', 'reason': str(exc)}
        if not legacy:
            return unavailable(question, plan, str(exc), audit)
    else:
        if mode == 'primary':
            from backend.reports.aggregation_narrative import summarize_aggregation
            report = render_report(question, plan, result)
            narrative, narrative_audit = summarize_aggregation(question, result, model, api_key)
            report['retrieval']['aggregation_narrative'] = narrative_audit
            if narrative:
                report['content'] = narrative + '\n\n' + report['content']
            return report
        audit = compare_legacy(legacy, result, plan['aggregation']) if legacy else {
            'status': 'not_comparable', 'reason': '동일한 집계를 수행하는 기존 빌더가 없습니다.',
            'candidate_totals': result['totals'], 'elapsed_ms': result['elapsed_ms']}
    if legacy:
        retrieval = legacy.setdefault('retrieval', {})
        retrieval.update(aggregation_shadow=audit, query_plan=plan, intent_plan=plan)
        return legacy
    return unavailable(question, plan, '새 복합 집계를 검증 중입니다. 아직 응답 전환 전입니다.', audit)
