"""Notion 제약 추출, 검색, 필터링과 랭킹을 담당한다."""

import os
import re
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import HTTPException

from backend.common import (
    extract_quoted_value,
    is_valid_person_name,
    normalize_for_match,
    normalize_search_token,
    remove_quoted_value,
    strip_korean_particle,
)
from backend.config import (
    NOTION_API_BASE,
    STOPWORDS,
    get_notion_settings as _get_notion_settings,
    notion_post_json as _notion_post_json,
)
from backend.intent.constants import NOTION_QUERY_STOPWORDS
from backend.intent.entities import load_known_people
from backend.notion.client import (
    fetch_notion_database_schema as _fetch_notion_database_schema,
    fetch_notion_database_title as _fetch_notion_database_title,
    fetch_notion_page_blocks as _fetch_notion_page_blocks,
)
from backend.notion.compat import compat_dependency
from backend.notion.constants import (
    NOTION_BODY_SCAN_MAX_PER_DB,
    NOTION_STATUS_QUERY_TERMS,
    NOTION_STATUS_TYPOS,
)
from backend.notion.mapping import normalize_notion_page, resolve_notion_page_owner
from backend.notion.schema import (
    notion_property_schema as _notion_property_schema,
    notion_title_property,
)
from backend.query_filters import normalize_filter_clauses, values_for_clause
from backend.query_planning import extract_group_by


def get_notion_settings() -> Dict[str, Any]:
    return compat_dependency("get_notion_settings", _get_notion_settings)()

def notion_post_json(*args: Any, **kwargs: Any) -> requests.Response:
    return compat_dependency("notion_post_json", _notion_post_json)(*args, **kwargs)

def fetch_notion_database_schema(database_id: str) -> Dict[str, Any]:
    return compat_dependency("fetch_notion_database_schema", _fetch_notion_database_schema)(database_id)

def fetch_notion_database_title(database_id: str) -> str:
    return compat_dependency("fetch_notion_database_title", _fetch_notion_database_title)(database_id)

def fetch_notion_page_blocks(*args: Any, **kwargs: Any) -> str:
    return compat_dependency("fetch_notion_page_blocks", _fetch_notion_page_blocks)(*args, **kwargs)

def notion_property_schema(database_id: str) -> Dict[str, Any]:
    return compat_dependency("notion_property_schema", _notion_property_schema)(database_id)


def correct_notion_status_typo(text: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    """Notion 상태 오타를 바로잡은 텍스트와 (원문, 보정값)을 돌려준다."""
    for wrong, right in NOTION_STATUS_TYPOS.items():
        if wrong in text:
            return text.replace(wrong, right), (wrong, right)
    return text, None


def build_notion_terms(text: str, constraints: Optional[Dict[str, Any]] = None) -> List[str]:
    cleaned = re.sub(r"[#,:;!?()\[\]{}\"']", " ", text)
    cleaned = re.sub(r"\d+\s*(개|건)\s*만?", " ", cleaned)
    notion_stopwords = {
        normalize_for_match(keyword)
        for keyword in {*STOPWORDS, *NOTION_QUERY_STOPWORDS, "관한", "관해서", "관하여"}
    }
    terms: List[str] = []
    for token in cleaned.split():
        normalized = normalize_search_token(token)
        if not normalized or len(normalized) < 2 or normalize_for_match(normalized) in notion_stopwords:
            continue
        if normalized.isdigit():
            continue
        terms.append(normalized)
    normalized_text = normalize_for_match(text)
    if "결재요청" in normalized_text and "결재요청" not in terms:
        terms.append("결재요청")
    if "등록" in normalized_text and "등록됨" not in terms:
        terms.append("등록됨")
    return list(dict.fromkeys(terms))[:8]


def trim_owner_adnominal(candidate: str) -> str:
    """"권한별인"처럼 관형형 어미가 붙어 잡힌 이름을 실제 담당자명으로 다듬는다.

    담당자 필터는 부분 일치로 비교하므로 어미가 남으면 아무 항목과도 맞지
    않는다. 실제 담당자 명단에 있는 어간일 때만 잘라내 오탐을 막는다."""
    name = str(candidate or "").strip()
    handlers, reporters = load_known_people()
    known = set(handlers) | set(reporters)
    if name in known:
        return name
    for suffix in ("인", "이"):
        if name.endswith(suffix) and len(name) > 2 and name[: -len(suffix)] in known:
            return name[: -len(suffix)]
    return name


def inherit_notion_context(recent_texts: List[str]) -> str:
    """후속 질문이 잃어버린 Notion 조건(DB·상태)을 이전 턴에서 물려받는다.

    "노션에서 결재요청 상태인 항목 보여줘" 다음의 "담당자별로 묶어줘"는
    Mantis 쪽 조건 상속만으로는 상태와 소스를 모두 잃는다. 반환한 접두
    텍스트를 질문 앞에 붙이면 기존 조건 추출이 그대로 다시 인식한다.
    """
    if len(recent_texts) < 2:
        return ""
    latest = extract_notion_constraints(recent_texts[0])
    if latest.get("status") or latest.get("exact_title") or latest.get("excluded_statuses"):
        return ""
    for prior in recent_texts[1:]:
        prior_constraints = extract_notion_constraints(prior)
        parts = [
            value
            for value in (
                prior_constraints.get("database_label"),
                prior_constraints.get("status"),
            )
            if value
        ]
        if parts:
            # 소스가 Notion이라는 사실도 함께 물려줘야 Mantis로 새지 않는다.
            return " ".join([*parts, "노션"])
    return ""


def extract_notion_database_label_from_text(text: str) -> Optional[str]:
    normalized_text = normalize_for_match(text)
    if "이슈리스트" in normalized_text:
        return "이슈리스트"
    if "주요이슈" in normalized_text or "주요이슈현황" in normalized_text:
        return "주요이슈"
    return None


def extract_notion_constraints(text: str) -> Dict[str, Any]:
    exact_title = extract_quoted_value(text) or ""
    constraint_text = remove_quoted_value(text) if exact_title else text
    # 인용된 제목은 사용자가 지정한 정확한 문자열이므로 건드리지 않는다.
    constraint_text, status_typo = correct_notion_status_typo(constraint_text)
    normalized = normalize_for_match(constraint_text)
    status = ""
    excluded_statuses: List[str] = []
    for candidate in sorted(NOTION_STATUS_QUERY_TERMS, key=len, reverse=True):
        if normalize_for_match(candidate) in normalized:
            candidate_text = normalize_for_match(candidate)
            if re.search(
                re.escape(candidate_text) + r"(?:은|는|이|가)?(?:아닌|말고|제외|빼고)",
                normalized,
            ):
                excluded_statuses.append(candidate)
            elif not status:
                status = candidate

    group_by = extract_group_by(constraint_text)
    owner_text = re.sub(
        r"(?:담당자|담당)\s*(?:별(?:로)?|기준(?:으)?로|마다)",
        " ",
        constraint_text,
    )
    owner = ""
    # "담당자 미지정 항목만"은 미지정이라는 이름을 찾는 게 아니라 담당자가
    # 비어 있는 항목을 찾는 요청이다.
    unassigned_owner = bool(
        re.search(
            r"담당자?\s*(?:가|는|를|도)?\s*"
            r"(?:없는|없이|없음|미지정|미배정|안\s*정해진|지정\s*안\s*된|할당\s*안\s*된|배정\s*안\s*된)",
            constraint_text,
        )
    )
    owner_match = re.search(
        # 조사 "가"를 함께 소비하지 않으면 "담당자가"가 담당+자가로 쪼개져
        # "자가"가 이름으로 잡힌다.
        r"(?:담당자|담당|owner)\s*(?:가|는|은|를|을|=|:)?\s*([가-힣]{2,4}|[A-Za-z]{2,20})",
        owner_text,
        re.IGNORECASE,
    ) or re.search(
        # "담당자가 권한별인"에서 "자가"처럼 담당자라는 말 자체가 이름으로
        # 잘려 나오지 않도록 앞에 담당이 붙은 경우를 제외한다.
        r"(?<!담당)([가-힣]{2,4}|[A-Za-z]{2,20})\s*(?:님)?(?:이|가|은|는)?\s*(?:담당|소유|작성|등록|할당)",
        owner_text,
    )
    if owner_match and not unassigned_owner:
        candidate = owner_match.group(1)
        # 조사가 붙은 일반 명사가 이름으로 잡히지 않도록 어간까지 검증한다.
        if is_valid_person_name(candidate) and is_valid_person_name(
            strip_korean_particle(candidate)
        ):
            owner = trim_owner_adnominal(candidate)

    return {
        "database_label": extract_notion_database_label_from_text(constraint_text) or "",
        "exact_title": exact_title,
        "status": status,
        "excluded_statuses": excluded_statuses,
        "owner": owner,
        "unassigned_owner": unassigned_owner,
        "status_typo": status_typo,
        "group_by": group_by,
    }


def merge_common_notion_constraints(
    notion_constraints: Dict[str, Any],
    constraints: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """공통 필터 계획을 Notion 검색/후처리 조건으로 연결한다."""
    merged = {
        **notion_constraints,
        "common_filter_clauses": normalize_filter_clauses((constraints or {}).get("filter_clauses") or []),
        "excluded_statuses": list(
            dict.fromkeys(
                [
                    *(notion_constraints.get("excluded_statuses") or []),
                    *((constraints or {}).get("excluded_statuses") or []),
                ]
            )
        ),
    }
    for clause in merged["common_filter_clauses"]:
        values = values_for_clause(clause.get("value"))
        if clause["field"] == "status":
            if clause["operator"] in {"not_equals", "not_in"}:
                merged["excluded_statuses"].extend(values)
            elif clause["operator"] in {"equals", "in"} and values and not merged.get("status"):
                merged["status"] = values[0]
        elif clause["field"] == "owner" and clause["operator"] in {"equals", "in"} and values and not merged.get("owner"):
            merged["owner"] = values[0]
        elif clause["field"] == "database" and clause["operator"] in {"equals", "contains"} and values and not merged.get("database_label"):
            merged["database_label"] = values[0]
        elif clause["field"] == "title" and clause["operator"] == "equals" and values and not merged.get("exact_title"):
            merged["exact_title"] = values[0]
    merged["excluded_statuses"] = list(dict.fromkeys(merged["excluded_statuses"]))
    return merged


def notion_page_matches_common_filters(page: Dict[str, Any], clauses: List[Dict[str, Any]]) -> bool:
    field_values = {
        "customer": page.get("customer") or page.get("search_text", ""),
        "platform": page.get("platform") or page.get("search_text", ""),
        "status": page.get("status", ""),
        "owner": page.get("owner", ""),
        "created_at": page.get("created_time", ""),
        "title": page.get("title", ""),
        "keyword": "\n".join([page.get("search_text", ""), page.get("content", "")]),
        "database": page.get("database_label", ""),
    }
    for clause in normalize_filter_clauses(clauses):
        if clause["field"] not in field_values:
            continue
        candidate_raw = str(field_values[clause["field"]] or "")
        candidate = normalize_for_match(candidate_raw)
        values = [normalize_for_match(value) for value in values_for_clause(clause.get("value"))]
        operator = clause["operator"]
        # customer/platform/keyword are represented inside a property search
        # document, so equality is intentionally compiled as membership.
        document_field = clause["field"] in {"customer", "platform", "keyword"}
        if operator in {"equals", "in"}:
            matched = any(value in candidate if document_field else value == candidate for value in values)
            if not matched:
                return False
        elif operator in {"not_equals", "not_in"}:
            matched = any(value in candidate if document_field else value == candidate for value in values)
            if matched:
                return False
        elif operator == "contains" and not any(value in candidate for value in values):
            return False
        elif operator == "not_contains" and any(value in candidate for value in values):
            return False
        elif operator == "exists" and not candidate:
            return False
        elif operator == "not_exists" and candidate:
            return False
        elif operator == "gte" and values and candidate_raw[:10] < str(clause.get("value"))[:10]:
            return False
        elif operator == "lte" and values and candidate_raw[:10] > str(clause.get("value"))[:10]:
            return False
        elif operator == "between" and len(values) == 2:
            date_value = candidate_raw[:10]
            raw_values = values_for_clause(clause.get("value"))
            if not date_value or not (raw_values[0] <= date_value <= raw_values[1]):
                return False
    return True


def notion_page_matches_constraints(page: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    if constraints.get("database_label") and normalize_for_match(constraints["database_label"]) not in normalize_for_match(
        page.get("database_label", "")
    ):
        return False
    if constraints.get("exact_title") and normalize_for_match(constraints["exact_title"]) not in normalize_for_match(
        page.get("title", "")
    ):
        return False
    if constraints.get("status") and normalize_for_match(constraints["status"]) not in normalize_for_match(
        page.get("status", "")
    ):
        return False
    if constraints.get("owner") and normalize_for_match(constraints["owner"]) not in normalize_for_match(
        page.get("owner", "")
    ):
        return False
    if constraints.get("unassigned_owner") and str(page.get("owner", "")).strip() not in {
        "",
        "담당자 미지정",
        "미지정",
    }:
        return False
    if any(
        normalize_for_match(status) == normalize_for_match(page.get("status", ""))
        for status in constraints.get("excluded_statuses", [])
    ):
        return False
    if not notion_page_matches_common_filters(page, constraints.get("common_filter_clauses", [])):
        return False
    return True


def notion_page_matches_and_enrich(page: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    """Apply cheap constraints first, then lazily resolve relation-backed owners."""
    without_owner = {**constraints, "owner": ""}
    if not notion_page_matches_constraints(page, without_owner):
        return False
    if constraints.get("owner") or constraints.get("group_by") == "owner":
        resolve_notion_page_owner(page)
    return notion_page_matches_constraints(page, constraints)


def notion_exact_match_score(page: Dict[str, Any], constraints: Dict[str, Any]) -> int:
    score = 0
    if constraints.get("database_label"):
        score += 30
    if constraints.get("exact_title"):
        score += 60
    if constraints.get("status"):
        score += 25
    if constraints.get("owner"):
        score += 20
    return score


def notion_page_match_score(page: Dict[str, Any], terms: List[str]) -> int:
    if not terms:
        return 1
    weighted_fields = [
        (page.get("title", ""), 6),
        (page.get("type", ""), 4),
        (page.get("status", ""), 3),
        (page.get("owner", ""), 2),
        (page.get("search_text", ""), 1),
    ]
    score = 0
    for term in terms:
        normalized_term = normalize_for_match(term)
        for value, weight in weighted_fields:
            if normalized_term in normalize_for_match(value):
                score += weight
                break
    return score


def notion_minimum_match_score(terms: List[str]) -> int:
    if not terms:
        return 1
    if len(terms) <= 2:
        return 1
    return 2


def notion_page_matches_terms(page: Dict[str, Any], terms: List[str]) -> bool:
    if not terms:
        return True
    normalized_search_text = normalize_for_match(page.get("search_text", ""))
    return all(normalize_for_match(term) in normalized_search_text for term in terms)


def notion_search_max_pages(constraints: Dict[str, Any]) -> int:
    # Structured filters and grouped summaries must scan the complete configured
    # databases; otherwise an arbitrary recent-300 window produces incomplete
    # counts and groups. Keep ordinary fuzzy searches bounded for latency.
    if (
        constraints.get("status")
        or constraints.get("owner")
        or constraints.get("group_by")
        or constraints.get("common_filter_clauses")
    ):
        return 30
    return 3


def notion_database_status_filter(
    database_id: str,
    status: str,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Return whether a DB has status-like select fields and an exact API filter.

    When the requested status is absent from every status-like option list, the
    caller can safely skip that database. If schema inspection fails, return
    ``False`` so the existing local-scan fallback remains available.
    """
    if not status:
        return False, None
    try:
        properties = fetch_notion_database_schema(database_id).get("properties", {})
    except (HTTPException, requests.RequestException):
        return False, None
    found_status_property = False
    target = normalize_for_match(status)
    for prop_name, prop in properties.items():
        normalized_name = normalize_for_match(prop_name)
        if "상태" not in normalized_name and "status" not in normalized_name:
            continue
        prop_type = prop.get("type")
        if prop_type not in {"select", "status"}:
            continue
        found_status_property = True
        options = (prop.get(prop_type) or {}).get("options", [])
        for option in options:
            option_name = str(option.get("name") or "")
            if normalize_for_match(option_name) == target:
                return True, {
                    "property": prop_name,
                    prop_type: {"equals": option_name},
                }
    return found_status_property, None


def execute_notion_search(
    query_text: str,
    limit: int = 5,
    constraints: Optional[Dict[str, Any]] = None,
    planned_terms: Optional[List[str]] = None,
    include_blocks: bool = True,
) -> Dict[str, Any]:
    settings = get_notion_settings()
    database_ids = settings.get("database_ids", [])
    if not settings["token"] or not database_ids:
        return {
            "source": "notion",
            "status": "skipped",
            "reason": "NOTION_API_TOKEN/NOTION_DATABASE_IDS가 설정되지 않았습니다.",
            "notion_pages": [],
            "meta": {"displayed_count": 0},
        }

    notion_constraints = merge_common_notion_constraints(
        extract_notion_constraints(query_text),
        constraints,
    )
    exact_title = notion_constraints.get("exact_title", "")
    terms = list(dict.fromkeys(planned_terms or build_notion_terms(query_text, constraints)))[:8]
    # Status/owner are structured filters. Leaving them in free-text terms makes
    # every field participate in an accidental AND query and hides valid rows.
    structured_values = {
        normalize_for_match(value)
        for value in (notion_constraints.get("status"), notion_constraints.get("owner"))
        if value
    }
    structured_values.update(
        normalize_for_match(value)
        for clause in notion_constraints.get("common_filter_clauses", [])
        for value in values_for_clause(clause.get("value"))
        if clause.get("field") not in {"title", "keyword"}
    )
    terms = [
        term
        for term in terms
        if not any(
            normalize_for_match(term) in value or value in normalize_for_match(term)
            for value in structured_values
        )
    ]
    title_terms = [
        term
        for term in terms
        if normalize_for_match(term)
        not in {normalize_for_match(status) for status in NOTION_STATUS_QUERY_TERMS}
    ]
    target_database_label = notion_constraints.get("database_label", "")
    pages: List[Dict[str, Any]] = []
    scanned_count = 0
    database_meta = []
    errors = []
    seen_page_ids: set[str] = set()
    max_pages = notion_search_max_pages(notion_constraints)
    page_size = 100

    try:
        for database_id in database_ids:
            database_label = fetch_notion_database_title(database_id)
            if target_database_label and normalize_for_match(target_database_label) not in normalize_for_match(database_label):
                continue
            database_found_count = 0
            body_scan_candidates: List[Dict[str, Any]] = []
            status_filter_supported, status_api_filter = notion_database_status_filter(
                database_id,
                notion_constraints.get("status", ""),
            )
            if notion_constraints.get("status") and status_filter_supported and not status_api_filter:
                database_meta.append(
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "found_count": 0,
                    }
                )
                continue
            try:
                title_property = notion_title_property(notion_property_schema(database_id))
            except HTTPException:
                title_property = ""
            if exact_title and not title_property:
                errors.append(
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "reason": "Notion title property could not be resolved",
                    }
                )
                database_meta.append(
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "found_count": 0,
                    }
                )
                continue
            if exact_title and title_property:
                exact_payload = {
                    "page_size": min(page_size, max(limit, 5)),
                    "filter": {"property": title_property, "title": {"contains": exact_title}},
                    "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                }
                try:
                    exact_response = notion_post_json(
                        f"{NOTION_API_BASE}/databases/{database_id}/query",
                        exact_payload,
                        timeout=45,
                        retries=2,
                    )
                except requests.RequestException as exc:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "reason": f"Notion exact-title query failed: {exc}",
                        }
                    )
                    database_meta.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "found_count": 0,
                        }
                    )
                    continue
                if exact_response.status_code < 400:
                    exact_results = exact_response.json().get("results", [])
                    scanned_count += len(exact_results)
                    for raw_page in exact_results:
                        page = normalize_notion_page(
                            raw_page,
                            include_blocks=False,
                            database_id=database_id,
                            database_label=database_label,
                        )
                        page_id = page.get("id", "")
                        if page_id in seen_page_ids:
                            continue
                        if not notion_page_matches_and_enrich(page, notion_constraints):
                            continue
                        seen_page_ids.add(page_id)
                        page["match_score"] = (
                            notion_page_match_score(page, terms)
                            + notion_exact_match_score(page, notion_constraints)
                            + 60
                        )
                        page["matched_terms"] = terms
                        page["match_method"] = "notion_exact_title_filter"
                        pages.append(page)
                        database_found_count += 1
                else:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "reason": f"{exact_response.status_code} {exact_response.text[:300]}",
                        }
                    )
                # Exact-title requests are backed by Notion's server-side title
                # filter. Do not fall through to a multi-thousand-row database
                # scan when a literal title is absent or one database is slow.
                database_meta.append(
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "found_count": database_found_count,
                    }
                )
                continue
            if title_terms and title_property:
                try:
                    title_filter = {
                        "and": [
                            {"property": title_property, "title": {"contains": term}}
                            for term in title_terms
                        ]
                    }
                    targeted_response = notion_post_json(
                        f"{NOTION_API_BASE}/databases/{database_id}/query",
                        {
                            "page_size": page_size,
                            "filter": title_filter,
                            "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                        },
                        timeout=45,
                        retries=2,
                    )
                    if targeted_response.status_code < 400:
                        targeted_results = targeted_response.json().get("results", [])
                        scanned_count += len(targeted_results)
                        for raw_page in targeted_results:
                            page = normalize_notion_page(
                                raw_page,
                                include_blocks=False,
                                database_id=database_id,
                                database_label=database_label,
                            )
                            page_id = page.get("id", "")
                            if page_id in seen_page_ids:
                                continue
                            if not notion_page_matches_and_enrich(page, notion_constraints):
                                continue
                            seen_page_ids.add(page_id)
                            page["match_score"] = (
                                notion_page_match_score(page, terms)
                                + notion_exact_match_score(page, notion_constraints)
                                + 20
                            )
                            page["matched_terms"] = terms
                            page["match_method"] = "notion_title_filter"
                            pages.append(page)
                            database_found_count += 1
                except (HTTPException, requests.RequestException) as exc:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "reason": f"Notion targeted-title query failed: {exc}",
                        }
                    )
            cursor = None
            for _ in range(max_pages):
                payload: Dict[str, Any] = {
                    "page_size": page_size,
                    "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                }
                if status_api_filter:
                    payload["filter"] = status_api_filter
                if cursor:
                    payload["start_cursor"] = cursor

                try:
                    response = notion_post_json(
                        f"{NOTION_API_BASE}/databases/{database_id}/query",
                        payload,
                        timeout=45,
                        retries=2,
                    )
                except requests.RequestException as exc:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "reason": f"Notion database scan failed: {exc}",
                        }
                    )
                    break
                if response.status_code >= 400:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "reason": f"{response.status_code} {response.text[:300]}",
                        }
                    )
                    break

                data = response.json()
                results = data.get("results", [])
                scanned_count += len(results)
                for raw_page in results:
                    page = normalize_notion_page(
                        raw_page,
                        include_blocks=False,
                        database_id=database_id,
                        database_label=database_label,
                    )
                    page_id = page.get("id", "")
                    if page_id in seen_page_ids:
                        continue
                    if not notion_page_matches_and_enrich(page, notion_constraints):
                        continue
                    match_score = notion_page_match_score(page, terms)
                    if notion_page_matches_terms(page, terms):
                        seen_page_ids.add(page_id)
                        page["match_score"] = match_score + notion_exact_match_score(page, notion_constraints)
                        page["matched_terms"] = [
                            term
                            for term in terms
                            if normalize_for_match(term)
                            in normalize_for_match(page.get("search_text", ""))
                        ]
                        pages.append(page)
                        database_found_count += 1
                    elif terms and len(body_scan_candidates) < NOTION_BODY_SCAN_MAX_PER_DB:
                        body_scan_candidates.append(page)
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")

            if terms and database_found_count < limit and body_scan_candidates:
                for page in body_scan_candidates:
                    page_id = page.get("id", "")
                    if page_id in seen_page_ids:
                        continue
                    if not notion_page_matches_and_enrich(page, notion_constraints):
                        continue
                    block_text = fetch_notion_page_blocks(page_id, max_blocks=20)
                    if not block_text:
                        continue
                    body_search_text = f"{page.get('search_text', '')}\n{block_text}"
                    if not all(
                        normalize_for_match(term) in normalize_for_match(body_search_text)
                        for term in terms
                    ):
                        continue
                    seen_page_ids.add(page_id)
                    page["content"] = "\n\n".join(
                        part for part in (page.get("properties_text", ""), block_text) if part
                    ).strip()
                    page["match_score"] = notion_page_match_score(page, terms) - 5
                    page["matched_terms"] = [
                        term
                        for term in terms
                        if normalize_for_match(term) in normalize_for_match(body_search_text)
                    ]
                    page["match_method"] = "notion_body_scan"
                    pages.append(page)
                    database_found_count += 1
                    if database_found_count >= limit:
                        break
            database_meta.append(
                {
                    "database_id": database_id,
                    "database_label": database_label,
                    "found_count": database_found_count,
                }
            )
    except requests.RequestException as exc:
        return {
            "source": "notion",
            "status": "error",
            "reason": f"Notion connection failed: {exc}",
            "notion_pages": [],
            "meta": {"displayed_count": 0},
        }

    pages.sort(
        key=lambda page: (page.get("match_score", 0), page.get("last_edited_time", "")),
        reverse=True,
    )
    deduplicated_pages = deduplicate_notion_pages_by_id(pages)
    limited_pages = deduplicated_pages[:limit]
    if include_blocks and os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() not in {"0", "false", "no"}:
        for page in limited_pages:
            # notion_body_scan 단계에서 이미 본문을 조회한 페이지는 재조회하지 않는다.
            if page.get("match_method") == "notion_body_scan" and page.get("content"):
                continue
            block_text = fetch_notion_page_blocks(page.get("id", ""), max_blocks=20)
            if block_text:
                page["content"] = "\n\n".join(
                    part for part in (page.get("properties_text", ""), block_text) if part
                ).strip()
    status = "partial_error" if errors and limited_pages else "error" if errors else "ok"

    return {
        "source": "notion",
        "status": status,
        "reason": "; ".join(
            f"{error['database_label']}: {error['reason']}" for error in errors
        )[:500]
        if errors
        else None,
        "meta": {
            "displayed_count": len(limited_pages),
            "candidate_count": len(deduplicated_pages),
            "scanned_count": scanned_count,
            "terms": terms[:6],
            "constraints": notion_constraints,
            "database_ids": database_ids,
            "databases": database_meta,
            "errors": errors,
        },
        "notion_pages": limited_pages,
    }


def deduplicate_notion_pages_by_id(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduplicated_pages: List[Dict[str, Any]] = []
    seen_result_page_ids = set()
    for page in pages:
        page_id = str(page.get("id") or "")
        if not page_id or page_id in seen_result_page_ids:
            continue
        seen_result_page_ids.add(page_id)
        deduplicated_pages.append(page)
    return deduplicated_pages


def collect_retrieved_notion_pages(retrieval_result: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    pages = []
    seen_page_ids = set()
    for stage in retrieval_result.get("stages", []):
        for page in stage.get("notion_pages", []):
            page_id = page.get("id")
            if not page_id or page_id in seen_page_ids:
                continue
            seen_page_ids.add(page_id)
            pages.append(page)
            if len(pages) >= limit:
                return pages
    return pages


__all__ = [
    "correct_notion_status_typo",
    "build_notion_terms",
    "trim_owner_adnominal",
    "inherit_notion_context",
    "extract_notion_database_label_from_text",
    "extract_notion_constraints",
    "merge_common_notion_constraints",
    "notion_page_matches_common_filters",
    "notion_page_matches_constraints",
    "notion_page_matches_and_enrich",
    "notion_exact_match_score",
    "notion_page_match_score",
    "notion_minimum_match_score",
    "notion_page_matches_terms",
    "notion_search_max_pages",
    "notion_database_status_filter",
    "execute_notion_search",
    "deduplicate_notion_pages_by_id",
    "collect_retrieved_notion_pages",
]
