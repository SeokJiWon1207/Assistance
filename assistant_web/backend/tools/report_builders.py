"""검증된 보고서 계약과 기존 계산기를 연결한다. 모델 호출은 주입된 실행기만 쓴다."""
from copy import deepcopy
from datetime import date

from backend.tools.report_contracts import WORK_REPORTS


def calculate_report(args, context, contract):
    constraints = deepcopy(context.conditions['mantis'].constraints)
    options = contract.report.get('options') or {}
    plan = {'resolved_constraints': constraints, 'effort': args.effort, 'limit': contract.list_limit}
    if args.kind == 'effort':
        from backend.reports.effort import build_effort_report_from_unified_plan
        return build_effort_report_from_unified_plan(context.retrieval_text, plan)
    if args.kind in {'weekly', 'monthly', 'profile'}:
        from backend.reports.weekly_customer import build_weekly_report
        from backend.reports.monthly_customer import build_monthly_customer_report
        from backend.reports.customer import build_customer_profile_report
        return {'weekly': build_weekly_report, 'monthly': build_monthly_customer_report,
                'profile': build_customer_profile_report}[args.kind](context.retrieval_text, constraints=constraints)
    if args.kind == 'sla':
        from backend.reports.sla import build_sla_report
        return build_sla_report(context.retrieval_text, mode='duration', constraints=constraints, group_by=options.get('group_by'))
    if args.kind == 'effort_sla':
        from backend.reports.effort_sla import build_effort_sla_report
        return build_effort_sla_report(context.retrieval_text, plan=plan, group_by=options.get('group_by'))
    if args.kind == 'similar':
        from backend.reports.similar import build_similar_resolution_report
        return build_similar_resolution_report(context.retrieval_text, validated_plan=True, constraints=constraints,
                                               topic_terms=contract.terms, chat=context.report_chat)
    if args.kind == 'triage':
        from backend.reports.incident_triage import build_incident_triage_report
        return build_incident_triage_report(context.retrieval_text, constraints=constraints, topic_terms=contract.terms,
                                            search_similar=bool(context.enabled_sources.get('vectorDb')))
    if args.kind in WORK_REPORTS:
        from backend.reports.work_queries import build_work_query_report
        return build_work_query_report(context.retrieval_text, constraints, operation=args.kind,
                                       limit=contract.list_limit, today=date.fromisoformat(options['as_of']), report_options=options)
    from backend.reports.aggregate import build_mantis_pivot_report, build_mantis_spike_report
    return {'pivot': build_mantis_pivot_report, 'spike': build_mantis_spike_report}[args.kind](
        context.retrieval_text, constraints=constraints, axis=options.get('group_by'))
