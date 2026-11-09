"""여러 조회 범위의 원문 근거와 공통 조건 계약. 실행 권한은 각 단일 계획이 검증한다."""
from copy import deepcopy
from datetime import date
import re

from backend.customer_identity import canonical_customer, customer_mentions
from backend.intent.validation_rules import RELATIVE_CREATED_AT_EVIDENCE, require
from backend.query.dates import extract_created_at_range, extract_yearless_month_range


_PERIOD = re.compile(
    r'(?:이번\s*(?:달|주|월)|지난\s*(?:달|주)|오늘|어제|올해|작년|'
    r'최근\s*\d{1,3}\s*(?:일|주|개월|달)|'
    r'(?:20\d{2}\s*년\s*)?\d{1,2}\s*월|20\d{2}\s*년|'
    r'20\d{2}-\d{2}-\d{2}\s*(?:~|부터|–)\s*20\d{2}-\d{2}-\d{2}(?:까지)?)')


def comparison_scopes(question: str, proposal: dict) -> list[dict]:
    """모델이 제안한 값만 검사한다. 비교 범위를 서버의 추측으로 추가하거나 삭제하지 않는다."""
    spec = proposal.get('comparison')
    if not isinstance(spec, dict) or set(spec) != {'axis', 'cohorts'}:
        raise ValueError('비교 axis와 cohorts가 필요합니다')
    axis, cohorts = spec['axis'], spec['cohorts']
    require(axis in {'customer', 'created_at'}, '고객사 또는 등록 기간 한 축으로 비교하세요')
    require(isinstance(cohorts, list) and 2 <= len(cohorts) <= 4, '비교 범위는 2~4개가 필요합니다')
    require(proposal.get('source') == 'mantis' and proposal.get('operation') in {'count', 'search', 'aggregate'},
            '비교는 Mantis 건수·목록·이슈 건수 집계만 지원합니다')
    require(not proposal.get('followup'), '비교 범위와 공통 조건을 현재 질문에 명시해 주세요')
    require(not any(c.get('field') == axis for c in proposal.get('filters') or []),
            '비교 축의 조건은 공통 filters가 아니라 cohorts에 지정하세요')
    agg = proposal.get('aggregation')
    require(not agg, '독립 범위 비교에 aggregation 명세를 함께 사용할 수 없습니다. '
            '단순 건수 비교는 changes={"aggregation":null,"operation":"count","group_by":null}로 보정하세요. '
            '그룹별 비교는 aggregation=null, operation=aggregate, 최상위 group_by=요청 그룹(예: status)입니다. '
            '고객사·미해결·기간 조건과 comparison은 보존하세요.')
    require(proposal.get('operation') != 'aggregate' or proposal.get('group_by'),
            '범위별 그룹 집계는 최상위 group_by가 필요합니다. 건수만 비교하면 changes={"operation":"count"}로 보정하세요.')
    scopes: list[dict] = []
    for index, cohort in enumerate(cohorts):
        require(isinstance(cohort, dict) and set(cohort) == {'value', 'evidence'}, '비교 범위의 value/evidence를 확인하세요')
        value, evidence = cohort['value'], cohort['evidence']
        require(isinstance(evidence, str) and evidence.strip() == evidence and evidence and evidence in question,
                '비교 범위에 현재 질문의 정확한 원문 근거가 필요합니다')
        if axis == 'customer':
            mentions = customer_mentions(evidence)
            require(isinstance(value, str) and len(mentions) == 1
                    and mentions[0][:2] == (0, len(evidence)) and canonical_customer(value) == mentions[0][2],
                    '비교 고객사와 원문 근거가 다릅니다')
            value = canonical_customer(value)
            label = evidence
        else:
            require(bool(_PERIOD.fullmatch(evidence)), '등록 기간 근거는 달력 기간 표현만 포함해야 합니다')
            period = extract_created_at_range(evidence) or extract_yearless_month_range(evidence)
            if not isinstance(value, list) or len(value) != 2 or period is None:
                raise ValueError('비교 기간의 날짜 쌍이 필요합니다')
            ends = {period['to']}
            if RELATIVE_CREATED_AT_EVIDENCE.fullmatch(evidence):
                ends.add(min(period['to'], date.today().isoformat()))
            require(value[0] == period['from'] and value[1] in ends,
                    f"'{evidence}' 비교 기간은 {period['from']}~{period['to']}입니다")
            label = f'{evidence} ({value[0]} ~ {value[1]})'
        require(not any(s['clause']['value'] == value for s in scopes), '동일한 비교 범위를 중복 지정할 수 없습니다')
        scopes.append({'id': f'S{index + 1}', 'label': label, 'evidence': evidence,
                       'clause': {'field': axis, 'operator': 'equals' if axis == 'customer' else 'between',
                                  'value': deepcopy(value), 'evidence': evidence}})
    mentioned = {m[2] for m in customer_mentions(question)}
    if axis == 'customer':
        require(mentioned == {s['clause']['value'] for s in scopes}, '질문에 명시한 비교 고객사가 누락되거나 추가됐습니다')
    else:
        residual = question
        for scope in scopes:
            residual = residual.replace(scope['evidence'], ' ')
        require(not extract_created_at_range(residual) and not extract_yearless_month_range(residual),
                '질문에 명시한 비교 기간이 누락됐습니다')
        shared = {canonical_customer(v) for c in proposal.get('filters') or [] if c.get('field') == 'customer'
                  and c.get('operator') in {'equals', 'in'}
                  for v in (c['value'] if isinstance(c.get('value'), list) else [c.get('value')])}
        require(mentioned == shared, '기간 비교의 공통 고객사 조건을 보존하세요')
    for scope in scopes:
        branch_question = question
        if axis == 'created_at':
            for other in scopes:
                if other is not scope:
                    branch_question = branch_question.replace(other['evidence'], ' ')
        scope['question'] = branch_question
    return scopes


def common_constraints(child: dict, axis: str) -> dict:
    common = deepcopy(child['resolved_constraints'])
    common['filter_clauses'] = [c for c in common['filter_clauses'] if c['field'] != axis]
    if axis == 'customer':
        common.update(customer_key=None, customer_aliases=[])
    else:
        common['created_at'] = None
    return common


def check_comparison_contract(question: str, plan: dict, validate_child) -> None:
    require(plan.get('operation') == 'compare' and plan.get('source') == 'mantis'
            and plan.get('grounding_text') == question, '비교 실행 계획과 질문이 다릅니다')
    proposal = plan.get('comparison_proposal') or {}
    expected = comparison_scopes(question, proposal)
    actual = plan.get('query_scopes') or []
    require(len(actual) == len(expected), '비교 범위가 누락됐습니다')
    common = None
    for saved, scope in zip(actual, expected, strict=True):
        require(all(saved.get(k) == scope[k] for k in ('id', 'label', 'question')), '비교 범위의 근거가 변경됐습니다')
        child = saved.get('plan') or {}
        require(not child.get('query_scopes') and child.get('operation') == proposal['operation'], '비교 하위 계획 불일치')
        checked = validate_child(scope['question'], child)
        require(checked['valid'], checked['checks'][0]['detail'])
        clause = {k: v for k, v in scope['clause'].items() if k != 'evidence'}
        require([c for c in child.get('filter_clauses', []) if c['field'] == clause['field']] == [clause],
                '비교 축의 실행 조건이 다릅니다')
        current = common_constraints(child, proposal['comparison']['axis'])
        require(common is None or current == common, '비교 범위 사이의 공통 조건이 다릅니다')
        common = current
        require(all(child.get(k) == actual[0]['plan'].get(k) for k in ('sort', 'limit', 'terms', 'group_by', 'aggregation')),
                '비교 범위 사이의 정렬·집계·검색어가 다릅니다')
    require(plan.get('resolved_constraints') == common and plan.get('filter_clauses') == (common or {}).get('filter_clauses'),
            '비교의 공통 실행 조건이 다릅니다')
