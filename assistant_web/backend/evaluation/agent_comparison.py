"""최종 비교의 모든 범위에 기존 조건·건수·집계 검증을 적용한다."""
from datetime import datetime

from backend.evaluation.agent_queries import verify_agent_aggregation, verify_query_results
from backend.intent.execution_contract import validate_execution_contract
from backend.intent.display_contract import expected_display_limit
from backend.services.agent.query_comparison import render_comparison, scope_stages, scope_text


def verify_agent_comparison(question: str, content: str, retrieval: dict, mode: str) -> dict:
    plan = retrieval.get('query_plan') or retrieval.get('intent_plan') or {}
    stages = retrieval.get('stages') or []
    checks = validate_execution_contract(question, plan)['checks']
    valid = all(c['status'] == 'pass' for c in checks)
    try:
        expected, _, result = render_comparison(plan, stages)
        valid = valid and content == expected and result == retrieval.get('comparison_result')
        known = {s['id'] for s in plan['query_scopes']}
        valid = valid and all(s.get('meta', {}).get('condition_step', {}).get('query_scope_id') in known for s in stages)
        timestamps: set[str] = set()
        for scope in plan['query_scopes']:
            scoped = scope_stages(stages, scope['id'])
            rendered, _ = scope_text(scope, scoped)
            queries = [s for s in scoped if s.get('meta', {}).get('query_result')]
            complete_sets = [set(s['meta'].get('matched_issue_ids') or []) for s in queries if s['meta'].get('search_complete')]
            if complete_sets and any(ids != complete_sets[0] for ids in complete_sets):
                valid = False
            timestamps.update(s['meta']['fetchedAt'] for s in queries if s['meta'].get('fetchedAt'))
            check = verify_query_results(rendered, scoped, expected_display_limit(question, scope['plan'])) if queries else {
                'status': 'warning', 'detail': '조회 미실행 · 0건으로 확정하지 않음'}
            checks.append({**check, 'id': 'agent_query_result', 'label': scope['label'] + ' 건수·목록'})
            if any(s.get('meta', {}).get('aggregation_result') for s in scoped):
                for stage in scoped:
                    entry = stage.get('meta', {}).get('aggregation_result')
                    if entry:
                        valid = valid and bool(complete_sets and set(entry.get('matched_issue_ids') or []) == complete_sets[0])
                        if entry.get('snapshot_as_of'):
                            timestamps.add(entry['snapshot_as_of'])
                agg = verify_agent_aggregation(scope['question'], rendered,
                    {'stages': scoped, 'query_plan': scope['plan'], 'constraints': scope['plan']['resolved_constraints']}, mode)
                checks.extend({**c, 'label': scope['label'] + ' ' + c['label']} for c in agg['checks'])
            child = validate_execution_contract(scope['question'], scope['plan'])
            valid = valid and child['valid']
        valid = valid and len(timestamps) <= 1
        if not result['complete']:
            checks.append({'id': 'comparison_complete', 'label': '모든 비교 범위 조회', 'status': 'warning',
                           'detail': '미완료 범위가 있어 차이·합계 미확정'})
    except (ValueError, TypeError, KeyError, IndexError):
        valid = False
    checks.append({'id': 'agent_comparison_result', 'label': '비교 범위·차이·합계 대조',
                   'status': 'pass' if valid else 'fail', 'detail': '범위별 원천 ID 집합·저장 시각·서버 계산·표시 내용 대조'})
    status = 'fail' if any(c['status'] == 'fail' for c in checks) else 'warning' if any(c['status'] == 'warning' for c in checks) else 'pass'
    score = round(100 * sum(c['status'] == 'pass' for c in checks) / len(checks))
    return {'status': status, 'score': score, 'checks': checks, 'completedDimensions': 1, 'applicableDimensions': 1,
            'dimensions': [{'id': 'comparison', 'label': '범위 비교', 'status': status, 'score': score, 'checks': checks}],
            'constraints': retrieval.get('constraints') or {}, 'queryPlan': plan, 'trace': stages,
            'sourceCount': 0, 'mode': mode, 'verifiedAt': datetime.now().astimezone().isoformat()}
