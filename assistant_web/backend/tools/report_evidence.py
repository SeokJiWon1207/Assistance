"""기준 이슈와 비교/초동 참고 사례를 일반 답변의 동일 조건 근거로 섞지 않는다."""
from copy import deepcopy


def role_contract(contract, role: str) -> dict | None:
    kind = (contract.report or {}).get('kind')
    ids = contract.conditions.constraints.get('issue_ids') or []
    if kind == 'similar' and len(ids) == 1:
        if role == 'seed':
            return {'role': role, 'grade': 'scoped', 'issue_ids': list(ids)}
        if role == 'candidate':
            return {'role': role, 'grade': 'reference', 'excluded_ids': list(ids), 'max_issue_id_exclusive': ids[0],
                    'filters': [deepcopy(f) for f in contract.filters if f['field'] != 'issue_id'], 'scope': contract.scope}
    if kind == 'triage' and role == 'reference':
        return {'role': role, 'grade': 'reference',
                'filters': [deepcopy(f) for f in contract.filters if f['field'] == 'platform'], 'scope': 'all'}
    return None


def evidence_stages(stage: dict, contract):
    kind = (contract.report or {}).get('kind')
    if kind not in {'similar', 'triage'}:
        return [(stage, contract.conditions.grade, ())]
    rows = stage.get('issues_list') or []
    ids = set(contract.conditions.constraints.get('issue_ids') or [])
    groups = [('seed', [r for r in rows if r.get('id') in ids]),
              ('candidate', [r for r in rows if r.get('id') not in ids])] if kind == 'similar' else [('reference', rows)]
    stages = []
    for role, selected in groups:
        binding = role_contract(contract, role)
        if binding is None:
            raise ValueError('보고서 참고 범위를 확정하지 못했습니다.')
        copied = deepcopy(stage)
        copied['issues_list'] = selected
        copied['meta']['report_evidence'] = binding
        # 원천의 기존 문서/첨부 배열에서 다른 역할의 행이 재진입하지 않는다.
        copied.pop('documents', None)
        stages.append((copied, binding['grade'], ('비교·초동 참고 범위',) if binding['grade'] == 'reference' else ()))
    return stages


def valid_report_evidence(stage, contract) -> bool:
    binding = stage.stage.get('meta', {}).get('report_evidence')
    if not binding:
        return stage.grade == contract.conditions.grade
    expected = role_contract(contract, binding.get('role'))
    if not expected or binding != expected or stage.grade != expected['grade']:
        return False
    ids = [r.get('id') for r in stage.stage.get('issues_list') or []]
    if binding['role'] == 'seed':
        return set(ids).issubset(binding['issue_ids'])
    if binding['role'] == 'candidate':
        return all(type(i) is int and 0 < i < binding['max_issue_id_exclusive'] and i not in binding['excluded_ids'] for i in ids)
    return True
