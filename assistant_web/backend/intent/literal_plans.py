"""명확한 리터럴 조회와 확정 목록의 단순 변경만 계획한다.

문장 전체가 지원 문법과 일치할 때만 사용한다. 추가 조건/쓰기/본문 요청은
일반 플래너에 맡기고, 생성한 계획도 동일한 근거 검증을 통과해야 한다.
"""
import re

from backend.settings.values import MANTIS_STATUS_NAMES
from backend.customer_identity import customer_alias_owner
from backend.utils.text import normalize_for_match

_REQUEST = r'(?:찾아서\s*알려\s*(?:줘|주세요)|알려\s*줘|보여\s*줘|찾아\s*줘|찾아\s*주세요|알려\s*주세요|보여\s*주세요)[.!?]?'
_LATEST = rf'(?:가장\s*)?최근\s*이슈\s*(?:하나|한\s*개|1개)만?\s*(?:자세히\s*)?{_REQUEST}'
_TITLE = re.compile(
    rf'''(?:노션|Notion)에서\s*(?:상태가\s*(?P<status>[^'"“”]+?)인\s*)?'''
    rf'''(?P<quote>['"])(?P<title>[^'"]+)(?P=quote)\s*(?:(?:문서|항목)?(?:을|를)?\s*)?'''
    rf'(?:좀\s*)?(?:정확히\s*)?{_REQUEST}'
    rf'(?:\s*(?P<excluded>.+?)\s*상태는\s*빼고[.!?]?)?', re.I)


def customer_field_issue(text):
    """번호가 확정된 단일 이슈의 고객사 속성 질문만 인식한다."""
    return re.fullmatch(
        r'(?:(?:맨티스|멘티스|Mantis)\s*)?(?:이슈\s*)?#?(?P<id>\d{3,8})\s*'
        r'(?:(?P<reference>이|그|해당)\s*)?(?:이슈)?(?:는|은|의)?\s*'
        r'(?:(?:어느|어떤)\s*(?:고객사|프로젝트)(?:\s*건)?(?:이야|인가요|야|예요)|'
        r'(?:고객사|프로젝트)(?:는|가|를)?\s*(?:어디(?:야|인가요)|알려\s*(?:줘|주세요)))[?.!]?',
        text.strip(), re.I)


def short_handler_plan(question, lookup, user, stored_list):
    """확정 목록이 있으면 담당자만 바꿔 같은 모집단을 다시 조회한다."""
    from backend.intent.unified_validation import validate_plan

    previous = stored_list.get('plan') or {}
    followup = bool(previous.get('resolved_constraints'))
    if lookup['followup'] and not followup:
        return {'planner': 'unified_llm', 'operation': 'clarify',
                'reason': '이어서 조회할 이전 Mantis 목록을 확인하지 못했습니다. 원래 조회 조건을 함께 입력해 주세요.'}
    owner = lookup['name']
    proposal = _proposal('mantis', followup,
                         [{'field': 'owner', 'operator': 'equals', 'value': owner, 'evidence': owner}],
                         {'mode': 'inherit' if followup else 'unspecified', 'values': [], 'evidence': ''})
    if followup:
        proposal.update(followup_context='list', limit=previous.get('limit'))
    else:
        proposal['scope'] = 'target'
    try:
        plan = validate_plan(question, proposal, user or {}, {'list_context': stored_list} if followup else {})
    except ValueError as exc:
        return {'planner': 'unified_llm', 'operation': 'clarify', 'reason': str(exc)}
    plan['planner_rule'] = 'short_handler_lookup'
    return plan


def literal_plan(question, previous):
    text = question.strip()
    field = customer_field_issue(text)
    if field:
        followup = bool(field['reference'] and previous.get('resolved_constraints'))
        plan = _proposal('mantis', followup,
                         [{'field': 'issue_id', 'operator': 'equals', 'value': field['id'], 'evidence': field['id']}],
                         {'mode': 'inherit' if followup else 'unspecified', 'values': [], 'evidence': ''})
        plan.update(operation='get', scope='inherit' if followup else 'target')
        return plan
    selected = re.fullmatch(
        rf'(?P<id>\d{{3,7}})\s+(?:그\s*중\s*)?(?:첫|두|세|네|다섯|여섯|일곱|여덟|아홉|열|\d+)\s*번째\s*'
        rf'(?:이슈|항목)(?:를|을)?\s*(?:자세히\s*)?{_REQUEST}', text)
    if selected:
        plan = _proposal('mantis', False,
                         [{'field': 'issue_id', 'operator': 'equals', 'value': selected['id'],
                           'evidence': selected['id']}],
                         {'mode': 'unspecified', 'values': [], 'evidence': ''})
        plan.update(operation='get', scope='target')
        return plan
    match = _TITLE.fullmatch(text)
    if match:
        if match['status'] and match['excluded']:
            return None
        filters = [{'field': 'title', 'operator': 'equals', 'value': match['title'],
                    'evidence': match['title']}]
        value = match['status'] or match['excluded']
        if value:
            filters.append({'field': 'status', 'operator': 'not_equals' if match['excluded'] else 'equals',
                            'value': value.strip(), 'evidence': value.strip()})
        return _proposal('notion', False, filters, {'mode': 'unspecified', 'values': [], 'evidence': ''})
    standalone = _standalone_mantis(text)
    if standalone is not None:
        return standalone
    if previous.get('source') != 'mantis' or previous.get('operation') != 'search' or not previous.get('resolved_constraints'):
        return None
    status = {'mode': 'inherit', 'values': [], 'evidence': ''}
    if not re.fullmatch(rf'(?:방금\s*조건에서|같은\s*조건에서|그\s*조건에서)\s*{_LATEST}', text):
        correction = re.fullmatch(
            rf'(?:아니[,，]?\s*)?같은\s*고객사에서\s*(?P<status>.+?)\s*상태인\s*{_LATEST}', text)
        if not correction or not previous['resolved_constraints'].get('customer_key'):
            return None
        values = [key for key, aliases in MANTIS_STATUS_NAMES.items()
                  if normalize_for_match(correction['status']) in {normalize_for_match(a) for a in aliases}]
        if len(values) != 1:
            return None
        status = {'mode': 'include', 'values': values, 'evidence': correction['status']}
    plan = _proposal('mantis', True, [], status)
    plan.update(limit=1, sort={'field': 'created_at', 'direction': 'desc'})
    return plan


def _standalone_mantis(text):
    """고객사가 명시된 완결 문장만 처리하여 제외를 문맥 해제로 오인하지 않는다."""
    from backend.intent.keyword_terms import extract_requested_limit
    from backend.query.dates import extract_created_at_range
    excluded = re.fullmatch(
        rf'(?P<customer>.+?)\s*이슈에서\s*(?P<platform>HTS|MTS|WEB)(?:는|은)\s*빼고\s*'
        rf'최근\s*(?:한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|\d+)\s*개만\s*{_REQUEST}', text, re.I)
    period = re.fullmatch(
        rf'(?P<period>이번\s*주|지난\s*주|이번\s*달|지난\s*달)\s*(?P<customer>.+?)\s*'
        rf'미종료\s*이슈(?:를)?\s*(?:표로\s*)?모두\s*{_REQUEST}', text)
    match = excluded or period
    if match is None:
        return None
    customer = customer_alias_owner(match['customer'])
    if not customer:
        return None
    filters = [{'field': 'customer', 'operator': 'equals', 'value': customer, 'evidence': match['customer']}]
    status = {'mode': 'unspecified', 'values': [], 'evidence': ''}
    if excluded:
        filters.append({'field': 'platform', 'operator': 'not_equals',
                        'value': excluded['platform'].lower(), 'evidence': excluded['platform']})
        limit = extract_requested_limit(text)
        if not limit:
            return None
        plan = _proposal('mantis', False, filters, status)
        plan.update(scope='team', limit=limit, sort={'field': 'created_at', 'direction': 'desc'})
        return plan
    created = extract_created_at_range(match['period'])
    if not created:
        return None
    filters.append({'field': 'created_at', 'operator': 'between',
                    'value': [created['from'], created['to']], 'evidence': match['period']})
    plan = _proposal('mantis', False, filters, {'mode': 'open', 'values': [], 'evidence': '미종료'})
    plan['scope'] = 'team'
    return plan


def _proposal(source, followup, filters, status):
    return {'source': source, 'operation': 'search', 'scope': 'inherit' if followup else 'all',
            'followup': followup, 'filters': filters, 'status': status, 'clear_fields': [],
            'terms': [], 'group_by': None, 'limit': None}
