"""명확한 리터럴 조회와 확정 목록의 단순 변경만 계획한다.

문장 전체가 지원 문법과 일치할 때만 사용한다. 추가 조건/쓰기/본문 요청은
일반 플래너에 맡기고, 생성한 계획도 동일한 근거 검증을 통과해야 한다.
"""
import re

from backend.config import MANTIS_STATUS_NAMES
from backend.common import normalize_for_match

_REQUEST = r'(?:알려\s*줘|보여\s*줘|찾아\s*줘|찾아\s*주세요|알려\s*주세요|보여\s*주세요)[.!?]?'
_LATEST = rf'(?:가장\s*)?최근\s*이슈\s*(?:하나|한\s*개|1개)만?\s*(?:자세히\s*)?{_REQUEST}'
_TITLE = re.compile(
    rf'''(?:노션|Notion)에서\s*(?:상태가\s*(?P<status>[^'"“”]+?)인\s*)?'''
    rf'''(?P<quote>['"])(?P<title>[^'"]+)(?P=quote)\s*(?:(?:문서|항목)?(?:을|를)?\s*)?'''
    rf'(?:좀\s*)?(?:정확히\s*)?{_REQUEST}'
    rf'(?:\s*(?P<excluded>.+?)\s*상태는\s*빼고[.!?]?)?', re.I)


def literal_plan(question, previous):
    text = question.strip()
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


def _proposal(source, followup, filters, status):
    return {'source': source, 'operation': 'search', 'scope': 'inherit' if followup else 'all',
            'followup': followup, 'filters': filters, 'status': status, 'clear_fields': [],
            'terms': [], 'group_by': None, 'limit': None}
