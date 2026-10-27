"""이슈의 내용 설명을 구조화 목록·집계 요청과 구분한다."""
import re
from copy import deepcopy
from typing import Any


def explanation_search_terms(text: str, constraints: dict[str, Any]) -> list[str]:
    from backend.intent.search_terms import build_keyword_terms
    return [term for term in build_keyword_terms(text, constraints)
            if not re.fullmatch(r"어떻게|왜|(?:원인|조치|수정|해결|처리|비교|설명|근거)"
                                r"(?:(?:해|하|했|됐|되|부탁).*|내용|사항|방법|과|와|을|를|은|는|이|가|로|도|만)?|"
                                r"알려.*|사례|내용|진행|경과|결과|과정|구체적.*", term)]


def wants_issue_explanation(text: str, constraints: dict[str, Any]) -> bool:
    from backend.intent.general import is_general_question
    if is_general_question(text):
        return False
    from backend.intent.source_router import rule_based_search_intent
    source_plan = rule_based_search_intent(text, constraints)
    source = source_plan.get("source")
    # '필요했던 자료'처럼 이슈 이력 속 자료를 언급한 경우에는 advisory
    # 문서 힌트보다 고객사 롤백·재전달의 설명 의도를 우선한다.
    issue_workflow = bool(
        constraints.get("customer_key")
        and re.search(r"롤백|재전달", text)
        and re.search(r"QC|검수|재전달|전달\s*기록", text)
    )
    advisory_document_hint = (
        source == "notion" and source_plan.get("authority") == "advisory" and issue_workflow
    )
    if source in {"notion", "both"} and not advisory_document_hint:
        return False
    scoped = source == "mantis" or bool(constraints.get("customer_key") or constraints.get("issue_ids") or constraints.get("platform_key"))
    if not scoped:
        return False
    # 기술 필드의 개수·배열 크기는 이슈 건수 집계와 다르다.
    from backend.query_planning import infer_operation
    count_text = re.sub(r"(?:필드|배열|원소|상수)(?:의|가|는|은|이)?\s*(?:개수|갯수|수량|몇\s*개)", " ", text)
    # 특정 이슈 안의 수정 항목 수는 원본 이슈 건수 집계와 구분한다.
    internal_quantity = (len(constraints.get('issue_ids') or []) == 1
            and re.search(r'수정\s*내역|재작업|변경\s*항목', text)
            and re.search(r'이유|처음|이후', text)
            and not re.search(r'이슈(?:는|가|의)?\s*(?:총\s*)?몇\s*건', text))
    if internal_quantity:
        count_text = re.sub(r'몇\s*건|건수', ' ', count_text)
    if re.search(r"필드|배열|원소|상수", text):
        count_text = re.sub(r"변경\s*전후\s*(?:개수|갯수)", " ", count_text)
    if infer_operation(count_text) in {"count", "aggregate"}:
        return False
    if re.search(r"몇\s*건|건수|이슈\s*(?:총\s*)?(?:개수|수량)|공수|통계|현황|업무\s*보고|주간\s*보고|추이", count_text):
        return False
    if not internal_quantity and re.search(r"(?:목록|리스트)(?:을|를)?\s*(?:\d+\s*건|만|보여|알려|조회|정리|$)", text):
        return False
    return bool(re.search(
        r"왜|어떻게|이유|설명|계산식|공식|무엇이라고\s*(?:답했|안내했)|"
        r"재현(?:된|되는|한)?\s*(?:조건|순서)|진행\s*(?:경과|상황)|"
        r"(?:뒤에|나중에|이후).{0,25}(?:결론|안내)|최종\s*(?:안내|답변|확인)|"
        r"(?:마지막|최종).{0,25}(?:검수|QC|전달|배포).{0,25}(?:상태|이력|결과|일정).{0,25}(?:정리|알려|확인)|"
        r"(?:결과|배포\s*상태).{0,40}(?:나눠|구분)|(?:해결|완료).{0,15}뜻이야|"
        r"(?:최종.{0,25}(?:내용|뭐야)|어떤\s*(?:형식|방식|절차)|(?:전달|QC|처리)\s*이력|테스트\s*범위)|"
        r"(?:원인|조치|수정\s*(?:내용|사항|방법)|종료\s*사유|최종\s*(?:결론|합의)|반영\s*상태).{0,45}(?:알려|정리|비교|확인|구분)|"
        r"(?:근거|원문|기록).{0,30}(?:알려|정리|답해|확인|구분|판단)|"
        r"(?:전달|QC|배포).{0,30}(?:구분|나눠)|"
        r"(?:발신자|노트\s*(?:작성자|등록자)|(?:최종|변경된)\s*(?:FID|TR|필드)).{0,60}(?:구분|알려|설명)|"
        r"(?:어떤|무엇|뭐|어디|언제|누가).{0,25}(?:발생|재현|전달|반영|적용|수정|처리|원인|조치|확인|문의)|"
        r"(?:안내|말해).{0,12}(?:돼|되나)|"
        r"결국\s*(?:구현|개발|수정|배포|반영)(?:했|됐)|"
        r"(?:반영|배포|개발|수정).{0,15}(?:됐|했|끝났|완료).{0,25}(?:돼|되나|맞아|있어|확인)", text))


def explanation_date_text(text: str) -> str:
    return re.sub(r"(?:당일|전일|익일|전날)\s*(?:값|환율|가격|종가|시세|데이터)", " ", text)


def explanation_filter_text(text: str) -> str:
    """이력 안에서 제외한 기능을 묻는 표현만 검색 제외 조건과 구분한다."""
    return re.sub(r'문제\s*없이|제외(?:한|했던|된)\s*(?:두|세|네|\d+)?\s*(?:항목|기능|내역)|'
                  r'(?:배포|반영|전달)\s*제외\s*여부', ' ', text)


def _model_open_evidence_describes_work_state(constraints: dict[str, Any]) -> bool:
    resolution = constraints.get('status_resolution') or {}
    if resolution.get('origin') != 'llm' or resolution.get('status_intent') != 'open':
        return True
    evidence = str(resolution.get('evidence') or '')
    return bool(re.search(
        r'미종료|미해결|해결되지|끝나지|안\s*끝|마무리\s*못|처리\s*중|'
        r'손봐야|챙겨야|남은\s*(?:이슈|업무|티켓|건|것|거)|'
        r'해야\s*할\s*(?:이슈|업무|티켓|건|것|거)', evidence))


def explanation_constraints(text: str, constraints: dict[str, Any]) -> dict[str, Any]:
    """상태를 설명 대상으로 언급한 문구만 마스킹하고 나머지 조건을 보존한다."""
    from backend.intent.scope_extraction import extract_status_scope
    from backend.common import normalize_for_match
    if (constraints.get('mantis_team_scope') == 'default'
            and not constraints.get('inherited_from_audit_id')
            and not constraints.get('statuses') and not constraints.get('excluded_statuses')):
        # 고객사의 특정 장애 경위를 묻는 설명에는 담당 팀을 명시하지 않았다.
        # 기본 팀 담당자 제한은 이슈 원문 자체를 숨길 수 있으므로 설명에만 적용하지 않는다.
        constraints = deepcopy(constraints)
        for key in ('mantis_team_members', 'mantis_team_label', 'mantis_team_scope'):
            constraints.pop(key, None)
    if (len(constraints.get('issue_ids') or []) == 1 and constraints.get('customer_key') == '우리'
            and not constraints.get('inherited_from_audit_id')):
        # 단일 이슈의 '우리가 확인한'은 우리선물 조건이 아니다. 대명사만 제거하고
        # 다시 해석해 별도로 명시된 '우리 HTS'와 실제 고객사 조건은 보존한다.
        customer_text = re.sub(r'(?<![가-힣A-Za-z0-9_])우리(?:가|는|도|를|에게|한테)(?=\s|[,.!?]|$)', ' ', text)
        if customer_text != text:
            from backend.intent.scope_extraction import extract_customer_scope
            from backend.query_filters import add_filter_clause
            customer = extract_customer_scope(customer_text, normalize_for_match(customer_text))
            customer_keys = ('customer_key', 'customer_aliases', 'customer_exact')
            if any(customer[key] != constraints.get(key) for key in customer_keys):
                constraints = deepcopy(constraints)
                for key in customer_keys:
                    constraints[key] = customer[key]
                constraints['filter_clauses'] = [c for c in constraints.get('filter_clauses', [])
                                                 if c['field'] != 'customer' or c['operator'] not in {'equals', 'in'}]
                if customer['customer_key']:
                    add_filter_clause(constraints['filter_clauses'], 'customer', 'equals',
                                      customer['customer_exact'] or customer['customer_key'])
    date_text = explanation_date_text(text)
    if date_text != text and not constraints.get("inherited_from_audit_id"):
        from backend.query_planning import extract_created_at_range
        from backend.query_filters import add_filter_clause
        constraints = deepcopy(constraints)
        constraints["created_at"] = extract_created_at_range(date_text)
        constraints["filter_clauses"] = [c for c in constraints.get("filter_clauses", []) if c["field"] != "created_at"]
        if constraints["created_at"]:
            period = constraints["created_at"]
            add_filter_clause(constraints["filter_clauses"], "created_at", "between", [period["from"], period["to"]])
    scope_text = explanation_filter_text(text)
    if constraints.get("inherited_from_audit_id") or re.search(
        r"(?:상태\s*[:=]|(?:종료|해결|검토|미종료|미해결)\s*(?:상태\s*)?(?:된|인|중인)?\s*이슈\s*(?:중|만)|제외|빼고)", scope_text
    ):
        return constraints
    masked = re.sub(r"(?:(?:HTS|MTS|앱|프로그램|프로세스)(?:가|이)?\s*종료(?:된|되는|됨)|검토\s*이슈|종료\s*(?:상태|사유)|(?:마지막\s*)?종료\s*[·,와]\s*재접수\s*조건|해결\s*(?:방안|방법)|수정\s*완료\s*여부|(?:개발|수정)\s*완료인지도|(?:실제\s*)?(?:버전\s*)?적용\s*완료와\s*계획|(?:개발|수정)\s*완료된\s*거야|(?:개발|수정)\s*완료로\s*단정하지\s*말고|(?:QC|검수|고객사?\s*전달)\s*완료(?:야|인지|인가|냐)?|예정과\s*완료를\s*구분|(?:고객\s*)?(?:파일|원문|노트|자료|로그)(?:에서|로)?\s*확인된\s*(?:것|내용))", " ", scope_text)
    # 기능 조작 중 프로그램이 종료되는 장애를 설명하는 문구는 이슈 종료 상태가 아니다.
    masked = re.sub(r"종료(?:되는|되던|된)\s*(?:원인|증상|현상|오류|이유)", " ", masked)
    if len(constraints.get('issue_ids') or []) == 1:
        # 지정한 이슈의 배포 경위·잔여 기능을 설명하는 문구는 검색 상태가 아니다.
        # 이슈 집합의 '남은 항목'이나 명시·상속한 상태 조건에는 적용하지 않는다.
        masked = re.sub(r'(?:개발|수정)\s*완료\s*상태(?:이고|이며|라서)|'
                        r'(?:정상\s*)?오픈\s*(?:메일|안내|공지)|남은\s*(?:항목|기능|문제)', ' ', masked)
    drop_model_open = not _model_open_evidence_describes_work_state(constraints)
    if masked == text and not drop_model_open:
        return constraints
    status = extract_status_scope(masked, normalize_for_match(masked), constraints.get("people") or {})
    # 명시적인 상태 범위나 상속 조건이 있으면 기존 조건 해석이 우선한다.
    if re.search(r"(?:상태\s*[:=]|(?:종료|해결|검토|미종료|미해결)\s*(?:상태\s*)?(?:된|인|중인)?\s*이슈\s*(?:중|만)|제외|빼고)", masked):
        return constraints
    result = deepcopy(constraints)
    result.update(status)
    if drop_model_open:
        result.pop('status_resolution', None)
    result["filter_clauses"] = [c for c in result.get("filter_clauses", []) if c["field"] != "status"]
    from backend.query_filters import add_filter_clause
    if status["statuses"]:
        add_filter_clause(result["filter_clauses"], "status", "in", status["statuses"])
    if status["excluded_statuses"]:
        add_filter_clause(result["filter_clauses"], "status", "not_in", status["excluded_statuses"])
    if (result.get('mantis_team_scope') == 'default'
            and not result.get('inherited_from_audit_id')
            and not result.get('statuses') and not result.get('excluded_statuses')):
        for key in ('mantis_team_members', 'mantis_team_label', 'mantis_team_scope'):
            result.pop(key, None)
    return result
