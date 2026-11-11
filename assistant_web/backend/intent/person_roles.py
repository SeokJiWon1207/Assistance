"""같은 구절에서 확인할 수 있는 이름과 Mantis 역할의 근거."""

import re
from typing import Any

from backend.domain.people import is_valid_person_name
from backend.intent.entities import extract_people_constraints
from backend.intent.people import plausible_explicit_person_name
from backend.intent.validation_rules import require, unquoted
from backend.query_filters import values_for_clause
from backend.utils.text import normalize_for_match


_LABEL_FIELDS = {'담당자': 'owner', '요청자': 'reporter', '등록자': 'registered_reporter'}
_PERSON_FILTER_FIELDS = frozenset(_LABEL_FIELDS.values())
_ROLE_LABEL = re.compile(
    r'(?<!\w)(?P<label>담당자|요청자|등록자)(?:가|는)?(?:\s*[:=]\s*|\s+)'
    r'(?P<name>[가-힣]{2,5}|[A-Za-z][A-Za-z0-9_.-]{1,31})(?=$|[\s,?!])'
)
_SUBJECT_PARTICLE = r'(?:께서|이|가|은|는)'
_PERSON_SUBJECT = re.compile(
    r'(?<!\w)(?P<name>[가-힣]{2,4}?)'
    r'(?:\s*(?P<title>(?:선임|차장|과장|대리|주임|수석|매니저|팀장|부장|사원)(?:님)?|님)'
    rf'{_SUBJECT_PARTICLE}?|{_SUBJECT_PARTICLE})(?=\s)'
)
_ROLE_ACTION_END = r'(?:한|하신|하고|하며|했고|했으며)'
_PERSON_ACTION = re.compile(
    rf'(?<![가-힣])(?:(?P<reporter>(?:요청|문의){_ROLE_ACTION_END})'
    rf'|(?P<registered_reporter>등록{_ROLE_ACTION_END}|요청자로\s*등록)'
    rf'|(?P<owner>담당(?:하는|했던|{_ROLE_ACTION_END})|맡(?:은|았던|았고)))(?=$|[\s,.!?])'
)
_CLAUSE_END = re.compile(r'[,.;!?\n]|(?<!\w)(?:그리고|또는|담당자(?:가|는)|등록자(?:가|는)|요청자(?:가|는))\s')


def _explicit_person_roles(text: str, names: set[str]) -> list[dict[str, str]]:
    """질문 전체의 역할 별칭 대신 명시적인 호칭 또는 행위 구절을 읽는다."""
    roles: dict[tuple[str, str], dict[str, str]] = {}
    for match in _ROLE_LABEL.finditer(text):
        name = match['name']
        # 실제 이름이 확인되면 보존하고 역할명 뒤의 소유격·서술격만 분리한다.
        if name not in names and name.endswith(('의', '인')):
            name = name[:-1]
        if name in names and plausible_explicit_person_name(name):
            field = _LABEL_FIELDS[match['label']]
            roles[normalize_for_match(name), field] = {
                'name': name, 'field': field, 'evidence': match.group(),
            }

    subjects = [
        match for match in _PERSON_SUBJECT.finditer(text)
        if (match['title'] or match['name'] in names or match.group() in names)
        and plausible_explicit_person_name(match['name']) and is_valid_person_name(match['name'])
    ]
    for index, subject in enumerate(subjects):
        next_subject = subjects[index + 1].start() if index + 1 < len(subjects) else len(text)
        tail = _CLAUSE_END.split(text[subject.end():min(next_subject, subject.end() + 100)], maxsplit=1)[0]
        for action in _PERSON_ACTION.finditer(tail):
            action_field = action.lastgroup
            if action_field is not None:
                name = subject['name']
                roles[normalize_for_match(name), action_field] = {
                    'name': name, 'field': action_field, 'evidence': text[subject.start():subject.end() + action.end()],
                }
    return list(roles.values())


def person_planning_hints(question: str) -> dict[str, Any]:
    text = unquoted(question)
    people = extract_people_constraints(text)
    names = people['names']
    recognized = {name for name, origin in people['name_origins'].items() if origin == 'known'}
    recognized.update(candidate['name'] for candidate in people['candidates'] if candidate['origin'] == 'inferred')
    roles = _explicit_person_roles(text, recognized)
    # 역할 표현이 확실하면 명부에 없는 이름 후보도 모델에 전달한다.
    return {
        'mentioned_people': list(dict.fromkeys([*names, *(role['name'] for role in roles)])),
        'person_role_hints': roles,
    }


def check_person_roles(text: str, incoming: list[dict], applied: list[dict]) -> None:
    """새 인물 조건을 확인하고 명시한 담당자가 누락되지 않게 한다."""
    proposed = [clause for clause in incoming if clause['field'] in _PERSON_FILTER_FIELDS]
    explicit_handler = bool(re.search(r'담당자|담당\s*[:=]', text))
    if not (proposed or explicit_handler):
        return
    people = extract_people_constraints(text)
    names = people['names']
    proposed_names = {name for clause in proposed for name in values_for_clause(clause.get('value'))}
    roles: dict[str, set[str]] = {}
    for hint in _explicit_person_roles(text, set(names) | proposed_names):
        roles.setdefault(normalize_for_match(hint['name']), set()).add(hint['field'])

    for clause in proposed:
        for name in values_for_clause(clause.get('value')):
            fields = roles.get(normalize_for_match(name), set())
            if len(fields) == 1 and clause['field'] not in fields:
                expected = next(iter(fields))
                raise ValueError(f'인물 역할 불일치: {name}의 원문 역할은 {expected}입니다. '
                                 f'{clause["field"]} 대신 {expected} 필터를 사용하세요.')

    if explicit_handler and people.get('role') == 'handler':
        expected_names = {name for name in names if 'owner' in roles.get(normalize_for_match(name), {'owner'})}
        applied_names = {name for clause in applied if clause['field'] == 'owner'
                         for name in values_for_clause(clause.get('value'))}
        require(expected_names <= applied_names,
                '명시된 담당자 이름이 필터에서 누락됐습니다: ' + ', '.join(sorted(expected_names)))
