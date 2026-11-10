"""목록의 모집단과 잠시 확인하는 상세 대상을 구분하는 서버 소유 대화 상태."""
from copy import deepcopy


PLAN_FIELDS = ('source', 'operation', 'report_kind', 'effort', 'aggregation', 'sort', 'limit',
               'resolved_constraints', 'terms', 'group_by', 'grounding_text', 'query_scopes')
LIST_OPERATIONS = {'search', 'count', 'aggregate'}


def list_context(previous: dict, *, audit_id: str, question: str, variant: str) -> dict:
    """인증한 감사 계획에서만 호출한다. 상세 선택 ID를 목록 필터에 합치지 않는다."""
    constraints = previous.get('resolved_constraints') or {}
    if (previous.get('source') in {'mantis', 'both'} and not previous.get('query_scopes')
            and (previous.get('operation') in LIST_OPERATIONS
                 or previous.get('operation') == 'explain' and previous.get('limit')
                 and not constraints.get('issue_ids'))):
        return {'audit_id': audit_id, 'question': question, 'variant': variant,
                'plan': deepcopy({key: previous[key] for key in PLAN_FIELDS if key in previous})}
    return deepcopy(previous.get('list_context') or {})


def followup_previous(proposal: dict, previous: dict) -> tuple[dict, str]:
    """모델이 선택한 맥락을 기존 조건 검증의 기준으로 사용한다. 새 필터를 만들지 않는다."""
    stored = previous.get('list_context') or {}
    default = 'list' if stored and proposal.get('followup') and proposal.get('operation') in LIST_OPERATIONS else 'current'
    basis = proposal.get('followup_context', default)
    if basis not in {'current', 'list'}:
        raise ValueError('followup_context는 current(상세 대상) 또는 list(이전 목록 조건)입니다')
    if not proposal.get('followup'):
        if basis == 'list':
            raise ValueError('목록 조건 상속은 followup=true인 후속 질문에서만 가능합니다')
        return previous, basis
    # 최초 목록 직후에는 previous 자체가 목록 계획이다. 모델의 list 선택을 그 계획의 상속으로 받는다.
    if basis == 'list' and not stored and list_context(previous, audit_id='', question='', variant=''):
        basis = 'current'
    if basis == 'current':
        if stored and proposal.get('operation') in LIST_OPERATIONS:
            raise ValueError('건수·목록·집계 후속은 followup_context=list로 목록 조건을 사용하세요. 상세 선택 ID는 목록 조건이 아닙니다')
        return previous, basis
    plan = deepcopy(stored.get('plan') or {})
    if not plan.get('resolved_constraints') or not stored.get('audit_id'):
        raise ValueError('상속할 서버 목록 기록이 없습니다. 임의의 목록 조건을 만들 수 없습니다')
    # 복합 답변의 Mantis 목록을 다시 묻는 것은 원천 조건의 해제가 아니다.
    if plan.get('source') == 'both' and proposal.get('source') == 'mantis':
        plan['source'] = 'mantis'
    plan['audit_id'] = stored['audit_id']
    return plan, basis


def preserve_list_context(plan: dict, stored: dict, *, continuing: bool) -> None:
    """상세 계획에는 목록 조건을 분리 저장한다. 독립 질문은 연결을 끊는다."""
    if (continuing and stored and plan.get('operation') in {'get', 'explain'}
            and (plan.get('resolved_constraints') or {}).get('issue_ids')):
        plan['list_context'] = deepcopy(stored)
