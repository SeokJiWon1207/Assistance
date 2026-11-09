"""합성 답변도 각 부분의 기존 조건·수치 검증을 모두 수행한다."""
from datetime import datetime
from typing import Callable


PART_MODES = frozenset({'agent_query_results', 'agent_aggregation_results', 'agent_comparison_results',
    'agent_notion_results', 'agent_model', 'agent_issue_document', 'agent_evidence_only', 'agent_no_evidence',
    'agent_complete_empty', 'agent_ordinal_title', 'explanation_evidence_incomplete',
    'deterministic_effort_report', 'deterministic_filtered_effort_report', 'deterministic_effort_ranking',
    'deterministic_weekly_report', 'deterministic_monthly_customer_report', 'deterministic_effort_trend',
    'deterministic_customer_profile', 'deterministic_mantis_sla', 'deterministic_effort_sla_report',
    'deterministic_similar_resolution', 'deterministic_incident_triage', 'deterministic_work_report',
    'deterministic_mantis_pivot', 'deterministic_mantis_spike'})


def verify_composed_response(question: str, content: str, sources: list, retrieval: dict, *,
                             verify_part: Callable[..., dict], add_dimension: Callable[..., dict]) -> dict:
    parts = retrieval.get('answer_parts') or []
    valid = bool(isinstance(parts, list) and 1 <= len(parts) <= 8 and all(
        isinstance(p, dict) and p.get('mode') in PART_MODES and isinstance(p.get('content'), str)
        and isinstance(p.get('sources'), list) and not (p.get('retrieval') or {}).get('answer_parts') for p in parts))
    footer = retrieval.get('answer_footer') or ''
    expected = '\n\n'.join([p['content'].rstrip() for p in parts] + ([footer] if footer else [])) if valid else None
    report: dict = {'mode': 'agent_composed', 'checks': [], 'dimensions': [], 'queryPlan': retrieval.get('query_plan'),
              'constraints': retrieval.get('constraints') or {}, 'sourceCount': len(sources), 'trace': [],
              'verifiedAt': datetime.now().astimezone().isoformat(), 'note': '부분별 기존 검증과 최종 표시를 함께 대조합니다.'}
    if valid:
        for index, part in enumerate(parts):
            kwargs = {}
            extra = []
            plan = (part.get('retrieval') or {}).get('query_plan') or {}
            if plan.get('source') == 'notion' and part['mode'] in {'agent_model', 'agent_evidence_only', 'agent_no_evidence'}:
                from backend.intent.execution_contract import validate_execution_contract
                from backend.notion.filter_conditions import merge_common_notion_constraints
                contract = validate_execution_contract(question, plan)
                if contract['valid']:
                    kwargs['_notion_conditions'] = merge_common_notion_constraints({}, plan.get('resolved_constraints') or {})
                extra.append({'id': 'agent_notion_contract', 'label': 'Notion 실행 조건 근거',
                              'status': 'pass' if contract['valid'] else 'fail', 'detail': '설명도 검증된 조건으로 원문 속성을 대조'})
            verified = verify_part(question, part['content'], part['sources'], part.get('retrieval'), part['mode'], **kwargs)
            report = add_dimension(report, f'answer_part_{index}', f'응답 부분 {index + 1}',
                                   verified['checks'] + part.get('checks', []) + extra, part['mode'])
    return add_dimension(report, 'composition', '응답 조합', [{
        'id': 'agent_response_composition', 'label': '검증 결과와 최종 응답 일치',
        'status': 'pass' if valid and content == expected else 'fail',
        'detail': '각 부분의 원래 검증을 실행하고 추가·누락·변조된 표시 내용을 차단'}], '검증되지 않은 응답 합성을 차단합니다.')
