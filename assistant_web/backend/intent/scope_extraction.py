"""질문에서 고객사·플랫폼·담당자·상태 범위와 공통 필터를 추출한다."""

import re
from typing import Any, Dict, List

from backend.common import (
    derive_customer_aliases_from_project,
    normalize_for_match,
    text_matches_customer_aliases,
)
from backend.config import PLATFORM_ALIASES, STATUS_ALIASES
from backend.intent.constants import SOURCE_NAME_ALIASES
from backend.intent.dates import extract_date_field_constraints, remove_date_field_phrases
from backend.intent.entities import (
    expression_is_excluded,
    extract_excluded_issue_ids,
    extract_explicit_keyword_filter_clauses,
    extract_issue_ids,
    extract_people_constraints,
    extract_screen_numbers,
    extract_status_constraints,
    extract_unknown_customers,
    get_customer_alias_groups,
)
from backend.intent.people import drop_people, empty_people
from backend.query_filters import add_filter_clause
from backend.query_planning import extract_created_at_range, extract_group_by


def empty_query_constraints() -> Dict[str, Any]:
    return {
        "customer_key": None,
        "customer_aliases": [],
        "platform_key": None,
        "platform_aliases": [],
        "people": empty_people(),
        "statuses": [],
        "issue_ids": [],
        "screen_numbers": [],
        "date_fields": {},
        "created_at": None,
    }


def extract_customer_scope(text: str, normalized_text: str) -> Dict[str, Any]:
    # DB03_작업 같은 Notion DB 식별자를 DB금융투자의 약칭으로 해석하지 않는다.
    customer_text = re.sub(r"(?<![A-Za-z0-9_])DB\d{2}_[가-힣A-Za-z0-9_]+", " ", text, flags=re.IGNORECASE)
    if customer_text != text:
        text, normalized_text = customer_text, normalize_for_match(customer_text)
    customer_matches: List[tuple[int, str, List[str]]] = []
    excluded_matches: List[tuple[int, str, List[str]]] = []
    for aliases in get_customer_alias_groups():
        for alias in aliases:
            normalized_alias = normalize_for_match(alias)
            if normalized_alias and text_matches_customer_aliases(text, [alias]):
                target = excluded_matches if expression_is_excluded(text, alias) else customer_matches
                target.append((len(normalized_alias), alias, aliases))

    # 프로젝트 코드와 표시명을 함께 적은 표기(예: ``138Meritz(메리츠)``)는
    # 현재 스냅샷의 동적 명부가 비어 있어도 질의 자체에서 정확한 고객 범위를
    # 복원할 수 있어야 한다. 동적 별칭 목록에 의존하면 테스트·초기 구동 시
    # 동일한 명시 표기가 일반 별칭으로만 남아 ``customer_exact``가 사라진다.
    for match in re.finditer(
        r"(?<![A-Za-z0-9])(\d+[A-Za-z][A-Za-z0-9_-]*\([^()\s]{1,40}\))",
        text,
    ):
        exact_project = match.group(1)
        normalized_exact = normalize_for_match(exact_project)
        if expression_is_excluded(text, exact_project):
            excluded_matches.append((len(normalized_exact), exact_project, [exact_project]))
        else:
            aliases = list(dict.fromkeys([exact_project, *derive_customer_aliases_from_project(exact_project)]))
            customer_matches.append((len(normalized_exact), exact_project, aliases))

    if not any(
        normalize_for_match(marker) in normalized_text for marker in ("999Mantis", "903모바일", "고객사", "프로젝트")
    ):
        customer_matches = [
            item for item in customer_matches if normalize_for_match(item[1]) not in SOURCE_NAME_ALIASES
        ]

    # 수량 '이슈 하나'는 고객사 후보를 고르기 전에 제외한다. 이후에 지우면
    # 더 짧은 실제 고객사 NH가 하나에 밀려 함께 사라진다.
    hana_count_expression = bool(
        re.search(
            r"(?:이슈|건|항목|결과|문서|페이지)\s*(?:을|를)?\s*하나\s*(?:만|씩|를|가|는|$)",
            text,
        )
    )
    if hana_count_expression:
        customer_matches = [item for item in customer_matches if normalize_for_match(item[1]) != "하나"]

    # 긴 제외 별칭 안의 짧은 별칭(NH증권/NH)을 포함 조건으로 되살리지 않는다.
    excluded_aliases = {
        normalize_for_match(alias)
        for _, _, group in excluded_matches for alias in group
    }
    customer_matches = [
        item for item in customer_matches if normalize_for_match(item[1]) not in excluded_aliases
    ]
    customer_key = None
    customer_aliases: List[str] = []
    customer_exact = None
    if customer_matches:
        _, matched_alias, matched_group = max(customer_matches, key=lambda item: item[0])
        if normalize_for_match(matched_alias) == "서버자동주문" and not re.search(
            r"(?:고객사|프로젝트)\s*[:=]?\s*서버\s*자동주문", text,
        ):
            # '한화 서버 자동주문 장애'의 서버자동주문은 제품명이다.
            # 별도 PATS 프로젝트 별칭이 더 길다는 이유로 명시한 증권사를
            # 덮어쓰면 고객사=한화인 문서가 로컬 검사에서 탈락한다.
            broker_matches = [
                item for item in customer_matches
                if any(str(alias).endswith(("증권", "금융투자")) for alias in item[2])
            ]
            if broker_matches:
                _, matched_alias, matched_group = max(broker_matches, key=lambda item: item[0])
        customer_key = matched_alias
        customer_aliases = matched_group
        if re.match(r"^\d+", matched_alias) and "(" in matched_alias and ")" in matched_alias:
            customer_exact = matched_alias

    if extract_group_by(text) == "customer":
        customer_key, customer_aliases, customer_exact = None, [], None

    if customer_aliases and any(normalize_for_match(alias) == "krx" for alias in customer_aliases):
        krx_domain_query = any(
            normalize_for_match(marker) in normalized_text
            for marker in ("제도개선", "거래시간", "시장제도", "한국거래소", "장시간")
        )
        explicit_customer_context = any(
            normalize_for_match(marker) in normalized_text
            for marker in ("고객사", "프로젝트", "119krx", "라우스", "캄보디아")
        )
        if krx_domain_query and not explicit_customer_context:
            customer_key, customer_aliases, customer_exact = None, [], None

    return {
        "customer_key": customer_key,
        "customer_aliases": customer_aliases,
        "customer_exact": customer_exact,
        "excluded_customer_matches": excluded_matches,
    }


def extract_platform_scope(text: str, normalized_text: str) -> Dict[str, Any]:
    platform_key = None
    platform_aliases: List[str] = []
    excluded_keys: List[str] = []
    excluded_aliases: List[str] = []
    for key, aliases in PLATFORM_ALIASES.items():
        candidates = [key, *aliases]
        if any(normalize_for_match(alias) in normalized_text for alias in candidates):
            matched_alias = next(alias for alias in candidates if normalize_for_match(alias) in normalized_text)
            if expression_is_excluded(text, matched_alias):
                excluded_keys.append(key)
                excluded_aliases.extend(aliases)
            elif not platform_key:
                platform_key = key
                platform_aliases = aliases
    return {
        "platform_key": platform_key,
        "platform_aliases": platform_aliases,
        "excluded_platform_keys": list(dict.fromkeys(excluded_keys)),
        "excluded_platform_aliases": list(dict.fromkeys(excluded_aliases)),
    }


def extract_people_scope(text: str, customer_scope: Dict[str, Any]) -> Dict[str, Any]:
    people = extract_people_constraints(remove_date_field_phrases(text))
    unassigned_people = bool(
        re.search(
            r"담당자?\s*(?:가|는|를|도)?\s*"
            r"(?:없는|없이|없음|미지정|미배정|안\s*정해진|지정\s*안\s*된|할당\s*안\s*된|배정\s*안\s*된)",
            text,
        )
    )
    customer_aliases = customer_scope["customer_aliases"]
    if people.get("names") and customer_aliases:
        customer_alias_norms = {normalize_for_match(alias) for alias in customer_aliases}
        overlapping_names = [name for name in people.get("names") or [] if normalize_for_match(name) in customer_alias_norms]
        explicit_handler = bool(re.search(r"(?:담당자|담당)\s*[:=]?\s*", text, re.IGNORECASE))
        if overlapping_names and explicit_handler and people.get("role") == "handler":
            customer_scope.update(
                customer_key=None,
                customer_aliases=[],
                customer_exact=None,
            )
        else:
            people = drop_people(
                people,
                overlapping_names,
            )
    return {"people": people, "unassigned_people": unassigned_people}


def extract_status_scope(
    text: str,
    normalized_text: str,
    people: Dict[str, Any],
) -> Dict[str, List[str]]:
    statuses = extract_status_constraints(text)
    excluded_statuses = [
        status_key
        for status_key, aliases in STATUS_ALIASES.items()
        if any(
            normalize_for_match(alias) in normalized_text and expression_is_excluded(text, alias)
            for alias in [status_key, *aliases]
        )
    ]
    if excluded_statuses:
        statuses = [status for status in statuses if status not in excluded_statuses]
    if people.get("names") and people.get("role") == "handler":
        statuses = [status for status in statuses if status != "assigned"]
    return {"statuses": statuses, "excluded_statuses": excluded_statuses}


def extract_scope_constraints(text: str) -> Dict[str, Any]:
    keyword_text = text
    if re.search(r"제목|본문|내용|키워드", text):
        text = re.sub(r'''["'“‘][^"'”’]+["'”’]''', " ", text)
    normalized_text = normalize_for_match(text)
    # 명시한 기술 계정 안의 고객사 약칭(DRFN_NEXT → DRFN)을 고객 조건으로 삼지 않는다.
    customer_text = re.sub(r"(?:담당자|요청자)\s+[A-Za-z][A-Za-z0-9_]+", " ", text)
    customer = extract_customer_scope(customer_text, normalize_for_match(customer_text))
    platform = extract_platform_scope(text, normalized_text)
    people_scope = extract_people_scope(text, customer)
    status_scope = extract_status_scope(text, normalized_text, people_scope["people"])
    excluded_issue_ids = extract_excluded_issue_ids(text)
    issue_ids = [issue_id for issue_id in extract_issue_ids(text) if issue_id not in excluded_issue_ids]
    screen_numbers = extract_screen_numbers(text)
    created_at = extract_created_at_range(text)
    filter_clauses: List[Dict[str, Any]] = []

    if customer["customer_key"]:
        add_filter_clause(filter_clauses, "customer", "equals", customer["customer_key"])
    for _, excluded_customer, _ in sorted(customer["excluded_customer_matches"], reverse=True):
        add_filter_clause(filter_clauses, "customer", "not_equals", excluded_customer)
    if platform["platform_key"]:
        add_filter_clause(filter_clauses, "platform", "equals", platform["platform_key"])
    for excluded_platform in platform["excluded_platform_keys"]:
        add_filter_clause(filter_clauses, "platform", "not_equals", excluded_platform)
    if status_scope["statuses"]:
        add_filter_clause(filter_clauses, "status", "in", status_scope["statuses"])
    if status_scope["excluded_statuses"]:
        add_filter_clause(
            filter_clauses,
            "status",
            "not_in",
            status_scope["excluded_statuses"],
        )
    if issue_ids:
        add_filter_clause(filter_clauses, "issue_id", "in", issue_ids)
    if excluded_issue_ids:
        add_filter_clause(filter_clauses, "issue_id", "not_in", excluded_issue_ids)
    if screen_numbers:
        add_filter_clause(filter_clauses, "screen_number", "in", screen_numbers)
    if created_at:
        add_filter_clause(
            filter_clauses,
            "created_at",
            "between",
            [created_at["from"], created_at["to"]],
        )
    filter_clauses.extend(extract_explicit_keyword_filter_clauses(keyword_text))

    excluded_customer_matches = customer.pop("excluded_customer_matches")
    return {
        **customer,
        **platform,
        **people_scope,
        **status_scope,
        "excluded_customer_keys": list(dict.fromkeys(match[1] for match in excluded_customer_matches)),
        "excluded_customer_aliases": list(
            dict.fromkeys(alias for _, _, aliases in excluded_customer_matches for alias in aliases)
        ),
        "issue_ids": issue_ids,
        "exclude_issue_ids": excluded_issue_ids,
        "screen_numbers": screen_numbers,
        "date_fields": extract_date_field_constraints(text),
        "created_at": created_at,
        "unknown_customers": ([] if customer["customer_key"] else extract_unknown_customers(text)),
        "filter_clauses": filter_clauses,
    }


__all__ = [
    "empty_query_constraints",
    "extract_customer_scope",
    "extract_platform_scope",
    "extract_people_scope",
    "extract_status_scope",
    "extract_scope_constraints",
]
