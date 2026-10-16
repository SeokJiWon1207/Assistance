"""전체 질의 계획의 계약 검증과 확정 조건 변환. 질문을 다시 해석해 덮어쓰지 않는다."""
from copy import deepcopy
from datetime import date
import re

from backend.config import CUSTOMER_ALIASES, PLATFORM_ALIASES, MANTIS_STATUS_NAMES, MANTIS_OPEN_STATUSES
from backend.intent.scope_extraction import empty_query_constraints
from backend.query_filters import normalize_filter_clause, clause_supported, SUPPORTED_FIELDS, values_for_clause
from backend.team_scope import TEAM_MEMBERS, TEAM_SCOPE_LABEL

STATUS_ALL = re.compile(r'전체\s*상태|모든\s*상태|상태.{0,8}(?:무관|관계없|상관없|(?:제한|조건).{0,5}(?:없|풀|해제))|종료.{0,12}포함')
PENDING = re.compile(r'(?:이슈|업무|티켓).{0,20}남(?:았|아)|남(?:은|아\s*있는).{0,20}(?:이슈|업무|티켓)|미종료|미완료|안\s*끝난|못\s*끝낸')
PERSON_FIELDS = {'owner', 'reporter', 'registered_reporter'}
STATUS_VALUES = set(MANTIS_STATUS_NAMES)
REPORT_KINDS = {'effort', 'weekly', 'sla', 'pivot', 'spike', 'customers', 'priority', 'aging', 'due', 'comparison', 'completed', 'waiting', 'similar'}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def unquoted(question):
    return re.sub(r'"[^"]*"|\'[^\']*\'|“[^”]*”|‘[^’]*’', ' ', question)


def check_status(question, status, followup, source, operation=None):
    require(isinstance(status, dict), '상태 계획이 없습니다')
    mode, values, evidence = status.get('mode'), status.get('values'), status.get('evidence')
    require(mode in {'unspecified', 'all', 'open', 'include', 'exclude', 'inherit', 'unclear'}, '지원하지 않는 상태 의도')
    require(isinstance(values, list) and all(isinstance(v, str) and v in STATUS_VALUES for v in values), '지원하지 않는 상태값')
    require(isinstance(evidence, str) and (mode == 'inherit' or not evidence or evidence_is_grounded(evidence, question)), '상태 근거 불일치')
    require(mode != 'unclear', '조회할 상태를 확정하지 못했습니다')
    require(mode != 'inherit' or followup, '이전 상태가 없습니다')
    if source not in {'mantis', 'both'}:
        require(mode in {'unspecified', 'inherit'}, 'Mantis 상태를 다른 원천에 적용할 수 없습니다')
        return
    text = unquoted(question)
    if mode == 'all':
        require(bool(STATUS_ALL.search(evidence)), '모두/전부는 전체 상태를 뜻하지 않습니다')
    if mode in {'open', 'include', 'exclude'}:
        require(evidence_is_grounded(evidence, text), '상태 근거가 없거나 인용된 검색어입니다')
    if mode in {'include', 'exclude'}:
        require(bool(values), '포함/제외 상태가 없습니다')
    if mode == 'open':
        require(not values or set(values) == MANTIS_OPEN_STATUSES, '미종료 상태 정의 불일치')
    if mode in {'all', 'unspecified', 'inherit'}:
        require(not values or (mode == 'all' and set(values) == STATUS_VALUES), '상태 의도와 값이 모순됩니다')
    if operation != 'explain' and mode == 'unspecified' and PENDING.search(text) and not STATUS_ALL.search(text):
        require(False, '미종료 표현을 상태 조건 없이 집계할 수 없습니다')


def evidence_is_grounded(evidence, question):
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    # 모델이 중간 단어를 생략한 발췌도 원문의 같은 순서에 있는 단어만 허용한다.
    parts = [part for part in re.split(r'\s+|\.{3,}|…+', evidence) if part]
    return bool(parts and re.search(r'.*?'.join(re.escape(part) for part in parts), question, re.S))


def normalize_status_clauses(parsed):
    parsed = deepcopy(parsed)
    raw = parsed.get('filters')
    if not isinstance(raw, list) or parsed.get('source') not in {'mantis', 'both'}:
        return parsed
    items = [f for f in raw if isinstance(f, dict) and f.get('field') == 'status']
    if not items:
        return parsed
    require(len(items) == 1, '중복 상태 계획')
    item = items[0]
    op = item.get('operator')
    require(op in {'equals', 'in', 'not_equals', 'not_in'}, '상태 연산자 오류')
    values = values_for_clause(item.get('value'))
    converted = {'mode': 'exclude' if op in {'not_equals', 'not_in'} else 'include',
                 'values': values, 'evidence': item.get('evidence')}
    status = parsed.get('status') or {}
    require(isinstance(status, dict), '상태 계획 형식 오류')
    require(status.get('mode') == 'unspecified' or
            (status.get('mode') == converted['mode'] and set(status.get('values', [])) == set(values)), '서로 다른 상태 계획 충돌')
    parsed['status'] = converted
    parsed['filters'] = [f for f in raw if f not in items]
    return parsed


def check_clause(item, question, source):
    require(isinstance(item, dict), '필터 형식 오류')
    evidence = item.get('evidence')
    require(evidence_is_grounded(evidence, question), '필터 원문 근거 불일치')
    clause = normalize_filter_clause(item)
    if clause is None:
        raise ValueError('허용되지 않은 필터')
    field = clause['field']
    require(field != 'status' or source == 'notion', 'Mantis 상태는 상태 계획에 지정해야 합니다')
    families = ['mantis', 'notion'] if source == 'both' else [source]
    require(all(clause_supported(f, clause) for f in families), '원천이 지원하지 않는 필터')
    values = values_for_clause(clause.get('value'))
    if field in PERSON_FIELDS | {'title', 'keyword', 'screen_number', 'issue_id', 'database'}:
        require(all(str(v).lstrip('#') in question for v in values), '질문에 없는 필터값')
    if field == 'issue_id':
        require(all(v.lstrip('#').isdigit() for v in values), '이슈 번호 형식 오류')
    if field in {'customer', 'platform'}:
        aliases = CUSTOMER_ALIASES if field == 'customer' else PLATFORM_ALIASES
        for value in values:
            forms = [value, *aliases.get(value, [])]
            require(any(form.casefold() in question.casefold() for form in forms), '고객사/매체 근거 불일치')
    if field in {'created_at', 'due_at', 'deployed_at'}:
        try:
            dates = [date.fromisoformat(v) for v in values]
        except ValueError as exc:
            raise ValueError('날짜 형식 오류') from exc
        require(len(dates) in {1, 2} and (len(dates) == 1 or dates[0] <= dates[1]), '날짜 범위 오류')
    return clause


def constraints_from_clauses(clauses, status, scope, user, previous):
    c = empty_query_constraints()
    c.update(filter_clauses=clauses, excluded_statuses=[], excluded_platform_keys=[], excluded_platform_aliases=[],
             excluded_customer_keys=[], excluded_customer_aliases=[], exclude_issue_ids=[])
    for item in clauses:
        field, operator, value = item['field'], item['operator'], item.get('value')
        values = values_for_clause(value)
        included = operator in {'equals', 'in'}
        if field in PERSON_FIELDS and included:
            # 담당자/요청자 조건은 각각 보존하고 people은 대표 표시에도 사용한다.
            if not c['people']['names']:
                c['people'] = {'names': values, 'role': {'owner': 'handler'}.get(field, field),
                               'name_origins': {v: 'explicit_unverified' for v in values}, 'candidates': []}
        elif field == 'owner' and operator == 'not_exists':
            c['unassigned_people'] = True
        elif field == 'issue_id':
            if included:
                c['issue_ids'] = [int(v.lstrip('#')) for v in values]
            elif operator in {'not_equals', 'not_in'}:
                c['exclude_issue_ids'] = [int(v.lstrip('#')) for v in values]
        elif field == 'screen_number' and included:
            c['screen_numbers'] = values
        elif field == 'created_at' and operator == 'between':
            c['created_at'] = {'from': values[0], 'to': values[1]}
        elif field in {'customer', 'platform'} and included and len(values) == 1:
            catalog = CUSTOMER_ALIASES if field == 'customer' else PLATFORM_ALIASES
            canonical = next((key for key, aliases in catalog.items() if values[0].casefold() in {v.casefold() for v in [key, *aliases]}), None)
            if canonical:
                c[f'{field}_key'] = canonical
                c[f'{field}_aliases'] = catalog[canonical]
    mode = status['mode']
    if mode == 'inherit':
        c['statuses'] = list(previous.get('statuses') or [])
        c['excluded_statuses'] = list(previous.get('excluded_statuses') or [])
    elif mode == 'open':
        c['statuses'] = ['open']
    elif mode == 'include':
        c['statuses'] = list(status['values'])
    elif mode == 'exclude':
        c['excluded_statuses'] = list(status['values'])
    for op, values in [('in', c['statuses']), ('not_in', c['excluded_statuses'])]:
        if values:
            c['filter_clauses'].append({'field': 'status', 'operator': op, 'value': values})
    if scope == 'self':
        name = str(user.get('mantisUsername') or user.get('displayName') or '').strip()
        require(bool(name), '개인 이슈의 담당자명이 등록되어 있지 않습니다')
        require(not c['people']['names'] or c['people']['names'] == [name], '개인 범위와 다른 담당자가 충돌합니다')
        c['people'] = {'names': [name], 'role': 'handler', 'name_origins': {name: 'known'}, 'candidates': []}
        c['filter_clauses'].append({'field': 'owner', 'operator': 'equals', 'value': name})
    if scope == 'inherit':
        scope = previous.get('mantis_team_scope', 'default')
    if scope == 'default':
        scope = 'target' if c['people']['names'] or c['issue_ids'] or c.get('unassigned_people') else 'team'
    if scope == 'team':
        c.update(mantis_team_members=list(TEAM_MEMBERS), mantis_team_label=TEAM_SCOPE_LABEL)
    c['mantis_team_scope'] = 'target' if scope == 'self' else scope
    c['filter_clauses'] = list({(f['field'], f['operator'], str(f.get('value'))): f for f in c['filter_clauses']}.values())
    return c


def validate_plan(question, parsed, user, previous):
    require(isinstance(parsed, dict), '계획 JSON 형식 오류')
    parsed = normalize_status_clauses(parsed)
    required = {'source', 'operation', 'scope', 'followup', 'filters', 'status', 'clear_fields', 'terms', 'group_by', 'limit'}
    require(required <= parsed.keys(), '필수 계획 항목 누락')
    source, operation, scope = (parsed.get(k) for k in ('source', 'operation', 'scope'))
    require(source in {'mantis', 'notion', 'both', 'general'}, '잘못된 원천')
    require(operation in {'count', 'search', 'get', 'explain', 'aggregate', 'report', 'write', 'clarify'}, '잘못된 작업')
    require(operation != 'clarify', str(parsed.get('reason') or '질문 조건을 확정하지 못했습니다'))
    require(operation != 'write' or source == 'notion', '지원하지 않는 쓰기 원천')
    require(scope in {'self', 'team', 'all', 'target', 'default', 'inherit'}, '잘못된 범위')
    followup = parsed['followup']
    require(isinstance(followup, bool), '후속 질문 형식 오류')
    base = deepcopy(previous.get('resolved_constraints') or {}) if followup else {}
    require(not followup or bool(base), '이어서 조회할 확정 검색 기록이 없습니다')
    require(scope != 'inherit' or followup, '상속할 범위가 없습니다')
    if followup:
        require(previous.get('source') == source, '후속 조회의 원천이 바뀌었습니다')
    status = parsed['status']
    check_status(question, status, followup, source, operation)
    clear = parsed['clear_fields']
    require(isinstance(clear, list) and all(f in SUPPORTED_FIELDS | {'terms', 'team'} for f in clear), '해제 조건 오류')
    require(not clear or followup, '해제할 이전 조건이 없습니다')
    raw = parsed['filters']
    require(isinstance(raw, list) and len(raw) <= 20, '필터 개수 오류')
    incoming = []
    grounded_raw = []
    for item in raw:
        # 이전 확정 조건을 모델이 반복해 출력해도 새 조건으로 취급하지 않는다.
        # 값/필드/연산자가 서버 기록과 모두 같은 경우만 생략하며 새 값은 원문 근거가 필요하다.
        normalized = normalize_filter_clause(item)
        if (followup and normalized and normalized in base.get('filter_clauses', [])
                and normalized['field'] not in clear and not evidence_is_grounded(item.get('evidence'), question)):
            continue
        incoming.append(check_clause(item, question, source))
        grounded_raw.append(item)
    raw = grounded_raw
    replaced = {f['field'] for f in incoming} | set(clear) | {'status'}
    if replaced & PERSON_FIELDS:
        replaced |= PERSON_FIELDS
    clauses = [f for f in base.get('filter_clauses', []) if f['field'] not in replaced]
    clauses.extend(incoming)
    if scope == 'self':
        clauses = [f for f in clauses if f['field'] not in PERSON_FIELDS]
    if 'team' in clear:
        scope = 'all'
    # 포함과 제외를 AND로 합쳐 빈 집합을 만드는 계획을 실행하지 않는다.
    for a in clauses:
        for b in clauses:
            if a['field'] == b['field'] and a['operator'] in {'equals', 'in', 'contains'} and b['operator'] in {'not_equals', 'not_in', 'not_contains'}:
                require(not set(values_for_clause(a.get('value'))) & set(values_for_clause(b.get('value'))), '동일 값의 포함/제외 조건이 충돌합니다')
    if operation == 'explain' and scope == 'default':
        scope = 'all'
    effective_scope = scope if source in {'mantis', 'both'} or scope == 'self' else 'all'
    scoped_user = {**user, 'mantisUsername': user.get('displayName')} if source == 'notion' else user
    c = constraints_from_clauses(clauses, status, effective_scope, scoped_user, base)
    if followup:
        c['inherited_from_audit_id'] = previous.get('audit_id') or 'confirmed_previous_plan'
        c['list_scope_edit'] = True
    terms = parsed['terms']
    require(isinstance(terms, list) and len(terms) <= 6 and all(isinstance(t, str) and 0 < len(t) <= 100 for t in terms), '검색어 형식 오류')
    if followup and not terms and 'terms' not in clear and not (replaced & {'keyword', 'title'}):
        terms = list(previous.get('terms') or [])
    if operation in {'count', 'aggregate'}:
        for term in terms:
            require(term in question or (followup and term in (previous.get('terms') or [])), '집계 검색어의 원문 근거 누락')
            require(not any(f['field'] in {'keyword', 'title'} and f['operator'] in {'not_contains', 'not_equals', 'not_in'}
                            and term in values_for_clause(f.get('value')) for f in c['filter_clauses']), '집계 검색어의 포함/제외 충돌')
            clause = {'field': 'keyword', 'operator': 'contains', 'value': term}
            if clause not in c['filter_clauses']:
                c['filter_clauses'].append(clause)
    group = parsed['group_by']
    require(group in {None, 'customer', 'platform', 'status', 'owner', 'month'}, '그룹 조건 오류')
    limit = parsed['limit']
    require(limit is None or (type(limit) is int and 1 <= limit <= 200), '표시 개수 오류')
    require(operation != 'count' or limit is None, '전체 집계에 표시 개수를 적용할 수 없습니다')
    sort = parsed.get('sort') or {'field': 'created_at', 'direction': 'desc'}
    require(isinstance(sort, dict) and sort.get('field') in {'created_at', 'updated_at', 'issue_id'} and sort.get('direction') in {'asc', 'desc'}, '정렬 형식 오류')
    report = parsed.get('report_kind')
    require(operation != 'report' or report in REPORT_KINDS, '지원하지 않는 보고서')
    c['unified_plan_validated'] = True
    return {'version': 3, 'planner': 'unified_llm', 'source': source, 'operation': operation,
            'grounding_text': question, 'planned': True, 'confidence': 1.0, 'reason': '전체 질문의 단일 검증 계획',
            'filters': {'customer': c.get('customer_key'), 'platform': c.get('platform_key'), 'people': c['people'],
                        'statuses': c['statuses'], 'issue_ids': c['issue_ids'], 'screen_numbers': c['screen_numbers'], 'created_at': c['created_at']},
            'filter_clauses': deepcopy(c['filter_clauses']), 'resolved_constraints': c, 'terms': terms,
            'group_by': group, 'limit': limit, 'sort': sort, 'report_kind': report,
            'interpretation': {'status': deepcopy(status), 'followup': followup, 'scope': scope,
                               'evidence': deepcopy(raw), 'clear_fields': clear}}


def validate_execution_contract(question, plan):
    """실행 계획의 근거/필터 일치 검사. 의미 정확도 평가는 별도 정답 데이터로 수행한다."""
    checks = []
    try:
        require(plan.get('grounding_text') == question, '질문과 계획 불일치')
        c = plan.get('resolved_constraints') or {}
        require(c.get('unified_plan_validated'), '미검증 계획')
        require(c.get('filter_clauses') == plan.get('filter_clauses'), '실행 조건과 계획 불일치')
        interpretation = plan.get('interpretation') or {}
        check_status(question, interpretation.get('status'), interpretation.get('followup'), plan.get('source'), plan.get('operation'))
        for clause in interpretation.get('evidence') or []:
            check_clause(clause, question, plan.get('source'))
        require(not interpretation.get('followup') or bool(c.get('inherited_from_audit_id')), '이전 기록 근거 누락')
        checks.append({'id': 'unified_contract', 'label': '단일 계획의 근거·실행 조건 일치', 'status': 'pass',
                       'detail': '스키마·원문 근거·확정 조건 일치 검사이며 의미 정답 보증은 아님'})
    except (ValueError, TypeError, KeyError) as exc:
        checks.append({'id': 'unified_contract', 'label': '단일 계획 계약', 'status': 'fail', 'detail': str(exc)})
    return {'valid': all(c['status'] == 'pass' for c in checks), 'checks': checks}
