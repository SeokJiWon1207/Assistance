"""집계 표와 모델 설명을 별도로 평가한다. 모델 문장을 결정형으로 간주하지 않는다."""
from datetime import datetime

from backend.answers import display_constraints
from backend.intent.unified_validation import validate_execution_contract
from backend.reports.aggregation_narrative import build_facts, validate_claim


def verify_aggregation(question, content, retrieval, mode):
    plan = retrieval.get('query_plan') or {}
    result = retrieval.get('aggregation_result') or {}
    source = result.get('source') or {}
    narrative = retrieval.get('aggregation_narrative') or {}
    checks = validate_execution_contract(question, plan)['checks']
    checks.append({'id': 'aggregation_source', 'label': '전체 저장본',
                   'status': 'pass' if source.get('search_complete') is True else 'fail',
                   'detail': '검증된 전체 저장본 기준' if source.get('search_complete') else '전체 모집단을 확인하지 못함'})
    totals = result.get('totals') or {}
    sums_match = bool(totals) and all(abs(sum(row['values'][m] for row in result['rows']) - value) < 0.0001
                                     for m, value in totals.items())
    checks.append({'id': 'aggregation_totals', 'label': '표 합계', 'status': 'pass' if sums_match else 'fail',
                   'detail': '상위 그룹과 기타의 합계 대조이며 원천 의미 정답 보증은 아님'})
    status = narrative.get('status')
    if status == 'accepted' and result:
        catalog = {f['id']: f for f in build_facts(result)}
        claims = narrative.get('claims') or []
        valid = bool(claims) and all(validate_claim(c, catalog) and c['text'] in content for c in claims)
        explanation_status = 'pass' if valid else 'fail'
        detail = '사실 ID·숫자·부호·기간·그룹 대조 및 모델 의미 검토 통과' if valid else '설명 사실 연결 불일치'
    elif status == 'not_requested':
        explanation_status, detail = 'pass', '설명 요청이 없어 서버 계산 표만 응답'
    else:
        explanation_status, detail = 'warning', '설명을 채택하지 않고 서버 계산 표로 대체'
    checks.append({'id': 'aggregation_narrative', 'label': '집계 설명 검증',
                   'status': explanation_status, 'detail': detail})
    status = 'fail' if any(c['status'] == 'fail' for c in checks) else 'warning' if explanation_status == 'warning' else 'pass'
    score = round(100 * sum(c['status'] == 'pass' for c in checks) / len(checks))
    return {'score': score, 'status': status, 'checks': checks,
            'dimensions': [{'id': 'aggregation', 'label': '집계와 설명', 'score': score, 'status': status, 'checks': checks}],
            'completedDimensions': 1, 'applicableDimensions': 1,
            'constraints': display_constraints(plan.get('resolved_constraints') or {}),
            'queryPlan': plan, 'trace': retrieval.get('stages', []), 'sourceCount': 0, 'mode': mode,
            'verifiedAt': datetime.now().astimezone().isoformat()}
