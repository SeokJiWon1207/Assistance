"""검색 조건, 데이터 소스, 결과 제한과 모델 의도 계획을 구성한다."""

import json
import os
import re
from datetime import date
from typing import Any, Dict, List, Optional

import requests

from backend.common import (
    normalize_for_match,
    normalize_search_token,
    text_matches_customer_aliases,
)
from backend.config import (
    PLATFORM_ALIASES,
    STATUS_ALIASES,
    STOPWORDS,
)
from backend.intent.constants import (
    NOTION_QUERY_STOPWORDS,
    SOURCE_NAME_ALIASES,
)
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
    is_simple_greeting,
)
from backend.intent.people import (
    drop_people,
    empty_people,
    grounded_model_people,
)
from backend.intent.search_terms import (
    build_keyword_terms,
    build_similar_issue_search_text,
    expand_compound_search_term,
    extract_limit,
    extract_requested_limit,
    resolve_result_limit,
    split_project_search_terms,
    term_is_covered_by_constraints,
    wants_all_results,
    wants_similar_issue_search,
)
from backend.intent.source_router import (
    SourceAuthority,
    rule_based_search_intent,
    should_run_notion_search,
    should_run_retrieval,
)
from backend.llm import extract_model_content, post_model_chat
from backend.query_filters import add_filter_clause, normalize_filter_clauses
from backend.query_planning import extract_created_at_range, extract_group_by, infer_operation


def extract_query_constraints(text: str) -> Dict[str, Any]:
    # "하이"는 하이투자증권의 현업 약칭이기도 하지만, 단독 입력은 인사로 취급한다.
    # "하이 이슈 보여줘"처럼 검색 의도가 붙은 문장은 기존 고객사 별칭 검색을 유지한다.
    if is_simple_greeting(text):
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
    normalized_text = normalize_for_match(text)
    customer_key = None
    customer_aliases: List[str] = []
    customer_exact = None
    customer_matches: List[tuple[int, str, List[str]]] = []
    excluded_customer_matches: List[tuple[int, str, List[str]]] = []
    for aliases in get_customer_alias_groups():
        for alias in aliases:
            normalized_alias = normalize_for_match(alias)
            if normalized_alias and text_matches_customer_aliases(text, [alias]):
                target = excluded_customer_matches if expression_is_excluded(text, alias) else customer_matches
                target.append((len(normalized_alias), alias, aliases))

    # "Mantis와 Notion에서 찾아줘"의 Mantis/Notion은 조회할 데이터 소스 이름이지
    # 고객사가 아니다. 다만 실제 프로젝트 목록에 999Mantis가 있어 고객사 별칭으로
    # 잡히고, 별칭이 길어 진짜 고객사(KB증권 등)까지 밀어내 버린다.
    # 프로젝트를 명시적으로 가리킬 때만 고객사 후보로 남긴다.
    if not any(
        normalize_for_match(marker) in normalized_text
        for marker in ("999Mantis", "903모바일", "고객사", "프로젝트")
    ):
        customer_matches = [
            item
            for item in customer_matches
            if normalize_for_match(item[1]) not in SOURCE_NAME_ALIASES
        ]

    if customer_matches:
        _, matched_alias, matched_group = max(customer_matches, key=lambda item: item[0])
        customer_key = matched_alias
        customer_aliases = matched_group
        if re.match(r"^\d+", matched_alias) and "(" in matched_alias and ")" in matched_alias:
            customer_exact = matched_alias

    # "고객사별 ... 하나씩"에서 "하나"는 고객사명이 아니라 그룹별 개수를
    # 나타내는 말이다. 고객사별/프로젝트별 요청은 단일 고객사 필터로 좁히지 않는다.
    if extract_group_by(text) == "customer":
        customer_key = None
        customer_aliases = []
        customer_exact = None

    # "최근 이슈 하나만"의 하나는 수량이지 하나금융의 짧은 별칭이 아니다.
    # 명시적인 "하나증권/하나금융"이나 "하나 이슈"는 계속 고객사로 인식한다.
    hana_count_expression = bool(
        re.search(
            r"(?:이슈|건|항목|결과|문서|페이지)\s*(?:을|를)?\s*하나\s*(?:만|씩|를|가|는|$)",
            text,
        )
    )
    if customer_key and normalize_for_match(customer_key) == "하나" and hana_count_expression:
        customer_key = None
        customer_aliases = []
        customer_exact = None

    # KRX is both a legacy project/customer alias and a market-domain keyword.
    # In questions such as "KRX 제도개선" it describes the subject, not 119KRX.
    # Keep the customer constraint only when the user explicitly points to a
    # project/customer or uses a project-specific alias.
    if customer_aliases and any(
        normalize_for_match(alias) == "krx" for alias in customer_aliases
    ):
        krx_domain_query = any(
            normalize_for_match(marker) in normalized_text
            for marker in ("제도개선", "거래시간", "시장제도", "한국거래소", "장시간")
        )
        explicit_customer_context = any(
            normalize_for_match(marker) in normalized_text
            for marker in ("고객사", "프로젝트", "119krx", "라우스", "캄보디아")
        )
        if krx_domain_query and not explicit_customer_context:
            customer_key = None
            customer_aliases = []
            customer_exact = None

    platform_key = None
    platform_aliases: List[str] = []
    excluded_platform_keys: List[str] = []
    excluded_platform_aliases: List[str] = []
    for key, aliases in PLATFORM_ALIASES.items():
        if any(normalize_for_match(alias) in normalized_text for alias in [key, *aliases]):
            matched_alias = next(
                alias
                for alias in [key, *aliases]
                if normalize_for_match(alias) in normalized_text
            )
            if expression_is_excluded(text, matched_alias):
                excluded_platform_keys.append(key)
                excluded_platform_aliases.extend(aliases)
            elif not platform_key:
                platform_key = key
                platform_aliases = aliases

    people = extract_people_constraints(remove_date_field_phrases(text))
    # "담당자가 없는 건만", "담당자 미지정"처럼 담당자가 비어 있는 이슈만 찾는 요청.
    unassigned_people = bool(
        re.search(
            r"담당자?\s*(?:가|는|를|도)?\s*"
            r"(?:없는|없이|없음|미지정|미배정|안\s*정해진|지정\s*안\s*된|할당\s*안\s*된|배정\s*안\s*된)",
            text,
        )
    )
    if people.get("names") and customer_aliases:
        explicit_handler_context = bool(re.search(r"(?:담당자|담당)\s*[:=]?\s*", text, re.IGNORECASE))
        if explicit_handler_context and people.get("role") == "handler":
            customer_key = None
            customer_aliases = []
            customer_exact = None
        else:
            customer_alias_norms = {normalize_for_match(alias) for alias in customer_aliases}
            people = drop_people(
                people,
                [
                    name
                    for name in people.get("names") or []
                    if normalize_for_match(name) in customer_alias_norms
                ],
            )

    statuses = extract_status_constraints(text)
    excluded_statuses: List[str] = []
    for status_key, aliases in STATUS_ALIASES.items():
        if any(
            normalize_for_match(alias) in normalized_text and expression_is_excluded(text, alias)
            for alias in [status_key, *aliases]
        ):
            excluded_statuses.append(status_key)
    if excluded_statuses:
        statuses = [status for status in statuses if status not in excluded_statuses]
    if people.get("names") and people.get("role") == "handler":
        statuses = [status for status in statuses if status != "assigned"]

    excluded_issue_ids = extract_excluded_issue_ids(text)
    issue_ids = [issue_id for issue_id in extract_issue_ids(text) if issue_id not in excluded_issue_ids]

    filter_clauses: List[Dict[str, Any]] = []
    if customer_key:
        add_filter_clause(filter_clauses, "customer", "equals", customer_key)
    for _, excluded_customer, _ in sorted(excluded_customer_matches, reverse=True):
        add_filter_clause(filter_clauses, "customer", "not_equals", excluded_customer)
    if platform_key:
        add_filter_clause(filter_clauses, "platform", "equals", platform_key)
    for excluded_platform in excluded_platform_keys:
        add_filter_clause(filter_clauses, "platform", "not_equals", excluded_platform)
    if statuses:
        add_filter_clause(filter_clauses, "status", "in", statuses)
    if excluded_statuses:
        add_filter_clause(filter_clauses, "status", "not_in", excluded_statuses)
    if issue_ids:
        add_filter_clause(filter_clauses, "issue_id", "in", issue_ids)
    if excluded_issue_ids:
        add_filter_clause(filter_clauses, "issue_id", "not_in", excluded_issue_ids)
    screen_numbers = extract_screen_numbers(text)
    if screen_numbers:
        add_filter_clause(filter_clauses, "screen_number", "in", screen_numbers)
    created_at = extract_created_at_range(text)
    if created_at:
        add_filter_clause(
            filter_clauses,
            "created_at",
            "between",
            [created_at["from"], created_at["to"]],
        )
    filter_clauses.extend(extract_explicit_keyword_filter_clauses(text))

    return {
        "customer_key": customer_key,
        "customer_aliases": customer_aliases,
        "customer_exact": customer_exact,
        "platform_key": platform_key,
        "platform_aliases": platform_aliases,
        "excluded_platform_keys": list(dict.fromkeys(excluded_platform_keys)),
        "excluded_platform_aliases": list(dict.fromkeys(excluded_platform_aliases)),
        "excluded_customer_keys": list(
            dict.fromkeys(match[1] for match in excluded_customer_matches)
        ),
        "excluded_customer_aliases": list(
            dict.fromkeys(alias for _, _, aliases in excluded_customer_matches for alias in aliases)
        ),
        "people": people,
        "statuses": statuses,
        "excluded_statuses": excluded_statuses,
        "issue_ids": issue_ids,
        "exclude_issue_ids": excluded_issue_ids,
        "screen_numbers": screen_numbers,
        "date_fields": extract_date_field_constraints(text),
        "created_at": created_at,
        "unassigned_people": unassigned_people,
        # 고객사를 가리켰지만 별칭 목록에 없는 이름. customer_key가 비어 있을 때만
        # 의미가 있으며, 조건 없는 전체 검색으로 흘러가는 것을 막는 근거가 된다.
        "unknown_customers": [] if customer_key else extract_unknown_customers(text),
        "filter_clauses": filter_clauses,
    }


def has_targeting_constraint(constraints: Dict[str, Any]) -> bool:
    """검색 대상 자체를 특정하는 조건이 있는지 본다.

    상태만 있는 경우는 제외한다. "핵심 해결 내용을 요약해줘"의 '해결'처럼
    본문 낱말이 상태로 잡히는 일이 잦아, 상태 유무로는 독립 질문인지
    가려낼 수 없다."""
    return bool(
        constraints.get("customer_key")
        or constraints.get("platform_key")
        or constraints.get("issue_ids")
        or constraints.get("screen_numbers")
        or constraints.get("people", {}).get("names")
        or constraints.get("created_at")
    )


def has_constraint(constraints: Dict[str, Any]) -> bool:
    return bool(
        constraints.get("customer_key")
        or constraints.get("platform_key")
        or constraints.get("excluded_customer_keys")
        or constraints.get("excluded_platform_keys")
        or constraints.get("statuses")
        or constraints.get("issue_ids")
        or constraints.get("screen_numbers")
        or constraints.get("date_fields")
        or constraints.get("created_at")
        or constraints.get("people", {}).get("names")
        or constraints.get("unassigned_people")
        or constraints.get("filter_clauses")
    )


def legacy_filters_to_clauses(filters: Any) -> List[Dict[str, Any]]:
    """기존 ``filters`` 객체를 공통 조건식으로 변환한다."""
    if isinstance(filters, list):
        return normalize_filter_clauses(filters)
    if not isinstance(filters, dict):
        return []
    clauses: List[Dict[str, Any]] = []
    for field, legacy_key in (
        ("customer", "customer"),
        ("platform", "platform"),
        ("owner", "people"),
        ("issue_id", "issue_ids"),
        ("screen_number", "screen_numbers"),
    ):
        value = filters.get(legacy_key)
        if not value:
            continue
        if legacy_key == "people" and isinstance(value, dict):
            value = value.get("names") or []
        add_filter_clause(
            clauses,
            field,
            "in" if isinstance(value, list) else "equals",
            value,
        )
    if filters.get("statuses"):
        add_filter_clause(clauses, "status", "in", filters["statuses"])
    created_at = filters.get("created_at")
    if isinstance(created_at, dict) and created_at.get("from") and created_at.get("to"):
        add_filter_clause(
            clauses,
            "created_at",
            "between",
            [created_at["from"], created_at["to"]],
        )
    return clauses


def merge_query_plan_constraints(
    constraints: Dict[str, Any],
    query_plan: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Apply validated planner filters without trusting arbitrary model values."""
    merged = {
        **constraints,
        "customer_aliases": list(constraints.get("customer_aliases") or []),
        "platform_aliases": list(constraints.get("platform_aliases") or []),
        "excluded_platform_keys": list(constraints.get("excluded_platform_keys") or []),
        "excluded_platform_aliases": list(constraints.get("excluded_platform_aliases") or []),
        "excluded_customer_keys": list(constraints.get("excluded_customer_keys") or []),
        "excluded_customer_aliases": list(constraints.get("excluded_customer_aliases") or []),
        "excluded_statuses": list(constraints.get("excluded_statuses") or []),
        "people": dict(constraints.get("people") or empty_people()),
        "statuses": list(constraints.get("statuses") or []),
        "issue_ids": list(constraints.get("issue_ids") or []),
        "exclude_issue_ids": list(constraints.get("exclude_issue_ids") or []),
        "screen_numbers": list(constraints.get("screen_numbers") or []),
        "date_fields": dict(constraints.get("date_fields") or {}),
        "filter_clauses": normalize_filter_clauses(constraints.get("filter_clauses") or []),
    }
    filters = (query_plan or {}).get("filters") or {}

    planned_clauses = normalize_filter_clauses((query_plan or {}).get("filter_clauses") or [])
    planned_clauses.extend(
        clause for clause in legacy_filters_to_clauses(filters) if clause not in planned_clauses
    )
    for clause in planned_clauses:
        field, operator, value = clause["field"], clause["operator"], clause.get("value")
        values = value if isinstance(value, list) else [value]
        grounded_people = None
        if field in {"owner", "reporter"} and operator in {"equals", "in"}:
            grounded_people = grounded_model_people(
                str((query_plan or {}).get("grounding_text") or ""),
                merged["people"],
                [str(item) for item in values],
                "handler" if field == "owner" else "reporter",
            )
            accepted = {
                normalize_for_match(name)
                for name in grounded_people.get("names") or []
            }
            accepted_values = [item for item in values if normalize_for_match(str(item)) in accepted]
            if not accepted_values:
                continue
            values = accepted_values
            clause = {**clause, "value": accepted_values if isinstance(value, list) else accepted_values[0]}
        if clause not in merged["filter_clauses"]:
            merged["filter_clauses"].append(clause)
        if field == "platform":
            for planned_value in values:
                key = normalize_for_match(str(planned_value))
                matched = next(
                    (
                        (platform_key, aliases)
                        for platform_key, aliases in PLATFORM_ALIASES.items()
                        if key in {normalize_for_match(platform_key), *(normalize_for_match(alias) for alias in aliases)}
                    ),
                    None,
                )
                if not matched:
                    continue
                platform_key, aliases = matched
                if operator in {"not_equals", "not_in"}:
                    merged["excluded_platform_keys"].append(platform_key)
                    merged["excluded_platform_aliases"].extend(aliases)
                    if merged.get("platform_key") == platform_key:
                        merged["platform_key"], merged["platform_aliases"] = None, []
                elif operator in {"equals", "in"} and not merged.get("platform_key"):
                    merged["platform_key"], merged["platform_aliases"] = platform_key, list(aliases)
        elif field == "customer":
            for planned_value in values:
                planned_customer = extract_query_constraints(str(planned_value))
                if operator in {"not_equals", "not_in"}:
                    merged["excluded_customer_keys"].append(str(planned_value))
                    merged["excluded_customer_aliases"].extend(planned_customer.get("customer_aliases") or [str(planned_value)])
                elif operator in {"equals", "in"} and not merged.get("customer_key"):
                    if planned_customer.get("customer_key"):
                        for key_name in ("customer_key", "customer_aliases", "customer_exact"):
                            merged[key_name] = planned_customer.get(key_name)
        elif field == "status" and operator in {"not_equals", "not_in"}:
            merged["excluded_statuses"].extend(str(item) for item in values)
        elif field == "status" and operator in {"equals", "in"} and not merged.get("statuses"):
            merged["statuses"] = [str(item) for item in values]
        elif field in {"owner", "reporter"} and operator in {"equals", "in"}:
            merged["people"] = grounded_people or merged["people"]
        elif field == "issue_id":
            numeric_values = [int(str(item)) for item in values if str(item).isdigit()]
            if operator in {"not_equals", "not_in"}:
                merged["exclude_issue_ids"].extend(numeric_values)
            elif operator in {"equals", "in"}:
                merged["issue_ids"] = list(dict.fromkeys([*merged.get("issue_ids", []), *numeric_values]))
        elif field == "screen_number" and operator in {"equals", "in"}:
            merged["screen_numbers"] = list(
                dict.fromkeys([*merged.get("screen_numbers", []), *(str(item) for item in values)])
            )
        elif field == "created_at" and operator == "between" and len(values) == 2:
            start, end = str(values[0]), str(values[1])
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) and start <= end:
                merged["created_at"] = {"from": start, "to": end, "precision": "range"}

    if isinstance(filters, dict) and not merged.get("customer_key") and filters.get("customer"):
        planned_customer = extract_query_constraints(str(filters["customer"]))
        if planned_customer.get("customer_key"):
            for key in ("customer_key", "customer_aliases", "customer_exact"):
                merged[key] = planned_customer.get(key)

    if isinstance(filters, dict) and not merged.get("platform_key") and filters.get("platform"):
        planned_platform = extract_query_constraints(str(filters["platform"]))
        if planned_platform.get("platform_key"):
            merged["platform_key"] = planned_platform["platform_key"]
            merged["platform_aliases"] = planned_platform["platform_aliases"]

    created_at = filters.get("created_at") if isinstance(filters, dict) else None
    if isinstance(created_at, dict):
        start, end = str(created_at.get("from", "")), str(created_at.get("to", ""))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) and start <= end:
            merged["created_at"] = {
                "from": start,
                "to": end,
                "precision": str(created_at.get("precision") or "range"),
            }
    for key in (
        "excluded_platform_keys",
        "excluded_platform_aliases",
        "excluded_customer_keys",
        "excluded_customer_aliases",
        "excluded_statuses",
    ):
        merged[key] = list(dict.fromkeys(merged.get(key) or []))
    return merged



def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip(), flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def plan_search_intent_with_model(
    question: str,
    model: Dict[str, Any],
    api_key: str,
) -> Dict[str, Any]:
    fallback = {
        "source": "general",
        "operation": infer_operation(question),
        "filters": {},
        "terms": [],
        "reason": "planner_fallback",
        "planned": False,
    }
    today_str = date.today().isoformat()
    prompt = (
        f"오늘은 {today_str}이다. "
        "사용자 질문을 사내 도구 실행 계획 JSON으로만 변환해라. "
        "source는 notion, mantis, both, general 중 하나다. "
        "operation은 search, count, get, aggregate 중 하나다. '몇 개/몇 건/건수'는 count이고, "
        "'N개 보여줘'는 search이며, 합계/총합은 aggregate다. "
        "notion은 팀 공지, 회식, 휴가, 일정, 회의, 계약, 업무 공유, 문서 및 조직 운영 자료다. "
        "mantis는 고객사 소프트웨어 이슈, 오류, 장애, 화면번호, 담당자, 상태, 개발/QC 일정, 공수 및 과거 장애 이력이다. "
        "KRX 제도개선, HTS, MTS, 차트, 조건검색 관련 요청은 별도 언급이 없어도 mantis다. "
        "both는 사용자가 두 데이터 소스를 함께 요구할 때만 사용한다. "
        "general은 사내 데이터 검색이 필요 없는 일반 대화다. "
        "filters는 [{\"field\":필드,\"operator\":연산자,\"value\":값}] 배열이다. "
        "field는 customer, platform, status, owner, reporter, issue_id, screen_number, created_at, "
        "title, keyword, database 중 하나다. operator는 equals, not_equals, in, not_in, contains, "
        "not_contains, exists, not_exists, gte, lte, between 중 하나다. "
        "'빼고/제외/아닌/말고/없는'은 반드시 not_equals, not_in, not_contains, not_exists 중 하나로 표현해라. "
        "예: 'MTS는 빼고'는 {\"field\":\"platform\",\"operator\":\"not_equals\",\"value\":\"mts\"}다. "
        "'종료 안 된/종료되지 않은/미종료'는 {\"field\":\"status\",\"operator\":\"not_equals\",\"value\":\"closed\"}다. "
        "'고객사별로/담당자별로/상태별로/유형별로'처럼 결과를 묶어 달라는 요청은 필터로 만들지 말고 "
        "각각 group_by를 customer/owner/status/type으로 출력해라. "
        "created_at은 등록일 기준이며 from/to를 YYYY-MM-DD로 출력한다. 예를 들어 2025년은 "
        "{\"from\":\"2025-01-01\",\"to\":\"2025-12-31\"}이다. "
        "'오늘/어제/이번 주/지난주/최근 N일'처럼 상대적인 시점도 위 오늘 날짜를 기준으로 "
        "실제 YYYY-MM-DD 범위로 계산해서 넣어라. "
        "terms에는 조사와 요청 표현을 제외한 실제 검색어만 최대 6개 넣어라. "
        "limit은 '5개 보여줘'처럼 표시 개수가 명시된 경우에만 숫자로 넣고 count에는 null을 넣어라. "
        "질문이 작업 공수(공수, 작업량, 투입 시간) 집계나 월별 추이에 관한 것이면 effort 필드를 추가해라. "
        "특정 인물의 공수 합산이면 {\"type\":\"person\",\"person\":\"이름\"}이고, "
        "누가 많이/적게 했는지 순위·비교를 물으면 {\"type\":\"ranking\"}이다. "
        "여러 달에 걸친 변화·흐름을 묻거나 달별로 나눠서 보여 달라고 하면 {\"type\":\"trend\"}다. "
        "'월별/월간/달별 건수', '추이', '추세', '트렌드', '증감', '어떻게 변했는지'가 trend다. "
        "trend는 이슈 건수나 공수를 여러 달에 걸쳐 비교할 때만 쓴다. "
        "특정 한 달이나 한 해의 합계만 묻는다면 trend 대신 person을 쓰거나 effort를 생략해라. "
        "회의록·공지·문서를 달별로 정리해 달라는 요청에는 effort를 넣지 마라. "
        "공수 질문에 인물 이름이 있으면 person, 순위·비교를 물으면 ranking을 반드시 넣어라. "
        "회의록·공지처럼 이슈 집계와 무관한 질문에는 effort를 넣지 마라. "
        "형식1(공수와 무관): {\"source\":\"mantis\",\"operation\":\"count\",\"filters\":["
        "{\"field\":\"customer\",\"operator\":\"equals\",\"value\":\"신한\"},"
        "{\"field\":\"created_at\",\"operator\":\"between\",\"value\":[\"2025-01-01\",\"2025-12-31\"]}],"
        "\"terms\":[],\"limit\":null,"
        "\"sort\":{\"field\":\"created_at\",\"direction\":\"desc\"},\"reason\":\"연도별 고객사 이슈 집계\"} "
        "형식2(월별 추이): {\"source\":\"mantis\",\"operation\":\"aggregate\",\"filters\":[],"
        "\"terms\":[],\"limit\":null,\"effort\":{\"type\":\"trend\"},"
        "\"reason\":\"월별 이슈 유형 추이\"} "
        "형식3(인물 공수): {\"source\":\"mantis\",\"operation\":\"aggregate\",\"filters\":[],"
        "\"terms\":[],\"limit\":null,\"effort\":{\"type\":\"person\",\"person\":\"홍길동\"},"
        "\"reason\":\"인물 공수 합계\"} "
        "형식4(공수 순위): {\"source\":\"mantis\",\"operation\":\"aggregate\",\"filters\":[],"
        "\"terms\":[],\"limit\":null,\"effort\":{\"type\":\"ranking\"},"
        "\"reason\":\"공수 순위 비교\"}"
    )
    payload = {
        "model": model.get("model", model["id"]),
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
        "max_tokens": 300,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        response = post_model_chat(
            model,
            api_key,
            payload,
            timeout=max(5, min(int(os.getenv("INTENT_PLANNER_TIMEOUT_SECONDS", "25")), 45)),
            think=False,
        )
        if response.status_code >= 400:
            return fallback
        data = response.json()
        content = extract_model_content(data)
        parsed = extract_json_object(content)
        if not parsed:
            return fallback
    except (requests.RequestException, ValueError, TypeError):
        return fallback

    source = str(parsed.get("source", "general")).strip().lower()
    if source not in {"notion", "mantis", "both", "general"}:
        source = "general"
    normalized_question = normalize_for_match(question)
    explicit_notion = any(
        normalize_for_match(marker) in normalized_question
        for marker in ("notion", "노션", "노선")
    )
    explicit_mantis = any(
        normalize_for_match(marker) in normalized_question
        for marker in ("mantis", "멘티스", "맨티스")
    )
    if explicit_notion and explicit_mantis:
        source = "both"
        parsed["reason"] = "사용자가 Notion과 Mantis를 모두 명시함"
    elif explicit_notion:
        source = "notion"
        parsed["reason"] = "사용자가 Notion을 검색 대상으로 명시함"
    elif explicit_mantis:
        source = "mantis"
        parsed["reason"] = "사용자가 Mantis를 검색 대상으로 명시함"
    terms = []
    for value in parsed.get("terms", []):
        term = normalize_search_token(str(value).strip())
        if (
            not term
            or len(term) < 2
            or len(term) > 40
            or term in STOPWORDS
            or term in NOTION_QUERY_STOPWORDS
        ):
            continue
        terms.append(term)
    unique_terms = list(dict.fromkeys(terms))[:6]
    notion_status_match = re.search(
        r"결재완료\s*\([^)]+\)|결재완료|결재요청|등록됨|보관됨",
        question,
    )
    if source in {"notion", "both"} and notion_status_match:
        status_term = re.sub(r"\s+", "", notion_status_match.group(0))
        unique_terms = [
            term
            for term in unique_terms
            if normalize_for_match(term) not in normalize_for_match(status_term)
        ]
        unique_terms.append(status_term)
    if len(unique_terms) > 1:
        unique_terms = [term for term in unique_terms if term not in {"일정", "공지", "항목"}]
    return {
        "source": source,
        "operation": str(parsed.get("operation") or parsed.get("intent") or infer_operation(question)).lower(),
        "filters": parsed.get("filters") if isinstance(parsed.get("filters"), (dict, list)) else {},
        "filter_clauses": normalize_filter_clauses(
            parsed.get("filters") if isinstance(parsed.get("filters"), list) else parsed.get("filter_clauses")
        ),
        "sort": parsed.get("sort") if isinstance(parsed.get("sort"), dict) else {},
        "group_by": str(parsed.get("group_by") or "").strip().lower() or None,
        "limit": parsed.get("limit") if isinstance(parsed.get("limit"), int) else None,
        "terms": unique_terms,
        "effort": parsed.get("effort") if isinstance(parsed.get("effort"), dict) else None,
        "reason": str(parsed.get("reason", ""))[:120],
        "confidence": 0.85,
        "planned": True,
    }


__all__ = [
    "SourceAuthority",
    "extract_query_constraints",
    "has_targeting_constraint",
    "has_constraint",
    "legacy_filters_to_clauses",
    "merge_query_plan_constraints",
    "rule_based_search_intent",
    "should_run_retrieval",
    "should_run_notion_search",
    "extract_json_object",
    "plan_search_intent_with_model",
    "term_is_covered_by_constraints",
    "build_keyword_terms",
    "wants_similar_issue_search",
    "split_project_search_terms",
    "expand_compound_search_term",
    "build_similar_issue_search_text",
    "extract_requested_limit",
    "wants_all_results",
    "resolve_result_limit",
    "extract_limit",
]
