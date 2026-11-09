"""독립 범위를 기존 단일 조건 검증기에 각각 통과시킨다."""
from copy import deepcopy

from backend.intent.comparison_contract import common_constraints, comparison_scopes
from backend.intent.unified_validation import validate_plan


def validate_comparison_plan(question: str, proposal: dict, user: dict) -> dict:
    scopes = comparison_scopes(question, proposal)
    children = []
    for scope in scopes:
        branch = {k: deepcopy(v) for k, v in proposal.items() if k != 'comparison'}
        branch['filters'] = [*branch.get('filters', []), scope['clause']]
        child = validate_plan(scope['question'], branch, user, {})
        children.append({k: scope[k] for k in ('id', 'label', 'question')} | {'plan': child})
    common = common_constraints(children[0]['plan'], proposal['comparison']['axis'])
    return {**deepcopy(children[0]['plan']), 'operation': 'compare', 'grounding_text': question,
            'resolved_constraints': common, 'filter_clauses': deepcopy(common['filter_clauses']),
            'query_scopes': children, 'comparison_proposal': deepcopy(proposal)}
