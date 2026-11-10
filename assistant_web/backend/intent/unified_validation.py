"""전체 질의 계획의 계약 검증과 확정 조건 변환. 질문을 다시 해석해 덮어쓰지 않는다."""
from copy import deepcopy
from backend.intent import validation_rules
from datetime import date as date, timedelta as timedelta
import re

from backend.settings.catalogs import get_customer_aliases
from backend.settings.values import PLATFORM_ALIASES as PLATFORM_ALIASES, STATUS_ALIASES as STATUS_ALIASES, MANTIS_STATUS_NAMES as MANTIS_STATUS_NAMES, MANTIS_OPEN_STATUSES as MANTIS_OPEN_STATUSES
from backend.intent.entities import short_handler_lookup
from backend.intent.scope_extraction import empty_query_constraints
from backend.query_filters import normalize_filter_clause as normalize_filter_clause, clause_supported as clause_supported, SUPPORTED_FIELDS as SUPPORTED_FIELDS, values_for_clause as values_for_clause
from backend.query.dates import extract_relative_created_at_range as extract_relative_created_at_range
from backend.domain.team import TEAM_MEMBERS, TEAM_SCOPE_LABEL

from backend.intent.validation_rules import (
    STATUS_ALL as STATUS_ALL,
    PENDING as PENDING,
    PERSON_FIELDS as PERSON_FIELDS,
    STATUS_VALUES as STATUS_VALUES,
    _STATUS_LABEL as _STATUS_LABEL,
    EXPLICIT_STATUS_REQUEST as EXPLICIT_STATUS_REQUEST,
    RELATIVE_CREATED_AT_EVIDENCE as RELATIVE_CREATED_AT_EVIDENCE,
    REPORT_KINDS as REPORT_KINDS,
    require as require,
    unquoted as unquoted,
    check_status as check_status,
    evidence_is_grounded as evidence_is_grounded,
    effective_statuses as effective_statuses,
    declared_statuses as declared_statuses,
    normalize_status_clauses as normalize_status_clauses,
    check_clause as check_clause,
    check_relative_created_at as check_relative_created_at,
    check_aging_period as check_aging_period,
    check_query_period as check_query_period,
    same_filter as same_filter,
    _literal_similarity_keyword as _literal_similarity_keyword,
    check_notice_task as check_notice_task,
    check_effort_task as check_effort_task,
)
from backend.intent.execution_contract import (
    validate_execution_contract as validate_execution_contract,
)
from backend.query.notion_text import is_plain_notion_topic, split_notion_topic_terms


def constraints_from_clauses(clauses, status, scope, user, previous, *, self_field="owner"):
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
        elif field in {'created_at', 'delivered_at'} and operator == 'between':
            c[field] = {'from': values[0], 'to': values[1]}
        elif field in {'customer', 'platform'} and included and len(values) == 1:
            catalog = get_customer_aliases() if field == 'customer' else PLATFORM_ALIASES
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
    change = status.get('change', 'remove' if previous and mode == 'exclude' else 'replace')
    if change == 'remove':
        c['statuses'] = list(previous.get('statuses') or [])
        c['excluded_statuses'] = sorted(set(previous.get('excluded_statuses') or []) | set(status['values']))
    elif change == 'add':
        c['statuses'] = sorted(effective_statuses(previous.get('statuses') or [],
                                               previous.get('excluded_statuses') or []) | set(status['values']))
        c['excluded_statuses'] = []
    for op, values in [('in', c['statuses']), ('not_in', c['excluded_statuses'])]:
        if values:
            c['filter_clauses'].append({'field': 'status', 'operator': op, 'value': values})
    if scope == 'self':
        name = str(user.get('mantisUsername') or user.get('displayName') or '').strip()
        require(bool(name), '개인 이슈의 담당자명이 등록되어 있지 않습니다')
        self_names = [v for f in clauses if f['field'] == self_field and f['operator'] in {'equals', 'in'}
                      for v in values_for_clause(f.get('value'))]
        require(not self_names or set(self_names) == {name}, '개인 범위와 다른 인물이 충돌합니다')
        c['people'] = {'names': [name], 'role': 'handler' if self_field == 'owner' else self_field, 'name_origins': {name: 'known'}, 'candidates': []}
        c['filter_clauses'].append({'field': self_field, 'operator': 'equals', 'value': name})
    if scope == 'inherit':
        scope = previous.get('mantis_team_scope', 'default')
    if scope == 'default' or scope == 'target' and not (
            c['people']['names'] or c['issue_ids'] or c.get('unassigned_people')):
        # 고객사·상태·화면은 개인/번호 대상이 아니다. 모델의 target 표기로
        # 일반 조회의 기본 팀 범위가 사라지지 않게 한다.
        scope = 'target' if c['people']['names'] or c['issue_ids'] or c.get('unassigned_people') else 'team'
    if scope == 'team':
        c.update(mantis_team_members=list(TEAM_MEMBERS), mantis_team_label=TEAM_SCOPE_LABEL)
    c['mantis_team_scope'] = 'target' if scope == 'self' else scope
    c['filter_clauses'] = list({(f['field'], f['operator'], str(f.get('value'))): f for f in c['filter_clauses']}.values())
    return c


def validate_plan(question, parsed, user, previous):
    require(isinstance(parsed, dict), '계획 JSON 형식 오류')
    from backend.intent.conversation_scope import followup_previous
    previous, followup_context = followup_previous(parsed, previous)
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
    from backend.intent.context import starts_independent_question
    require(not (followup and starts_independent_question(question)),
            '독립 질문에는 이전 조건을 상속할 수 없습니다. followup=false로 현재 질문만 해석하세요')
    if re.match(r'^\s*(?:그중|그\s*조건|같은\s*조건)', unquoted(question)):
        require(followup and bool(previous.get('resolved_constraints')),
                '후속 조건을 이어받을 이전 확정 계획이 필요합니다. 앞 조건이 없으면 clarify로 답하세요')
    base = deepcopy(previous.get('resolved_constraints') or {}) if followup else {}
    require(not followup or bool(base), '이어서 조회할 확정 검색 기록이 없습니다')
    require(scope != 'inherit' or followup, '상속할 범위가 없습니다')
    inherited_scope = scope == 'inherit'
    from backend.intent.aggregation_spec import validate_aggregation, period_clause
    previous_aggregation = previous.get('aggregation') if followup else None
    aggregation = None
    if parsed.get('aggregation') is not None or (previous_aggregation and operation == 'aggregate'):
        require(source == 'mantis' and operation == 'aggregate', '집계 명세는 Mantis aggregate 작업입니다')
        aggregation = validate_aggregation(question, parsed.get('aggregation') if parsed.get('aggregation') is not None else {}, previous_aggregation)
        require(parsed.get('group_by') is None and parsed.get('limit') is None, '복합 집계와 기존 그룹/표시 개수 계약이 충돌합니다')
        require(not (scope == 'self' and len(aggregation['metrics']) > 1),
                '개인 건수의 담당자와 공수 기입자 범위를 구분해 지표별로 요청해 주세요')
    elif previous_aggregation and operation in {'count', 'search'}:
        # 출력 형태를 건수/목록으로 바꿔도 조회 기간은 버리지 않는다.
        period = period_clause(previous_aggregation, 'issue_count')
        base.setdefault('filter_clauses', []).append(period)
        base['created_at'] = {k: previous_aggregation['period'][k] for k in ('from', 'to')}
    if followup:
        require(previous.get('source') == source, '후속 조회의 원천이 바뀌었습니다')
    document_text = unquoted(question)
    from backend.intent.entities import extract_issue_ids
    document_ids = extract_issue_ids(document_text) or (base.get('issue_ids') if followup else []) or []
    # 현재 담당자는 이력 설명의 인용문이 아니라 이슈 속성에서 답한다.
    # 단일 대상이 확정되고 질문 전체가 현재 담당자 조회인 경우에만 정규화한다.
    if (source == 'mantis' and operation == 'explain' and len(document_ids) == 1
            and re.fullmatch(
                r'\s*(?:(?:(?:그|해당|이)\s*이슈|(?:맨티스\s*)?#?\d+(?:번)?(?:\s*(?:(?:그|해당|이)\s*)?이슈)?)(?:의)?\s*)?'
                r'(?:현재\s*)?담당자(?:는|가)?\s*(?:누구(?:야|인가요|인지|예요)?|누군지)'
                r'(?:\s*알려(?:줘|주세요)?)?\s*[?.!]*\s*', document_text)):
        operation, scope = 'get', 'target'

    if (source == 'mantis' and len(document_ids) == 1 and re.search(r'사후\s*분석|회고', document_text)
            and re.search(r'작성|형식|(?:5|다섯)\s*줄|요약으로', document_text)):
        require(operation == 'report' and parsed.get('report_kind') == 'postmortem',
                '사후 분석·회고 문서의 분량과 형식은 postmortem 보고서로 작성해야 합니다')
    # 조회 조건만 바꾸라는 요청에서는 비교 과업도 확정 문맥의 일부다.
    # 명시적인 기준 유지 요청은 followup 플래그 자체가 빠져도 거절한다.
    condition_edit = re.search(r'그대로|유지|좁혀|한정|제한.{0,8}(?:풀|해제)|조건.{0,8}(?:풀|해제)', question)
    explicit_switch = re.search(r'비교.{0,12}(?:끝내|그만|마치|중단|종료|말고|대신)|상세\s*(?:조회|알려|보여)|상태만\s*(?:알려|보여)', question)
    if previous.get('report_kind') == 'similar' and condition_edit and not explicit_switch:
        require(followup and operation == 'report' and (parsed.get('report_kind') or 'similar') == 'similar',
                '조건 변경 후에도 기준 이슈와 유사 사례 비교 작업을 유지해야 합니다')
    document_kinds = {'postmortem', 'handoff', 'notice'}
    presentation_edit = re.search(r'형식|말투|회의.{0,20}안건|메일|메신저|(?:5|다섯)\s*줄|간결|정중', question)
    if followup and previous.get('report_kind') in document_kinds and presentation_edit:
        requested_kind = ('notice' if re.search(r'고객.{0,15}(?:안내|메일)|메신저|점검\s*공지', question)
                          else 'handoff' if '인수인계' in question
                          else 'postmortem' if re.search(r'사후\s*분석|회고', question)
                          else previous['report_kind'])
        require(operation == 'report' and (parsed.get('report_kind') or previous['report_kind']) == requested_kind,
                f'문서 형식 변경은 {requested_kind} 문서의 이슈와 근거를 유지해야 합니다. report_kind를 확인하세요')
    parsed = normalize_status_clauses(parsed, base, question)
    status = parsed['status']
    report = parsed.get('report_kind') or (previous.get('report_kind') if followup and operation == 'report' else None)
    check_notice_task(question, source, operation, report)
    check_effort_task(question, report, followup)
    soft_topics = []
    if operation == 'report' and report == 'similar':
        # 유사성 주제와 명시적인 문자열 조건은 다른 계약이다. 이전 버전의 과제약도
        # 후속 질문에서 되살리지 않되, 사용자가 지정한 리터럴 조건·제외는 보존한다.
        literals = previous.get('similar_literal_filters', [])
        retained = []
        for clause in base.get('filter_clauses', []):
            if (clause['field'] == 'keyword' and clause['operator'] == 'contains'
                    and clause not in literals and not _literal_similarity_keyword(previous.get('grounding_text') or '')):
                soft_topics.extend(values_for_clause(clause.get('value')))
            else:
                retained.append(clause)
        base['filter_clauses'] = retained
    check_status(question, status, followup, source, operation, report)
    clear = parsed['clear_fields']
    require(isinstance(clear, list), '해제 조건은 검색 필드 배열이어야 합니다')
    # 그룹 집계를 단일 건수로 바꾸는 출력 지시는 검색 필드를 해제하지 않는다.
    if (followup and previous.get('operation') == 'aggregate' and operation == 'count'
            and parsed.get('group_by') is None):
        clear = [field for field in clear if field != 'group_by']
        parsed['clear_fields'] = clear
    invalid_clear = [f for f in clear if not isinstance(f, str) or f not in SUPPORTED_FIELDS | {'terms', 'team'}]
    require(not invalid_clear, '해제 조건 오류: ' + ', '.join(str(f)[:30] for f in invalid_clear) +
            '. report_kind/group_by/sort는 clear_fields가 아닙니다. 과업은 operation으로 바꾸고 검색 주제만 terms로 해제하세요')
    previous_fields = {f['field'] for f in (previous.get('resolved_constraints') or {}).get('filter_clauses', [])}
    if (clear and not followup and source == previous.get('source') == 'mantis'
            and operation in {'get', 'explain'} and scope == 'target' and len(document_ids) == 1
            and set(clear) <= previous_fields and not set(clear) & {'team', 'status'}):
        # 독립적인 번호 조회는 이전 필터를 상속하지 않는다. 이미 적용되지 않는
        # 이전 조건의 해제 표기만 제거하며, 현재 필터와 번호 근거 검증은 유지한다.
        clear = []
        parsed['clear_fields'] = clear
    require(not clear or followup, '해제할 이전 조건이 없습니다')
    from backend.intent.followup_changes import validate_clear_evidence
    comparison_end = bool(previous.get('report_kind') == 'similar' and operation == 'get'
                          and explicit_switch and len(document_ids) == 1)
    raw = parsed['filters']
    require(isinstance(raw, list) and len(raw) <= 20, '필터 개수 오류')
    # '관련 문서'의 일반 주제를 본문 전용 keyword로 좁히지 않는다.
    # 명시한 본문/제목/리터럴 조건과 후속의 기존 범위는 그대로 둔다.
    plain_notion_topics = source == 'notion' and operation == 'search' and not followup and is_plain_notion_topic(question)
    incoming = []
    grounded_raw = []
    for item in raw:
        # 이전 확정 조건을 모델이 반복해 출력해도 새 조건으로 취급하지 않는다.
        # 서버 기록과 값·필드가 같고 연산자도 동등한 경우만 생략하며 새 값은 원문 근거가 필요하다.
        normalized = normalize_filter_clause(item)
        if (followup and normalized and any(same_filter(normalized, prior) for prior in base.get('filter_clauses', []))
                and normalized['field'] not in clear):
            continue
        checked = check_clause(item, question, source)
        if (operation == 'report' and report == 'similar' and checked['field'] == 'keyword'
                and checked['operator'] == 'contains' and not _literal_similarity_keyword(question)):
            soft_topics.extend(values_for_clause(checked.get('value')))
            continue
        if plain_notion_topics and checked['field'] == 'keyword' and checked['operator'] == 'contains':
            soft_topics.extend(values_for_clause(checked.get('value')))
            continue
        incoming.append(checked)
        grounded_raw.append(item)
    raw = grounded_raw
    planned_status = [*incoming, *(base.get('filter_clauses', []) if followup else [])]
    if source == 'notion' and operation in {'search', 'count'} and not any(c['field'] == 'status' for c in planned_status):
        # Mantis의 '명시된 상태 조건 누락' 검사와 같은 원칙이다. 파서가 읽은 상태를 계획이 버리지 못하게 한다.
        from backend.intent.notion_constraints import extract_notion_constraints
        native = extract_notion_constraints(unquoted(question))
        require(not (native.get('status') or native.get('excluded_statuses')),
                '질문의 Notion 상태 조건이 계획에 없습니다. filters에 status equals/not_equals로 지정하세요')
    # 검증된 새 값으로 같은 필드를 교체하면 별도 해제가 필요 없다. 모델이
    # 중복 선언한 해제만 제거하며, 새 값은 위의 원문 근거 검사를 통과해야 한다.
    clear = [field for field in clear if field not in {clause['field'] for clause in incoming}]
    clear_evidence = {}
    ignored_changes = []
    requested_clear = list(clear)
    for field in clear:
        try:
            clear_evidence.update(validate_clear_evidence(question, [field], parsed.get('clear_evidence') or {},
                base.get('filter_clauses') or [], comparison_end=comparison_end))
        except ValueError as exc:
            ignored_changes.append({'field': field, 'reason': str(exc)})
    # 현재 발화에서 검증한 변경이 있으면 그 변경만 적용한다. 모델이 덧붙인
    # 근거 없는 해제로 다른 기존 조건을 잃지 않으며, 적용/보존 이유를 감사에 남긴다.
    if ignored_changes and not (incoming or clear_evidence or status['mode'] in {'open', 'include', 'exclude', 'all'}):
        require(False, ignored_changes[0]['reason'])
    clear = list(clear_evidence)
    if followup and report=='similar' and 'customer' in requested_clear and re.search(r'좁|한정',question):
        named_customer=any(re.search(r'(?<![A-Za-z0-9])'+re.escape(alias)+r'(?![A-Za-z0-9])',question,re.I)
                           for key,aliases in get_customer_aliases().items() for alias in [key,*aliases])
        explicit_removal=re.search(r'(?:고객사|증권사).{0,12}(?:해제|풀|빼|제거|제외|없)',question)
        require(not named_customer or explicit_removal or any(f['field']=='customer' for f in incoming),
                '고객사로 범위를 좁히는 질문에서 고객사 조건을 해제했습니다. 명시된 고객사와 플랫폼을 모두 필터에 넣고 기준 이슈는 유지하세요.')
    replaced = {f['field'] for f in incoming} | set(clear) | {'status'}
    handler_lookup = source == 'mantis' and operation == 'search' and short_handler_lookup(question)
    if replaced & (PERSON_FIELDS - {'effort_person'}) and not handler_lookup:
        replaced |= PERSON_FIELDS - {'effort_person'}
    clauses = [f for f in base.get('filter_clauses', []) if f['field'] not in replaced]
    clauses.extend(incoming)
    if handler_lookup:
        # 등록자/요청자 조건은 함께 유지하고, 응답에 표시할 인물 범위는 담당자로 둔다.
        clauses.sort(key=lambda clause: clause['field'] != 'owner')
    self_field = 'effort_person' if (operation == 'report' and report in {'effort', 'effort_sla'}
                                    or aggregation and aggregation['metrics'] == ['effort']) else 'owner'
    if scope == 'self':
        removed = {'effort_person'} if self_field == 'effort_person' else PERSON_FIELDS - {'effort_person'}
        clauses = [f for f in clauses if f['field'] not in removed]
    if 'team' in clear:
        scope = 'all'
    if not aggregation:
        check_query_period(question, source, operation, clauses, report)
    check_aging_period(question, source, operation, clauses, report)
    # 포함과 제외를 AND로 합쳐 빈 집합을 만드는 계획을 실행하지 않는다.
    for a in clauses:
        for b in clauses:
            if a['field'] == b['field'] and a['operator'] in {'equals', 'in', 'contains'} and b['operator'] in {'not_equals', 'not_in', 'not_contains'}:
                require(not set(values_for_clause(a.get('value'))) & set(values_for_clause(b.get('value'))), '동일 값의 포함/제외 조건이 충돌합니다')
    if operation == 'explain' and scope == 'default':
        scope = 'all'
    effective_scope = scope if source in {'mantis', 'both'} or scope == 'self' else 'all'
    scoped_user = {**user, 'mantisUsername': user.get('displayName')} if source == 'notion' else user
    c = constraints_from_clauses(clauses, status, effective_scope, scoped_user, base, self_field=self_field)
    from backend.intent.followup_changes import validate_all_scope_evidence
    scope_evidence = ''
    if (followup and c['mantis_team_scope'] == 'all' and base.get('mantis_team_scope') != 'all'
            and not c['people']['names'] and not c['issue_ids']):
        try:
            scope_evidence = validate_all_scope_evidence(question, question)
        except ValueError as exc:
            ignored_changes.append({'field': 'team', 'reason': str(exc)})
            scope = 'inherit'
            c = constraints_from_clauses([clause for clause in clauses if clause['field'] != 'status'],
                status, scope, scoped_user, base, self_field=self_field)
    if followup and inherited_scope and source in {'mantis', 'both'} and operation in {'search', 'count', 'aggregate'}:
        # 모델이 '내 담당만', '다시 우리 팀만' 같은 범위 변경을 inherit로 빠뜨리면
        # 이전 범위가 조용히 유지된다. 원문이 다른 범위를 명시하면 재해석을 요청한다.
        from backend.intent.followup_changes import requested_scope
        wanted = requested_scope(question)
        previous_scope = base.get('mantis_team_scope')
        own_name = str(user.get('mantisUsername') or user.get('displayName') or '').strip()
        if previous_scope == 'target' and own_name and (base.get('people') or {}).get('names') == [own_name]:
            previous_scope = 'self'
        labels = {'self': '본인 담당', 'team': '팀', 'all': '전사'}
        require(not wanted or wanted == previous_scope,
                f"질문이 조회 범위를 {labels.get(wanted, '')}(으)로 바꾸라고 요청했는데 scope=inherit로 이전 범위를 유지했습니다. "
                f"scope 값을 {wanted}(으)로 지정하고 나머지 조건은 그대로 상속하세요")
    if (previous.get('report_kind')=='similar' and followup and operation=='get'
            and explicit_switch and re.search(r'기준.{0,12}(?:하나|한\s*건|상태만)',question)):
        require(c.get('issue_ids')==base.get('issue_ids'), '비교 종료 후에도 기준 이슈 번호를 유지해야 합니다')
        inherited_candidate_fields={f['field'] for f in clauses if f['field']!='issue_id'}-{f['field'] for f in incoming}
        require(not inherited_candidate_fields,
                '기준 이슈 하나를 조회할 때 비교 후보 조건은 상속하지 않습니다. clear_fields로 후보 조건을 해제하세요: '+', '.join(sorted(inherited_candidate_fields)))
    # 계획에 없는 조건을 추측해 추가하지는 않되, 원문이 명시한 대상을 조용히
    # 버린 계획은 거부한다. 모델이 수정해도 끝내 확정하지 못하면 조회하지 않는다.
    if source in {'mantis', 'both'}:
        from backend.intent.entities import extract_issue_ids, extract_people_constraints
        text = unquoted(question)
        issue_ids = extract_issue_ids(text)
        # 특수 보고서는 검증된 전용 빌더가 기준 ID를 원문에서 복원한다.
        if issue_ids and operation != 'report':
            applied_ids = {int(value.lstrip('#')) for clause in c['filter_clauses']
                           if clause['field'] == 'issue_id'
                           for value in values_for_clause(clause.get('value'))}
            require(set(issue_ids) <= applied_ids, '명시된 이슈 번호가 필터에서 누락됐습니다')
        people = (extract_people_constraints(text) if operation in {'search', 'count', 'aggregate'}
                  and re.search(r'담당자|담당\s*[:=]', text) else {})
        if people.get('role') == 'handler' and people.get('names'):
            applied_names = {value for clause in c['filter_clauses'] if clause['field'] == 'owner'
                             for value in values_for_clause(clause.get('value'))}
            require(set(people['names']) <= applied_names,
                    '명시된 담당자 이름이 필터에서 누락됐습니다: ' + ', '.join(people['names']))
    if source == 'notion':
        from backend.intent.customer_constraints import check_customer_scope
        check_customer_scope(question, c['filter_clauses'], incoming)
        from backend.query.notion_text import notion_literal_title
        title = notion_literal_title(question)
        if operation == 'explain' and not title and '제목' not in question and 'title' not in question.casefold():
            require(not any(clause['field'] == 'title' for clause in incoming),
                    '설명 질문의 주제를 문서 제목 필터로 단정할 수 없습니다. title equals/contains 대신 terms로 표현하세요')
        if title:
            require(any(clause['field'] == 'title' and clause['operator'] == 'equals'
                        and clause.get('value') == title for clause in c['filter_clauses']),
                    '인용된 Notion 제목은 keyword가 아니라 title equals 필터로 보존해야 합니다')
    if followup:
        c['inherited_from_audit_id'] = previous.get('audit_id') or 'confirmed_previous_plan'
        c['list_scope_edit'] = True
    terms = parsed['terms']
    require(isinstance(terms, list) and len(terms) <= 6 and all(isinstance(t, str) and 0 < len(t) <= 100 for t in terms), '검색어 형식 오류')
    # '가장 최근 이슈'는 정렬·개수 표현이다. 검색어로 쓰면 제목·본문 포함 조건이 되어 결과가 0건이 된다.
    from backend.intent.validation_rules import is_ordering_phrase
    ordering = [t for t in terms if is_ordering_phrase(t)]
    require(not ordering, f"정렬·개수 표현은 검색어가 아닙니다: {', '.join(ordering)}. terms에서 빼고 sort·limit으로 표현하세요")
    if operation == 'report' and report in {'handoff', 'postmortem', 'notice'}:
        require(not terms, '이슈 문서의 출력 항목은 검색어가 아닙니다. terms=[]로 두고 실제 조회 주제만 keyword/title 등 filters에 원문 근거와 함께 지정하세요')
    if followup and not terms and 'terms' not in clear and not (replaced & {'keyword', 'title'}):
        terms = list(previous.get('terms') or [])
    if operation == 'report' and report == 'similar':
        terms = list(dict.fromkeys([*terms, *soft_topics]))[:6]
    if plain_notion_topics:
        terms = list(dict.fromkeys([*terms, *soft_topics]))
        require(len(terms) <= 6, '검색 주제가 너무 많습니다')
    if source == 'notion':
        from backend.intent.customer_constraints import consume_customer_terms
        terms = consume_customer_terms(terms, c['filter_clauses'])
        if plain_notion_topics:
            terms = split_notion_topic_terms(terms)
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
    from backend.intent.aggregation_spec import check_target
    check_target(operation, group, aggregation)
    if source == 'mantis' and operation in {'count', 'aggregate'} and group and c.get('created_at') and not aggregation:
        from backend.intent.aggregation_spec import explanation_requested
        require(not explanation_requested(unquoted(question)),
                '집계 설명 요청은 operation=aggregate, group_by=null, aggregation.group_by와 기간, explain=true로 보존하세요')
    limit = parsed['limit']
    require(limit is None or (type(limit) is int and 1 <= limit <= 200), '표시 개수 오류')
    require(operation != 'count' or limit is None, '전체 집계에 표시 개수를 적용할 수 없습니다')
    from backend.intent.display_contract import validate_display_limit
    validate_display_limit(question, source, operation, limit)
    sort = parsed.get('sort') or (previous.get('sort') if followup else None) or {'field': 'created_at', 'direction': 'desc'}
    require(isinstance(sort, dict) and sort.get('field') in {'created_at', 'updated_at', 'issue_id'} and sort.get('direction') in {'asc', 'desc'}, '정렬 형식 오류')
    if parsed.get('sort') or (followup and previous.get('sort')):
        c['sort'] = deepcopy(sort)
    require(operation != 'report' or report in REPORT_KINDS, '지원하지 않는 보고서')
    effort_options = None
    if operation == 'report' and report in {'effort', 'effort_sla'}:
        effort_options = parsed.get('effort') or (previous.get('effort') if followup else None) or {'type': 'total'}
        require(isinstance(effort_options, dict) and effort_options.get('type') in {'total', 'ranking', 'trend'}, '공수 출력 형태 오류')
        require(set(effort_options) <= {'type', 'order', 'show_all'}, '지원하지 않는 공수 계획 항목')
        require(effort_options.get('order', 'desc') in {'asc', 'desc'}, '공수 정렬 오류')
        require(isinstance(effort_options.get('show_all', False), bool), '공수 전체 표시 형식 오류')
        require(source == 'mantis', '공수는 Mantis 원천으로 조회해야 합니다')
        if any(f['field'] == 'created_at' for f in incoming):
            require(bool(re.search(r'등록|접수|생성', question)), '공수 기간은 고객사전달일 delivered_at에 지정해야 합니다')
        if not any(f['field'] == 'delivered_at' for f in clauses):
            # 기간 생략 시 올해라는 회사 기본값. 질문에 명시한 기간은 위에서 검증한다.
            year = validation_rules.date.today().year
            period = {'field': 'delivered_at', 'operator': 'between', 'value': [f'{year}-01-01', f'{year}-12-31']}
            c['filter_clauses'].append(period)
            c['delivered_at'] = {'from': period['value'][0], 'to': period['value'][1]}
        c['effort_date_basis'] = 'delivered_at'
    c['unified_plan_validated'] = True
    from backend.intent.condition_grounding import build_condition_grounding
    condition_grounding = build_condition_grounding(question, c, incoming, status, effective_scope, scoped_user, previous if followup else {})
    condition_grounding['previous_clauses'] = deepcopy(base.get('filter_clauses') or [])
    condition_grounding['previous_scope'] = base.get('mantis_team_scope')
    condition_grounding['preserved_clauses'] = deepcopy([clause for clause in clauses
        if clause in (base.get('filter_clauses') or []) and clause['field'] not in replaced
        and not (scope == 'self' and clause['field'] in PERSON_FIELDS)])
    return {'version': 3, 'planner': 'unified_llm', 'source': source, 'operation': operation,
            'grounding_text': question, 'condition_grounding': condition_grounding, 'planned': True, 'confidence': 1.0, 'reason': '전체 질문의 단일 검증 계획',
            'filters': {'customer': c.get('customer_key'), 'platform': c.get('platform_key'), 'people': c['people'],
                        'statuses': c['statuses'], 'issue_ids': c['issue_ids'], 'screen_numbers': c['screen_numbers'], 'created_at': c['created_at']},
            'filter_clauses': deepcopy(c['filter_clauses']), 'resolved_constraints': c, 'terms': terms,
            'group_by': group, 'limit': limit, 'sort': sort, 'report_kind': report,
            **({'aggregation': aggregation} if aggregation else {}),
            **({'effort': deepcopy(effort_options)} if effort_options else {}),
            'similar_literal_filters': [f for f in c['filter_clauses'] if f['field'] == 'keyword'] if report == 'similar' else [],
            'interpretation': {'status': deepcopy(status), 'followup': followup, 'scope': scope,
                               'followup_context': followup_context,
                               'evidence': deepcopy(raw), 'clear_fields': clear,
                               'clear_evidence': clear_evidence, 'comparison_end': comparison_end,
                               'scope_evidence': scope_evidence, 'ignored_changes': ignored_changes}}
