"""검색 계획과 실행할 확정 조건의 일치 검사."""
from backend.intent.validation_rules import require, check_effort_task, check_notice_task, check_status, check_query_period, check_aging_period, check_clause


def validate_execution_contract(question, plan):
    """실행 계획의 근거/필터 일치 검사. 의미 정확도 평가는 별도 정답 데이터로 수행한다."""
    checks = []
    try:
        require(plan.get('grounding_text') == question, '질문과 계획 불일치')
        c = plan.get('resolved_constraints') or {}
        require(c.get('unified_plan_validated'), '미검증 계획')
        require(c.get('filter_clauses') == plan.get('filter_clauses'), '실행 조건과 계획 불일치')
        interpretation = plan.get('interpretation') or {}
        from backend.intent.followup_changes import validate_clear_evidence, validate_all_scope_evidence
        proof = plan.get('condition_grounding') or {}
        validate_clear_evidence(question, interpretation.get('clear_fields') or [],
                                interpretation.get('clear_evidence') or {}, proof.get('previous_clauses') or [],
                                comparison_end=interpretation.get('comparison_end', False))
        require(all(clause in c.get('filter_clauses', []) for clause in proof.get('preserved_clauses', [])),
                '상속할 기존 조건이 실행에서 누락됐습니다')
        if (interpretation.get('followup') and c.get('mantis_team_scope') == 'all'
                and proof.get('previous_scope') != 'all' and not (c.get('people') or {}).get('names')
                and not c.get('issue_ids')):
            validate_all_scope_evidence(question, interpretation.get('scope_evidence') or question)
        check_effort_task(question, plan.get('report_kind'), interpretation.get('followup'))
        check_notice_task(question, plan.get('source'), plan.get('operation'), plan.get('report_kind'))
        check_status(question, interpretation.get('status'), interpretation.get('followup'), plan.get('source'), plan.get('operation'), plan.get('report_kind'))
        if plan.get('aggregation'):
            from backend.intent.aggregation_spec import check_shape
            check_shape(plan['aggregation'])
            require(plan.get('source') == 'mantis' and plan.get('operation') == 'aggregate', '집계 작업 불일치')
        else:
            check_query_period(question, plan.get('source'), plan.get('operation'), c.get('filter_clauses') or [], plan.get('report_kind'))
        check_aging_period(question, plan.get('source'), plan.get('operation'), c.get('filter_clauses') or [], plan.get('report_kind'))
        for clause in interpretation.get('evidence') or []:
            check_clause(clause, question, plan.get('source'))
        if plan.get('source') == 'notion':
            from backend.intent.customer_constraints import check_customer_scope
            check_customer_scope(question, c.get('filter_clauses') or [], interpretation.get('evidence') or [])
        require(not interpretation.get('followup') or bool(c.get('inherited_from_audit_id')), '이전 기록 근거 누락')
        checks.append({'id': 'unified_contract', 'label': '단일 계획의 근거·실행 조건 일치', 'status': 'pass',
                       'detail': '스키마·원문 근거·확정 조건 일치 검사이며 의미 정답 보증은 아님'})
    except (ValueError, TypeError, KeyError) as exc:
        checks.append({'id': 'unified_contract', 'label': '단일 계획 계약', 'status': 'fail', 'detail': str(exc)})
    return {'valid': all(c['status'] == 'pass' for c in checks), 'checks': checks}
