"""질의 계획이 공유하는 근거·상태·기간 검증 규칙."""
from copy import deepcopy
from datetime import date, timedelta
import re
from backend.settings.catalogs import get_customer_aliases
from backend.settings.values import PLATFORM_ALIASES, STATUS_ALIASES, MANTIS_STATUS_NAMES, MANTIS_OPEN_STATUSES
from backend.query_filters import normalize_filter_clause, clause_supported, values_for_clause
from backend.query.dates import extract_relative_created_at_range, question_has_temporal_expression


STATUS_ALL = re.compile(r'상태(?:를|는|도)?\s*(?:모두|전부)\s*포함|전체\s*상태|상태\s*전체|모든\s*상태|상태\s*구분\s*없이|상태.{0,8}(?:무관|관계없|상관없|(?:제한|조건).{0,5}(?:없|풀|해제))|종료.{0,12}포함|종료\s*여부(?:와|에)?\s*(?:관계|상관)\s*없이')


PENDING = re.compile(r'(?:이슈|업무|티켓).{0,20}남(?:았|아)|남(?:은|아\s*있는).{0,20}(?:이슈|업무|티켓)|미종료|미완료|안\s*끝난|못\s*끝낸')


PERSON_FIELDS = {'owner', 'reporter', 'registered_reporter', 'effort_person'}


STATUS_VALUES = set(MANTIS_STATUS_NAMES)


_STATUS_LABEL = '(?:' + '|'.join(
    re.escape(alias).replace(r'\ ', r'\s*')
    for alias in sorted({alias for names in [*STATUS_ALIASES.values(), *MANTIS_STATUS_NAMES.values()]
                         for alias in names}, key=len, reverse=True)
) + ')'


EXPLICIT_STATUS_REQUEST = re.compile(
    rf'상태\s*(?:가|는|를|만|=|:)?\s*{_STATUS_LABEL}'
    rf'|{_STATUS_LABEL}\s*(?:상태|이슈|업무|티켓|건|항목)', re.I)


# 근거가 상대 등록일 표현 하나로만 된 경우에만 서버 달력 계산과 대조한다.
# '지난달 15일 이후', '지난주 월요일'처럼 세부 조건이 붙으면 전체 기간과 다르므로 대조하지 않는다.
# '30일 넘은' 같은 기준일 이전 조건은 상한만 있어 제외한다.
RELATIVE_CREATED_AT_EVIDENCE = re.compile(
    r'\s*(?:등록일\s*(?:이|은|기준)?\s*)?'
    r'(?P<expr>최근\s*\d{1,3}\s*(?:일|개월|달|주)|지난\s*달|저번\s*달|전\s+달|전월|이번\s*달|이달|금월|당월'
    r'|지난\s*주|저번\s*주|전\s*주|이번\s*주|금주|그제|그저께|엊그제|어제|전일|오늘|금일)'
    r'(?:\s*(?:에|의|간|동안|사이|중|이내|안에|기준|기간|등록일|(?:에\s*)?(?:등록|생성|접수)(?:된|한|됐던|되었던)?'
    r'|(?:에\s*)?(?:올라온|들어온)))*\s*[.?!]?\s*')


REPORT_KINDS = {'effort', 'weekly', 'sla', 'pivot', 'spike', 'customers', 'priority', 'aging', 'due', 'comparison', 'completed', 'waiting', 'similar', 'profile', 'monthly', 'triage', 'effort_sla', 'handoff', 'postmortem', 'notice'}


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def unquoted(question):
    return re.sub(r'"[^"]*"|\'[^\']*\'|“[^”]*”|‘[^’]*’', ' ', question)


def check_status(question, status, followup, source, operation=None, report_kind=None):
    require(isinstance(status, dict), '상태 계획이 없습니다')
    mode, values, evidence = status.get('mode'), status.get('values'), status.get('evidence')
    require(mode in {'unspecified', 'all', 'open', 'include', 'exclude', 'inherit', 'unclear'}, '지원하지 않는 상태 의도')
    # status 계획은 Mantis 상태 전용이다. Notion 상태값('보관됨' 등)으로 거부만 하면 모델이 조건을 버린다.
    require(source in {'mantis', 'both'} or mode in {'unspecified', 'inherit'},
            'status 계획은 Mantis 전용입니다. Notion 상태는 status를 unspecified로 두고 '
            'filters에 {field: status, operator: equals 또는 not_equals, value, evidence}로 지정하세요')
    require(isinstance(values, list) and all(isinstance(v, str) and v in STATUS_VALUES for v in values), '지원하지 않는 상태값')
    require(isinstance(evidence, str) and (mode == 'inherit' or not evidence or evidence_is_grounded(evidence, question)), '상태 근거 불일치')
    require(mode != 'unclear', '조회할 상태를 확정하지 못했습니다')
    require(mode != 'inherit' or followup, '이전 상태가 없습니다')
    change = status.get('change', 'remove' if followup and mode == 'exclude' else 'replace')
    require(change in {'replace', 'add', 'remove'}, '지원하지 않는 상태 변경')
    require(change == 'replace' or followup, '상태 추가/제외에는 이전 조건이 필요합니다')
    require(change != 'add' or mode == 'include', '상태 추가는 include로 지정해야 합니다')
    require(change != 'remove' or mode == 'exclude', '상태 제외는 exclude로 지정해야 합니다')
    require(not followup or mode != 'exclude' or change == 'remove',
            '후속 상태 제외는 change=remove로 기존 범위에서 제거해야 합니다. 상태 전체 교체는 include+replace에 원하는 최종 상태 집합을 명시하세요')
    if source not in {'mantis', 'both'}:
        return
    text = unquoted(question)
    if operation == 'report' and report_kind == 'weekly':
        text = re.sub(r'신규\s*이슈\s*[,·]\s*미종료\s*이슈\s*[,·]\s*장기\s*적체', ' ', text)
    if mode == 'all':
        require(evidence_is_grounded(evidence, text) and bool(STATUS_ALL.search(text)),
                '모두/전부는 전체 상태를 뜻하지 않습니다')
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
    if (operation in {'search', 'count', 'aggregate'} and mode == 'unspecified'
            and EXPLICIT_STATUS_REQUEST.search(text) and not STATUS_ALL.search(text)):
        from backend.intent.entities import extract_status_constraints
        explicit = extract_status_constraints(text)
        require(not explicit, '명시된 상태 조건이 누락됐습니다: ' + ', '.join(explicit))


def check_compound_issue_list(question, plan):
    """에이전트가 설명 중 집계할 이슈에도 동일한 건수·목록 조건 검사를 적용한다."""
    from backend.intent.keyword_terms import extract_requested_limit
    from backend.query.operations import has_count_intent

    if plan.get('source') not in {'mantis', 'both'} or plan.get('operation') != 'explain':
        return
    # 이슈 조건 뒤에 붙인 문서의 기간과 상태는 이슈 목록의 필터가 아니다.
    prefix = re.split(r'notion|노션', question, maxsplit=1, flags=re.I)[0]
    list_question = prefix if has_count_intent(prefix) or extract_requested_limit(prefix) else question
    if not (has_count_intent(list_question) or extract_requested_limit(list_question)):
        return
    interpretation = plan['interpretation']
    check_status(list_question, interpretation['status'], interpretation['followup'], 'mantis', 'search')
    check_query_period(list_question, 'mantis', 'search', plan['filter_clauses'])


def evidence_is_grounded(evidence, question):
    if not isinstance(evidence, str) or not evidence.strip():
        return False
    # 모델이 중간 단어를 생략한 발췌도 원문의 같은 순서에 있는 단어만 허용한다.
    parts = [part for part in re.split(r'\s+|\.{3,}|…+', evidence) if part]
    return bool(parts and re.search(r'.*?'.join(re.escape(part) for part in parts), question, re.S))


def effective_statuses(included, excluded):
    """미종료 별칭까지 펼쳐 포함/제외가 뜻하는 실제 상태 범위를 비교한다."""
    def expand(values):
        require(all(v in STATUS_VALUES | {'open'} for v in values), '지원하지 않는 상태값')
        return set().union(*(MANTIS_OPEN_STATUSES if v == 'open' else {v} for v in values))
    return (expand(included) if included else set(STATUS_VALUES)) - expand(excluded)


def declared_statuses(status):
    """명시 상태 계획의 유효한 범위만 비교한다. 잘못된 값은 동치로 인정하지 않는다."""
    mode, values = status.get('mode'), status.get('values')
    require(isinstance(values, list) and all(isinstance(v, str) and v in STATUS_VALUES for v in values),
            '지원하지 않는 상태값')
    if mode == 'open':
        require(not values or set(values) == MANTIS_OPEN_STATUSES, '미종료 상태 정의 불일치')
        return set(MANTIS_OPEN_STATUSES)
    if mode in {'include', 'exclude'}:
        require(bool(values), '포함/제외 상태가 없습니다')
        return effective_statuses(values if mode == 'include' else [], values if mode == 'exclude' else [])
    return None


def normalize_status_clauses(parsed, previous=None, question=''):
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
    clause_range = effective_statuses(values if converted['mode'] == 'include' else [],
                                      values if converted['mode'] == 'exclude' else [])
    # 같은 사용자에게 묶인 이전 실행 조건을 확보한 뒤에만 반복 상태를 상속으로 인정한다.
    if (previous and status.get('mode') == 'inherit' and status.get('values') == [] and values
            and isinstance(parsed.get('clear_fields'), list) and 'status' not in parsed['clear_fields']
            and clause_range ==
                effective_statuses(previous.get('statuses') or [], previous.get('excluded_statuses') or [])):
        parsed['filters'] = [f for f in raw if f not in items]
        return parsed
    if values and declared_statuses(status) == clause_range:
        # open과 여섯 상태의 in / 해결·종료의 not_in은 같은 조건이다.
        # 현재 상태 요청이 없고 이전 확정 범위까지 같을 때만 이전 근거를 상속한다.
        from backend.intent.entities import extract_status_constraints
        text = unquoted(question)
        if (previous and parsed.get('followup') is True
                and isinstance(parsed.get('clear_fields'), list) and 'status' not in parsed['clear_fields']
                and clause_range == effective_statuses(previous.get('statuses') or [], previous.get('excluded_statuses') or [])
                and isinstance(status.get('evidence'), str)
                and not evidence_is_grounded(status['evidence'], text)
                and not (PENDING.search(text) or STATUS_ALL.search(text) or EXPLICIT_STATUS_REQUEST.search(text)
                         or re.search(rf'{_STATUS_LABEL}\s*(?:만|도|으로|로)', text)
                         or extract_status_constraints(text))):
            parsed['status'] = {'mode': 'inherit', 'values': [], 'evidence': ''}
        parsed['filters'] = [f for f in raw if f not in items]
        return parsed
    if status.get('mode') == 'inherit' and status.get('values') == [] and previous:
        from backend.intent.entities import extract_status_constraints
        evidence = converted.get('evidence')
        explicit = extract_status_constraints(unquoted(evidence or ''))
        # 상속 선언은 새 상태와 모순되는 명시 조건이 아니다. 현재 발화가 실제로
        # 지칭한 상태값만 최상위 상태 계획으로 옮긴다. 단순 '보여줘'는 근거가 아니다.
        if (evidence_is_grounded(evidence, unquoted(question)) and explicit
                and set(values) <= effective_statuses(explicit, [])):
            parsed['status'] = converted
            parsed['filters'] = [f for f in raw if f not in items]
            return parsed
    require(status.get('mode') == 'unspecified' or
            (status.get('mode') == converted['mode'] and set(status.get('values', [])) == set(values)), '서로 다른 상태 계획 충돌')
    parsed['status'] = converted
    parsed['filters'] = [f for f in raw if f not in items]
    return parsed


ORDERING_WORDS = {'가장', '제일', '최근', '최신', '마지막', '이슈', '건', '항목', '문서', '목록',
                  '하나', '하나만', '한', '두', '세', '개', '만'}


def is_ordering_phrase(text) -> bool:
    """'가장 최근 이슈', '최신 3건'처럼 정렬·개수 단어만으로 된 문구다. 검색 주제가 아니다."""
    words = re.findall(r'[가-힣A-Za-z0-9]+', str(text))
    return bool(words) and all(w in ORDERING_WORDS or re.fullmatch(r'\d+(?:건|개)', w) for w in words)


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
    hint = '. 기준 번호는 equals/in으로 지정하고 유사 비교의 과거 범위는 보고서에 맡기세요' if field == 'issue_id' else ''
    require(all(clause_supported(f, clause) for f in families),
            f"원천이 지원하지 않는 필터: {field} {clause['operator']}" + hint)
    values = values_for_clause(clause.get('value'))
    require(field != 'keyword' or not any(is_ordering_phrase(v) for v in values),
            '정렬·개수 표현은 키워드 조건이 아닙니다. sort·limit으로 표현하세요')
    if field == 'status':
        # 근거 문구가 질문에 있어도 포함·제외 방향은 보장되지 않는다. 기존 Notion 상태 파서가
        # 같은 상태값을 반대 방향으로 읽은 경우만 거부하고, 파서가 모르는 표현은 그대로 둔다.
        from backend.intent.notion_constraints import extract_notion_constraints
        native = extract_notion_constraints(unquoted(question))
        included = {re.sub(r'\s+', '', str(native.get('status') or ''))} - {''}
        excluded = {re.sub(r'\s+', '', str(v)) for v in native.get('excluded_statuses') or []}
        negative = clause['operator'] in {'not_equals', 'not_in'}
        opposite, same = (included, excluded) if negative else (excluded, included)
        for value in (re.sub(r'\s+', '', str(v)) for v in values):
            require(value not in opposite or value in same,
                    f"질문은 상태 '{value}'을(를) {'포함' if negative else '제외'} 조건으로 말합니다. "
                    f"{'equals/in' if negative else 'not_equals/not_in'}을 사용하세요")
    if field in PERSON_FIELDS | {'title', 'keyword', 'screen_number', 'issue_id', 'database'}:
        require(all(str(v).lstrip('#') in question for v in values), '질문에 없는 필터값')
    if field == 'issue_id':
        require(all(v.lstrip('#').isdigit() for v in values), '이슈 번호 형식 오류')
    if field in {'customer', 'platform'}:
        if field == 'customer':
            from backend.customer_identity import canonical_customer, customer_mentions
            from backend.intent.customer_constraints import CustomerScopeError
            mentioned = {item[2] for item in customer_mentions(question)}
            for value in values:
                canonical = canonical_customer(value)
                if canonical in get_customer_aliases() and canonical not in mentioned:
                    raise CustomerScopeError('고객사 원문 근거가 다른 회사 이름의 부분 문자열입니다')
        aliases = get_customer_aliases() if field == 'customer' else PLATFORM_ALIASES
        for value in values:
            # 모델이 약칭을 정식 명칭으로 풀어 써도 같은 등록 별칭 묶음으로 근거를 확인한다.
            canonical = next((key for key, variants in aliases.items()
                              if value.casefold() in {v.casefold() for v in [key, *variants]}), value)
            forms = [canonical, *aliases.get(canonical, [])]
            require(any(form.casefold() in question.casefold() for form in forms), '고객사/매체 근거 불일치')
    if field in {'created_at', 'due_at', 'deployed_at', 'delivered_at'}:
        if field == 'created_at' and re.search(r'최근|최신', str(evidence)):
            # '최근 N건'은 정렬·개수 근거다. 실제 기간 없이 모델이 만든 날짜는
            # 실행하지 않고 기존 재시도 경로에서 날짜 필터를 제거하도록 한다.
            require(question_has_temporal_expression(str(evidence))
                    or extract_relative_created_at_range(str(evidence))
                    or re.search(r'20\d{2}[-./]\d{1,2}[-./]\d{1,2}', str(evidence)),
                    '최근/최신은 정렬 근거이며 등록 기간이 아닙니다. 명시한 기간이 없으면 created_at 필터를 제거하세요')
        try:
            dates = [date.fromisoformat(v) for v in values]
        except ValueError as exc:
            raise ValueError('날짜 형식 오류') from exc
        require(len(dates) in {1, 2} and (len(dates) == 1 or dates[0] <= dates[1]), '날짜 범위 오류')
        if field in {'created_at', 'delivered_at'}:
            check_relative_created_at(clause, str(evidence))
    return clause


def check_relative_created_at(clause, evidence):
    """상대 등록일은 모델 계산 대신 서버 달력 계산과 같은 범위만 실행한다.

    근거 구절만 해석해 한 질문의 다른 날짜 표현과 섞지 않는다. 등록일에는 미래가
    없으므로 오늘 이후의 끝 날짜는 오늘로 맞춰 '이번 달 1일~오늘'과 '1일~말일'을
    같은 조회로 본다. 불일치 사유에 정답 범위를 담아 플래너 재시도가 바로잡게 한다."""
    match = RELATIVE_CREATED_AT_EVIDENCE.fullmatch(evidence)
    if not match:
        return
    today = date.today()
    expected = extract_relative_created_at_range(match.group('expr'), today)
    if not expected:
        return
    values = values_for_clause(clause.get('value'))
    operator = clause['operator']
    if operator == 'between' and len(values) == 2:
        start, end = values
    elif operator == 'equals' and len(values) == 1:
        start = end = values[0]
    elif operator == 'gte' and len(values) == 1:
        start, end = values[0], today.isoformat()
    else:
        start = end = ''
    def clip(value):
        return min(value, today.isoformat())
    expected_range = (expected['from'], clip(expected['to']))
    require((start, clip(end)) == expected_range,
            f"'{evidence.strip()}' 기간은 달력 기준 {expected_range[0]}~{expected_range[1]}입니다")


def check_aging_period(question, source, operation, clauses, report_kind=None):
    """명시한 경과 일수의 상한을 버린 계획은 재시도하거나 차단한다."""
    if source != 'mantis' or not (operation in {'count', 'search', 'aggregate'}
                                 or operation == 'report' and report_kind == 'aging'):
        return
    text = unquoted(question)
    for clause in clauses:
        if clause['field'] in {'title', 'keyword'}:
            for value in values_for_clause(clause.get('value')):
                text = text.replace(value, ' ')
    if not re.search(r'미종료|미완료|열린|오픈', text):
        return
    match = re.search(r'(?P<days>\d+)\s*일\s*(?:넘게|이상|넘은|넘긴|이\s*넘은)', text)
    if not match:
        return
    days = int(match['days'])
    require(1 <= days <= (date.today() - date.min).days, '경과 일수는 1일 이상의 유효한 범위여야 합니다')
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    dates = [c for c in clauses if c['field'] == 'created_at']
    require(any((c['operator'] == 'lte' and c['value'] == cutoff)
                or (c['operator'] == 'between' and c['value'] == ['0001-01-01', cutoff]) for c in dates),
            f"'{days}일' 미종료 조건은 등록일 created_at lte {cutoff}입니다. 경과 일수 조건이 누락되거나 다릅니다")


def check_query_period(question, source, operation, clauses, report_kind=None):
    """명확한 조회 기간의 누락·잘못된 상속만 거부한다. 필터를 생성하지 않는다."""
    if source != 'mantis' or not (operation in {'count', 'search', 'aggregate'} or
            operation == 'report' and report_kind in {'weekly', 'monthly', 'profile', 'effort', 'sla', 'effort_sla',
                                                     'handoff', 'postmortem', 'notice'}):
        return
    if (report_kind in {'handoff', 'postmortem', 'notice'}
            and any(c['field'] == 'issue_id' for c in clauses)):
        # 특정 이슈의 이번 달 노트로 문서를 쓰는 요청은 이슈 등록일 검색이 아니다.
        return
    text = unquoted(question)
    # 따옴표 없이 쓴 제목/본문 검색어도 날짜 조건으로 승격하지 않는다.
    for clause in clauses:
        if clause['field'] in {'title', 'keyword'}:
            for value in values_for_clause(clause.get('value')):
                text = text.replace(value, ' ')
    if re.search(r'(?:기간|등록일|날짜).{0,12}(?:해제|제거|풀|빼|무관|관계없|상관없|제한\s*없이)', text):
        return
    # 문서의 전달 대상·작성 목적은 조회 기간이 아니다. 문서 작업에서는 실제로
    # 확정한 전달일 필터가 있을 때만 등록 기간 검사를 대신한다.
    work_period = (report_kind not in {'handoff', 'postmortem', 'notice'}
                   or any(c['field'] == 'delivered_at' for c in clauses))
    if work_period and report_kind not in {'effort', 'effort_sla'} and re.search(r'(?:개발|QC|전달|배포|마감|수정|해결|종료|완료)\s*(?:일|날짜|기한)|'
                 r'(?:완료|해결|종료|수정|변경|전달|배포|처리)(?:된|한|했|됐|할|해야)', text, re.I):
        return
    periods = list(re.finditer(
        r'(?<![A-Za-z가-힣0-9])(?:최근\s*\d{1,3}\s*(?:일|개월|달|주)|지난\s*달|저번\s*달|전\s+달|전월|이번\s*달|이달|금월|당월'
        r'|지난\s*주|저번\s*주|전\s*주(?!차)|이번\s*주|금주(?!차)|그저께|엊그제|그제|어제|전일(?!\s*정산)|오늘|금일)'
        r'(?=$|[\s,.?!:~()]|에|의|만|도|로|부터|까지|것|건|중|분|은|는|자)', text))
    if len(periods) != 1:
        return
    period = periods[0]
    suffix = text[period.end():]
    if re.match(r'\s*(?:(?:현재|말|말일|초|초일|\d{1,2}\s*일)\s*)?(?:기준|시점)', suffix):
        return
    if re.match(r'\s*(?:의\s*)?(?:조건|제한|필터|기간).{0,8}(?:해제|제거|풀|빼|제외)', suffix):
        return
    period_field = 'delivered_at' if report_kind in {'effort', 'effort_sla'} else 'created_at'
    dates = [clause for clause in clauses if clause['field'] == period_field]
    require(bool(dates), f"'{period.group()}' 조회의 등록 기간 또는 공수 전달 기간이 누락됐습니다. {period_field}과 현재 질문의 기간 근거를 포함하세요")
    # '지난달 15일 이후'처럼 범위가 세분화된 요청은 기존 필터 근거 검증에 맡긴다.
    if not re.match(r'\s*(?:\d{1,2}\s*일|[월화수목금토일]요일|이전|이후|부터|까지)', suffix):
        for clause in dates:
            check_relative_created_at(clause, period.group())


def same_filter(left, right):
    if left == right:
        return True
    if not left or not right or left.get('field') != right.get('field'):
        return False
    for operators in ({'equals', 'in'}, {'not_equals', 'not_in'}):
        if left.get('operator') in operators and right.get('operator') in operators:
            return set(values_for_clause(left.get('value'))) == set(values_for_clause(right.get('value')))
    return False


def _literal_similarity_keyword(question):
    return bool(re.search(r'정확히|문자열|키워드|(?:본문|노트|내용|텍스트).{0,100}(?:포함|들어|있는|있어)', question))


def check_notice_task(question, source, operation, report_kind):
    text = unquoted(question)
    if (source == 'mantis' and re.search(r'고객\s*안내|메신저\s*답변|점검\s*공지|회의\s*공유문', text)
            and re.search(r'작성|형식|초안|바꿔', text)):
        require(operation == 'report' and report_kind == 'notice',
                '고객 안내·메신저 답변·점검 공지·회의 공유문 작성은 report/notice입니다. 내부 원문 설명으로 대체할 수 없습니다')


def check_effort_task(question, report, followup):
    if report == 'effort_sla' and not followup:
        require(bool(re.search(r'처리\s*(?:시간|기간)|소요\s*(?:시간|기간)|리드\s*타임|해결\s*(?:시간|기간)|응답\s*시간|SLA',
                               unquoted(question), re.I)),
                '처리시간 요청이 없는 등록건수·공수 추이는 report/effort입니다. effort.type=trend로 계획하세요')
