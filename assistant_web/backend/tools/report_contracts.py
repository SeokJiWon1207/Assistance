"""보고서의 계산 의미·날짜·결측·참고 범위를 서버 계약으로 고정한다."""
from copy import deepcopy
from datetime import date, timedelta

WORK_REPORTS = frozenset({'priority', 'aging', 'due', 'comparison', 'completed', 'waiting', 'customers'})
REPORT_KINDS = frozenset({'effort', 'weekly', 'monthly', 'profile', 'sla', 'effort_sla',
                          'similar', 'triage', 'pivot', 'spike', *WORK_REPORTS})
DUE_FIELDS = ('고객사전달 목표일', '개발완료 목표일', 'QC 완료 목표일', '완료예정일',
              '수정예정일', '처리예정일', '마감일', '배포예정일')


def report_contract(question: str, plan: dict) -> dict | None:
    kind = plan.get('report_kind')
    if plan.get('operation') != 'report' or kind not in REPORT_KINDS:
        return None
    result = {'kind': kind, 'effort': deepcopy(plan.get('effort'))}
    if kind in {'effort', 'weekly', 'monthly'}:
        return result
    from backend.query.operations import extract_group_by
    today = date.today()
    options: dict = {'as_of': today.isoformat(), 'group_by': plan.get('group_by') or extract_group_by(question),
                     'missing_dates': 'exclude_and_report', 'population': 'confirmed_filters'}
    if kind in {'pivot', 'spike'}:
        from backend.reports.aggregate import _detect_pivot_axis, _spike_axis
        detected = _detect_pivot_axis(question) if kind == 'pivot' else _spike_axis(question)
        allowed = {'customer', 'owner', 'screen', 'status'} if kind == 'pivot' else {'customer', 'owner', 'screen'}
        axis = plan.get('group_by') or (detected[0] if detected else None)
        if axis not in allowed:
            raise ValueError('이 보고서에서 지원하는 집계 축을 확인해 주세요.')
        options['group_by'] = axis
    if kind in {'sla', 'effort_sla'} and options['group_by'] not in {None, 'customer', 'owner'}:
        raise ValueError('처리시간 보고서는 고객사별·담당자별 집계를 지원합니다.')
    if kind in {'sla', 'effort_sla', 'profile'}:
        options.update(duration_basis='created_at_to_updated_at_approximation',
                       duration_statuses=['resolved', 'closed'], invalid_duration='exclude_negative_or_missing')
    if kind in {'effort_sla'}:
        options.update(effort_basis='delivered_at', missing_delivery='exclude_from_effort')
    if kind in {'aging', 'profile'}:
        from backend.reports.sla import _aging_threshold_days
        options.update(aging_basis='as_of_minus_created_at', aging_days=_aging_threshold_days(question) if kind == 'aging' else 30)
    if kind in {'due', 'priority'}:
        options.update(due_basis='first_valid_target_date', due_fields=list(DUE_FIELDS), missing_due='unknown_not_zero')
    if kind == 'due':
        from backend.query.dates import extract_created_at_range, extract_relative_created_at_range
        # 기한 전용 조회의 기간이다. 별도로 지정한 등록 기간은 필터에서 제거하지 않는다.
        c = plan.get('resolved_constraints') or {}
        date_text = question
        for clause in (plan.get('interpretation') or {}).get('evidence') or []:
            if clause.get('field') == 'created_at' and clause.get('evidence'):
                date_text = date_text.replace(clause['evidence'], '')
        options['due_period'] = (deepcopy(c.get('due_at')) or extract_relative_created_at_range(date_text, today)
                                 or extract_created_at_range(date_text)
                                 or {'from': today.isoformat(), 'to': (today + timedelta(days=6-today.weekday())).isoformat()})
    if kind in {'completed', 'comparison', 'waiting'}:
        options['missing_history'] = 'unavailable_do_not_infer_from_current_status'
    if kind == 'customers':
        options['default_created_period'] = ({'from': (today-timedelta(days=29)).isoformat(), 'to': today.isoformat()}
                                             if not (plan.get('resolved_constraints') or {}).get('created_at') else None)
    if kind == 'similar':
        options.update(seed_scope='confirmed_issue_id', candidate_scope='confirmed_filters_without_seed_id',
                       candidate_order='issue_id_less_than_seed', coverage='top_k_not_exhaustive')
    if kind == 'triage':
        options.update(reference_scope='platform_only', reference_grade='reference', coverage='top_k_not_exhaustive')
    if kind in {'pivot', 'spike'}:
        options.update(date_basis='created_at_month', missing_month='exclude_from_time_series')
    result['options'] = options
    return result
