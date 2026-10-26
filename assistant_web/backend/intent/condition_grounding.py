"""서버가 검증한 조건의 출처를 실행 값에 결속한다. 검색 조건 자체는 바꾸지 않는다."""
from copy import deepcopy
import re
from typing import Any

from backend.common import normalize_for_match
from backend.query_filters import values_for_clause


def build_condition_grounding(question, constraints, incoming, status, scope, user, previous):
    clauses = constraints['filter_clauses']
    current = [clause for clause in clauses if clause in incoming]
    if status['mode'] != 'inherit':
        current.extend(clause for clause in clauses if clause['field'] == 'status')
    current.extend(clause for clause in clauses if clause['field'] == 'keyword'
                   and all(value in question for value in values_for_clause(clause.get('value'))))
    owner = str(user.get('mantisUsername') or user.get('displayName') or '').strip()
    authenticated = ({'uid': str(user.get('uid') or ''), 'owner': owner}
                     if scope == 'self' and owner else {})
    audit_id = str(previous.get('audit_id') or '')
    inherited = []
    if (user.get('uid') and re.fullmatch(r'[0-9a-f]{32}', audit_id)
            and constraints.get('inherited_from_audit_id') == audit_id):
        inherited = [clause for clause in clauses
                     if clause in (previous.get('resolved_constraints') or {}).get('filter_clauses', [])
                     and clause not in current]
    return {'question': question, 'question_clauses': deepcopy(current),
            'authenticated': authenticated, 'audit_id': audit_id,
            'inherited_clauses': deepcopy(inherited)}


def grounded_values(question: str, constraints: dict[str, Any], plan: dict[str, Any] | None):
    """현재 실행과 일치하는 개별 조건만 인증·감사·원문 근거로 인정한다."""
    from backend.intent.unified_validation import validate_execution_contract
    if not plan or plan.get('planner') != 'unified_llm':
        return set()
    proof = plan.get('condition_grounding')
    if (not isinstance(proof, dict) or proof.get('question') != question
            or constraints.get('filter_clauses') != plan.get('filter_clauses')
            or not validate_execution_contract(question, plan)['valid']):
        return set()
    certified = []
    for clause in constraints.get('filter_clauses', []):
        if clause in proof.get('question_clauses', []):
            certified.append(clause)
        auth = proof.get('authenticated') or {}
        if (auth.get('uid') and (plan.get('interpretation') or {}).get('scope') == 'self'
                and clause in [{'field': field, 'operator': 'equals', 'value': auth.get('owner')}
                               for field in ('owner', 'effort_person')]):
            certified.append(clause)
        audit_id = str(proof.get('audit_id') or '')
        if (re.fullmatch(r'[0-9a-f]{32}', audit_id)
                and audit_id == constraints.get('inherited_from_audit_id')
                and clause in proof.get('inherited_clauses', [])):
            certified.append(clause)
    return {(clause['field'], normalize_for_match(
                str(int(value.lstrip('#'))) if clause['field'] == 'issue_id' and value.lstrip('#').isdigit()
                else value.lstrip('#')))
            for clause in certified for value in values_for_clause(clause.get('value'))}
