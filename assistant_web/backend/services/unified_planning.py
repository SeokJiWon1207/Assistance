"""검증된 전체 질의 계획을 기존 읽기 도구와 연결한다."""
from copy import deepcopy

from backend.audit import load_audit_query_plan
from backend.intent.scope_extraction import empty_query_constraints
from backend.intent.unified import plan_question
from backend.services.contracts import ChatPipelineContext


def prepare_context(request, question, user, model, api_key, messages, system_notes):
    previous = {}
    # 클라이언트가 보낸 답변/조건은 신뢰하지 않고 사용자·질문에 묶인 서버 기록만 사용한다.
    if request.contextAuditId and user:
        for message in reversed(request.messages[:-1]):
            if message.role != 'user':
                continue
            previous = load_audit_query_plan(request.contextAuditId, str(user.get('uid') or ''), message.content)
            if previous:
                previous = {**previous, 'audit_id': request.contextAuditId}
                break
    plan = plan_question(question, model, api_key, user, previous)
    if previous and plan.get('operation') == 'explain' and (plan.get('interpretation') or {}).get('followup'):
        # 표시용 질문 문맥도 같은 사용자에게 묶인 서버 기록만 사용한다.
        plan['previous_explanation_questions'] = [
            *(previous.get('previous_explanation_questions') or []), str(previous.get('grounding_text') or ''),
        ][-4:]
    return ChatPipelineContext(
        latest_user_text=question, model=model, api_key=api_key, messages=messages,
        retrieval_text=question, enabled_tools={'mantis': True, 'vectorDb': True, 'notion': True, **request.tools},
        system_notes=system_notes, query_constraints=deepcopy(plan.get('resolved_constraints') or empty_query_constraints()),
        active_limit=max(1, min(request.searchLimit or 5, 20)), intent_plan=plan,
        authenticated_owner=str((user or {}).get('mantisUsername') or (user or {}).get('displayName') or '').strip(),
    )


def clarification(request, reason):
    return {'modelId': request.modelId, 'content': f'조회 조건을 확인해야 합니다. {reason}\n\n조건을 확정하기 전에는 집계하지 않았습니다.',
            'sources': [], 'raw': {'mode': 'unified_plan_clarification'}}


def execute_plan(request, context, user):
    from backend.services import chat_service as service
    plan = context.intent_plan or {}
    operation, source = plan.get('operation'), plan.get('source')
    if operation == 'clarify':
        return clarification(request, plan.get('reason') or '질문을 구체적으로 다시 입력해 주세요.')
    required = ['mantis', 'notion'] if source == 'both' else [source]
    if any(not context.enabled_tools.get(tool) for tool in required if tool != 'general'):
        return clarification(request, '요청한 원천의 조회 도구가 꺼져 있습니다.')
    if source == 'mantis' and operation in {'count', 'aggregate'}:
        return {'modelId': request.modelId, **service.build_mantis_count_report(context.retrieval_text, {**plan, 'operation': 'count'})}
    if operation == 'write':
        result = service.run_notion_write_command(context.latest_user_text, request.messages,
                    allow_write=bool(user and user.get('role') in {'manager', 'admin'}),
                    user_uid=str((user or {}).get('uid') or ''))
        return {'modelId': request.modelId, **result} if result else clarification(request, 'Notion 작업 대상을 확인해 주세요.')
    if operation == 'report':
        kind = plan.get('report_kind')
        if kind in {'handoff', 'postmortem', 'notice'}:
            from backend.reports.issue_documents import build_issue_document_report
            result = build_issue_document_report(context.latest_user_text, context.query_constraints, kind)
            return {'modelId': request.modelId, **result} if result else clarification(request, '문서를 작성할 단일 이슈 번호와 조건을 확인해 주세요.')
        if kind == 'effort_sla':
            from backend.reports.effort_sla import build_effort_sla_report
            result = build_effort_sla_report(context.latest_user_text)
            return {'modelId': request.modelId, **result} if result else clarification(request, '공수·처리시간 조회 조건을 확인해 주세요.')
        builders = {'profile': service.build_customer_profile_report,
                    'monthly': service.build_monthly_customer_report,
                    'weekly': service.build_weekly_report,
                    'similar': service.build_similar_resolution_report}
        if kind == 'similar' and not context.enabled_tools.get('vectorDb'):
            return clarification(request, '유사 사례 검색 도구가 꺼져 있습니다.')
        if kind == 'similar':
            result = service.build_similar_resolution_report(context.latest_user_text, validated_plan=True,
                                                            model=context.model, api_key=context.api_key, topic_terms=plan.get('terms'))
            return {'modelId': request.modelId, **result} if result else clarification(request, '유사 비교의 기준 이슈를 확인해 주세요.')
        if kind == 'triage':
            result = service.build_incident_triage_report(context.latest_user_text,
                        search_similar=bool(context.enabled_tools.get('vectorDb')))
            return {'modelId': request.modelId, **result} if result else clarification(request, '장애 증상을 확인해 주세요.')
        if kind in builders:
            result = builders[kind](context.latest_user_text)
            return {'modelId': request.modelId, **result} if result else clarification(request, '보고서 대상을 확인해 주세요.')
        if plan.get('report_kind') in {'priority', 'aging', 'due', 'comparison', 'completed', 'waiting', 'customers'}:
            from backend.reports.work_queries import build_work_query_report
            result = build_work_query_report(context.latest_user_text, context.query_constraints,
                                             operation=plan['report_kind'], limit=context.active_limit)
            if result:
                return {'modelId': request.modelId, **result}
        # 보고서 계산/수치 검증은 기존 결정형 구현을 유지한다. 건수/설명은 이 경로에 들어오지 않는다.
        report_result = service.build_deterministic_report(context.latest_user_text, context.retrieval_text,
                                                    context.enabled_tools, request.messages, user)
        if report_result.response:
            return {'modelId': request.modelId, **report_result.response}
        return clarification(request, '요청한 보고서의 기간·대상 조건을 확인해 주세요.')
    return None
