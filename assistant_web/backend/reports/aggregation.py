"""전체 원본의 정규화 행을 대상으로 하는 제한된 Python 집계기."""
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from calendar import monthrange
from time import perf_counter
import hashlib
import json

from backend.common import display_mantis_status_label
from backend.effort_fields import issue_delivery_date, issue_effort_fields, parse_effort_field_value
from backend.execution import checkpoint
from backend.intent.aggregation_spec import METRIC_DATES, check_shape
from backend.query_filters import matches_source_filters, filter_audit_for_source
from backend.sources.trend_snapshot import trend_rows

MAX_BUCKETS = 100_000


def group_digest(groups):
    nonzero = {key: value for key, value in groups.items() if value}
    return hashlib.sha256(json.dumps(nonzero, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prior_month(month):
    return (date.fromisoformat(month + '-01') - timedelta(days=1)).strftime('%Y-%m')


def month_sequence(start, end):
    months = []
    current = start.replace(day=1)
    while current <= end:
        months.append(current.strftime('%Y-%m'))
        current = (current.replace(day=28) + timedelta(days=4)).replace(day=1)
    return months


def group_values(issue, dimensions):
    values = {'customer': issue.get('project'), 'platform': issue.get('media_type'),
              'status': display_mantis_status_label(issue.get('status')),
              'owner': issue.get('handler'), 'issue_type': issue.get('severity_label') or issue.get('severity')}
    return tuple(str(values.get(d) or '미지정') for d in dimensions)


def effort_value(issue, constraints):
    clauses = [f for f in constraints.get('filter_clauses', []) if f['field'] == 'effort_person']
    return sum((Decimal(str(amount)) for value in issue_effort_fields(issue).values()
                for person, amount in parse_effort_field_value(value)
                if matches_source_filters('mantis', {'effort_person': person}, clauses)), Decimal(0))


def change(current, previous, *, complete=True):
    delta = round(current - previous, 4)
    if not complete:
        return {'previous': previous, 'delta': delta, 'percentage': None, 'state': 'partial_period'}
    return {'previous': previous, 'delta': delta,
            'percentage': round(delta / previous * 100, 2) if previous else (0.0 if current == 0 else None),
            'state': 'unchanged' if delta == 0 else 'new' if previous == 0 else 'changed'}


def aggregate_rows(rows, spec, constraints, *, today=None):
    """rows는 원본 조건 검증을 마친 trend_rows의 스트림. 표시 상한은 계산 후 적용한다."""
    check_shape(spec)
    start = date.fromisoformat(spec['period']['from'])
    end = min(date.fromisoformat(spec['period']['to']), today or date.today())
    if start > end:
        raise LookupError('아직 도래하지 않은 기간은 실제 0건으로 집계할 수 없습니다.')
    periods = month_sequence(start, end)
    monthly = 'month' in spec['group_by']
    comparison = spec['comparison'] == 'previous_month'
    baseline = date.fromisoformat(prior_month(periods[0]) + '-01') if comparison else start
    dimensions = [d for d in spec['group_by'] if d != 'month']
    metrics = spec['metrics']
    buckets: dict[tuple, dict[str, Decimal]] = defaultdict(lambda: {m: Decimal(0) for m in metrics})
    totals = {m: Decimal(0) for m in metrics}
    missing_dates = {m: 0 for m in metrics}
    seen = set()
    duplicates = 0
    for _, _, _, issue in rows:
        checkpoint()
        if not issue or not issue.get('id'):
            raise LookupError('정규화된 이슈 식별자가 없어 중복 없는 집계를 보장할 수 없습니다.')
        identity = str(issue['id'])
        if identity in seen:
            duplicates += 1
            continue
        seen.add(identity)
        group = group_values(issue, dimensions)
        for metric in metrics:
            value_date = issue.get('created_at') if metric == 'issue_count' else issue_delivery_date(issue)
            try:
                day = date.fromisoformat(str(value_date or '')[:10])
            except ValueError:
                missing_dates[metric] += 1
                continue
            if not baseline <= day <= end:
                continue
            # 첫 달의 조회 시작 이전 일자를 비교 기간으로 혼합하지 않는다.
            if baseline < start and day < start and day.strftime('%Y-%m') == periods[0]:
                continue
            if not comparison and day < start:
                continue
            amount = Decimal(1) if metric == 'issue_count' else effort_value(issue, constraints)
            bucket = (group, day.strftime('%Y-%m') if monthly else '')
            buckets[bucket][metric] += amount
            if day >= start:
                totals[metric] += amount
            if len(buckets) > MAX_BUCKETS:
                raise LookupError('집계 그룹이 너무 많습니다. 기간이나 그룹 조건을 좁혀 주세요.')
    return finish_result(buckets, totals, spec, dimensions, periods, start, end,
                         missing_dates, duplicates, len(seen))


def finish_result(buckets, totals, spec, dimensions, periods, start, end, missing, duplicates, scanned):
    metrics = spec['metrics']
    monthly = 'month' in spec['group_by']
    comparison = spec['comparison'] == 'previous_month'
    zero = {m: Decimal(0) for m in metrics}
    ranks: dict[tuple, Decimal] = defaultdict(lambda: Decimal(0))
    for (group, period), values in buckets.items():
        ranks[group] += values[metrics[0]] if not monthly or period in periods else 0
    if not dimensions and not ranks:
        ranks[()] = Decimal(0)
    selected = sorted(ranks, key=lambda g: (-ranks[g], g))[:spec['top_n']]
    others = set(ranks) - set(selected)
    display_groups = [(g, False) for g in selected]
    if others:
        display_groups.append((tuple('기타' for _ in dimensions), True))

    def values_for(group, period, is_other):
        values = dict(zero)
        for key in (sorted(others) if is_other else [group]):
            for metric, value in buckets.get((key, period), zero).items():
                values[metric] += value
        return {m: float(round(v, 4)) if m == 'effort' else int(v) for m, v in values.items()}

    result_rows = []
    for group, is_other in display_groups:
        for period in periods if monthly else ['']:
            values = values_for(group, period, is_other)
            comparisons = {}
            if comparison:
                previous_period = prior_month(period)
                previous = values_for(group, previous_period, is_other)
                first = date.fromisoformat(period + '-01')
                last = first.replace(day=monthrange(first.year, first.month)[1])
                # 현재/전월 중 잘린 달이 있으면 증감률을 전체 월 비교로 표현하지 않는다.
                complete = first >= start and last <= end and not (previous_period == periods[0] and start.day != 1)
                comparisons = {m: change(values[m], previous[m], complete=complete) for m in metrics}
            result_rows.append({'group': list(group), 'period': period, 'values': values,
                                'is_other': is_other, 'comparisons': comparisons})
    comparison_groups = {}
    if spec['metrics'] == ['issue_count'] and len(spec['group_by']) == 1:
        for (group, period), values in buckets.items():
            if not monthly or period in periods:
                comparison_groups[period if monthly else group[0]] = int(values['issue_count'])
    return {'spec': spec, 'dimensions': dimensions, 'periods': periods if monthly else [],
            'rows': result_rows, 'totals': {m: float(round(v, 4)) if m == 'effort' else int(v) for m, v in totals.items()},
            'date_basis': {m: METRIC_DATES[m] for m in metrics}, 'missing_dates': missing,
            'duplicates': duplicates, 'scanned_count': scanned, 'group_count': len(ranks),
            'other_group_count': len(others), 'group_digest': group_digest(comparison_groups),
            'effective_period': {'from': start.isoformat(), 'to': end.isoformat()}}


def execute_aggregation(plan):
    spec = check_shape(plan['aggregation'])
    constraints = plan.get('resolved_constraints') or {}
    audit = filter_audit_for_source('mantis', constraints.get('filter_clauses', []))
    if not audit['fully_applied']:
        raise LookupError('적용할 수 없는 집계 필터가 있습니다.')
    start = spec['period']['from']
    if spec['comparison']:
        start = prior_month(start[:7]) + '-01'
    started = perf_counter()
    with trend_rows(None, start, constraints=constraints) as (rows, metadata):
        if metadata.get('search_complete') is not True:
            raise LookupError('검증된 전체 저장본을 확인하지 못했습니다.')
        result = aggregate_rows(rows, spec, constraints)
    result.update(source=dict(metadata), filter_audit=audit, elapsed_ms=round((perf_counter() - started) * 1000, 2))
    return result
