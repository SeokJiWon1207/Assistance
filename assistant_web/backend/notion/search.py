"""Notion 제약 추출, 검색, 필터링과 랭킹을 담당한다."""

import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import HTTPException

from backend.common import (
    LOOKUP_REQUEST_PATTERN,
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
    NOTION_IN_ENDING_NOUNS,
    NOTION_STATUS_QUERY_TERMS,
    NOTION_STATUS_TYPOS,
)
from backend.notion.mapping import normalize_notion_page, resolve_notion_page_owner
from backend.notion.schema import (
    notion_property_schema as _notion_property_schema,
    notion_title_property,
)
from backend.query_filters import clause_supported, matches_source_filters, normalize_filter_clauses, values_for_clause
from backend.query_planning import extract_group_by


# 의존 명사와 조사는 붙여 쓰기도 하지만, 다음 일반 명사의 일부까지
# 소비해서는 안 된다. 이 표현 뒤에는 반드시 어절 경계를 확인한다.
_STATUS_BOUND_NOUN = r"(?:것|거|건)(?:들)?(?:만|은|는|이|가|을|를|도)?"
_STATUS_SPOKEN_SUFFIX = rf"(?:(?:인|된)\s*)?{_STATUS_BOUND_NOUN}"
_STATUS_WORD_END = r"(?:\s|$|[,.;!?])"
_OWNER_PREDICATE = r"(?:맡|담당|처리)[가-힣]*"
_OWNER_SUFFIXES = ("이면서", "으로", "이고", "이며", "인", "이", "가", "은", "는", "의", "로")


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
    text = LOOKUP_REQUEST_PATTERN.sub(" ", text)
    cleaned = re.sub(r"[#,:;!?()\[\]{}\"']", " ", text)
    cleaned = re.sub(r"\d+\s*(개|건)\s*만?", " ", cleaned)
    notion_stopwords = {
        normalize_for_match(keyword)
        for keyword in {*STOPWORDS, *NOTION_QUERY_STOPWORDS, "관한", "관해서", "관하여"}
    }
    terms: List[str] = []
    for token in cleaned.split():
        normalized = token if token in NOTION_IN_ENDING_NOUNS else normalize_search_token(token)
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


def trim_owner_adnominal(candidate: str, following_text: str = "") -> str:
    """이름 자체를 우선 보존하고 확인 가능한 조사만 정리한다."""
    name = str(candidate or "").strip()
    handlers, reporters = load_known_people()
    known = set(handlers) | set(reporters)
    if name in known or len(name) <= 3:
        return name
    for suffix in _OWNER_SUFFIXES:
        if name.endswith(suffix) and len(name) > 2 and name[: -len(suffix)] in known:
            return name[: -len(suffix)]
    # 명단에 없는 4글자 이름 자체를 조사로 줄이지 않는다. 긴 연결
    # 어미나 원래 이름이 될 수 없는 길이는 분리하고, 짧은 조사는
    # 뒤 문법까지 확인한다 ('김지은이 맡은', '김지은의 문서').
    for suffix in _OWNER_SUFFIXES:
        if name.endswith(suffix) and 3 <= len(name) - len(suffix) <= 4:
            grammatical_suffix = (
                suffix in {"이", "가", "은", "는"}
                and re.match(rf"\s+{_OWNER_PREDICATE}", following_text)
            ) or (
                suffix == "의" and re.match(r"\s+(?:문서|항목|업무|이슈)", following_text)
            )
            if len(suffix) > 1 or len(name) > 4 or grammatical_suffix:
                return name[: -len(suffix)]
    return name


# Mantis 명단에는 svn·signalmaker·DRFN_SM 같은 연동 계정도 섞여 있다.
# '담당자' 표지가 없는 표현에서는 제품명과 구별되는 한글 이름과
# 대문자로 시작하는 영문 이름만 사람으로 본다.
_IMPLICIT_OWNER_NAME = re.compile(r"[가-힣]{2,4}|[A-Z][a-z]{1,19}")


@lru_cache(maxsize=8)
def _known_notion_owner_matcher(
    handlers: tuple[str, ...], reporters: tuple[str, ...],
) -> tuple[Optional[re.Pattern[str]], Dict[str, Tuple[int, str]]]:
    """스냅샷에서 읽은 명단 내용별로 검증·정렬·정규식 생성을 한 번만 한다."""
    names = sorted(
        (
            name for name in set(handlers) | set(reporters)
            if _IMPLICIT_OWNER_NAME.fullmatch(name) and is_valid_person_name(name)
        ),
        key=lambda name: (-len(name), name),
    )
    if not names:
        return None, {}
    pattern = re.compile(
        r"(?<![가-힣A-Za-z])(?P<owner>" + "|".join(re.escape(name) for name in names) + r")(?:님)?"
        rf"(?:(?:이|가|은|는)?\s+(?:{_OWNER_PREDICATE}|소유|작성|등록|할당)"
        r"|의\s+(?:문서|항목|업무|이슈))",
        re.IGNORECASE,
    )
    return pattern, {name.casefold(): (rank, name) for rank, name in enumerate(names)}


def known_notion_owner(text: str) -> str:
    """담당자 표지가 없는 표현은 실제 명단에 있는 이름만 인정한다."""
    # load_known_people은 스냅샷 경로·수정 시각·크기 변경을 감지한다.
    # 명단 내용이 바뀌면 새 matcher를 만들고, 내용이 같으면 재사용한다.
    handlers, reporters = load_known_people()
    pattern, names = _known_notion_owner_matcher(tuple(handlers), tuple(reporters))
    if pattern is None:
        return ""
    # 기존의 긴 이름 우선 선택을 유지하면서 텍스트는 합친 패턴으로 검색한다.
    match = min(
        pattern.finditer(text), key=lambda item: names[item.group("owner").casefold()][0], default=None,
    )
    return names[match.group("owner").casefold()][1] if match else ""


def inherit_notion_context(recent_texts: List[str]) -> str:
    """후속 질문이 잃어버린 Notion 조건(DB·상태)을 이전 턴에서 물려받는다.

    "노션에서 결재요청 상태인 항목 보여줘" 다음의 "담당자별로 묶어줘"는
    Mantis 쪽 조건 상속만으로는 상태와 소스를 모두 잃는다. 반환한 접두
    텍스트를 질문 앞에 붙이면 기존 조건 추출이 그대로 다시 인식한다.
    """
    if len(recent_texts) < 2:
        return ""
    from backend.intent.context import is_follow_up_question

    if re.search(r"mantis|맨티스|노션|notion", recent_texts[0], re.IGNORECASE) and not re.search(
        r"그\s*(?:중|항목|문서)|방금|그대로|상태만|담당자만", recent_texts[0],
    ):
        return ""
    if not is_follow_up_question(recent_texts[0]) and not re.search(
        r"(?:상태|담당자)만.*(?:바꿔|변경)", recent_texts[0],
    ):
        return ""
    latest = extract_notion_constraints(recent_texts[0])
    for prior in recent_texts[1:]:
        if not re.search(r"노션|notion|이슈리스트|주요이슈", prior, re.IGNORECASE):
            continue
        prior_constraints = extract_notion_constraints(prior)
        parts = []
        for key in ("database_label", "exact_title", "status", "owner"):
            value = prior_constraints.get(key)
            if not value or latest.get(key):
                continue
            if key == "status" and latest.get("excluded_statuses"):
                continue
            if key == "owner" and latest.get("unassigned_owner"):
                continue
            parts.append({
                "database_label": str(value), "exact_title": f'"{value}"',
                "status": f"상태가 {value}인", "owner": f"담당자 {value}",
            }[key])
        if not latest.get("status") and not latest.get("excluded_statuses"):
            parts.extend(f"{value} 제외" for value in prior_constraints.get("excluded_statuses", []))
        if prior_constraints.get("unassigned_owner") and not latest.get("owner"):
            parts.append("담당자 미지정")
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
    status = ""
    excluded_statuses: List[str] = []
    # 기존 상태명은 다양한 활용형(완료건수/완료처리된/시작전이었던)을
    # 계속 인식한다. 합성어와 모호한 '대기'에만 엄격한 뒤 경계를 둔다.
    remaining_status_text = constraint_text
    for candidate in sorted(NOTION_STATUS_QUERY_TERMS, key=len, reverse=True):
        spelling = r"\s*".join(re.escape(char) for char in candidate)
        boundary = (
            rf"(?=(?:{_STATUS_SPOKEN_SUFFIX}|상태|인|으로|로|만|은|는|이|가|을|를|된)?{_STATUS_WORD_END})"
            if candidate == "대기" else ""
        )
        pattern = re.compile(r"(?<![가-힣A-Za-z0-9_])" + spelling + boundary, re.IGNORECASE)
        for match in pattern.finditer(remaining_status_text):
            if re.match(
                rf"\s*(?:{_STATUS_SPOKEN_SUFFIX}|상태|항목|문서|이슈)?"
                r"\s*(?:은|는|이|가)?\s*(?:아닌|말고|제외|빼고)",
                remaining_status_text[match.end():],
            ):
                excluded_statuses.append(candidate)
            elif not status:
                status = candidate
        remaining_status_text = pattern.sub(" ", remaining_status_text)

    # 사용자 정의 상태는 명시적인 '상태가 X인' 표현만 받는다.
    # '상태가 변경된 항목'의 형용사를 옵션 이름으로 만들지 않는다.
    explicit_status = re.search(
        r"상태\s*(?:가|는|=|:)\s*([가-힣A-Za-z0-9_()-]+?)"
        rf"(?:인(?=(?:\s*{_STATUS_BOUND_NOUN})?{_STATUS_WORD_END})|(?:으로|로)(?={_STATUS_WORD_END}))",
        constraint_text,
    )
    if explicit_status:
        value = explicit_status.group(1).strip()
        if value and value not in excluded_statuses and not any(
            word in explicit_status.group(0) for word in ("말고", "제외", "빼고", "아닌")
        ):
            status = value

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
        r"(?:담당자|담당|owner)\s*(?:가|는|은|를|을|만|=|:)?\s*"
        r"([가-힣]{2,8}|[A-Za-z]{2,20})(?=\s|$|[,.;!?])",
        owner_text,
        re.IGNORECASE,
    )
    if owner_match and not unassigned_owner:
        candidate = trim_owner_adnominal(owner_match.group(1), owner_text[owner_match.end(1):])
        # 조사가 붙은 일반 명사가 이름으로 잡히지 않도록 어간까지 검증한다.
        if is_valid_person_name(candidate) and is_valid_person_name(
            strip_korean_particle(candidate)
        ):
            owner = candidate
    if not owner and not unassigned_owner:
        owner = known_notion_owner(owner_text)

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
        if not clause_supported("notion", clause) or clause["field"] not in field_values:
            continue
        if clause["field"] == "created_at":
            if not matches_source_filters("notion", field_values, [clause]):
                return False
            continue
        candidate_raw = str(field_values[clause["field"]] or "")
        candidate = normalize_for_match(candidate_raw)
        values = [normalize_for_match(value) for value in values_for_clause(clause.get("value"))]
        operator = clause["operator"]
        # customer/platform/keyword are represented inside a property search
        # document, so equality is intentionally compiled as membership.
        document_field = clause["field"] in {"customer", "platform", "keyword"}
        owner_names = {normalize_for_match(name) for name in candidate_raw.split(",")}
        if operator in {"equals", "in"}:
            matched = any(
                value in owner_names if clause["field"] == "owner"
                else value in candidate if document_field else value == candidate for value in values
            )
            if not matched:
                return False
        elif operator in {"not_equals", "not_in"}:
            matched = any(
                value in owner_names if clause["field"] == "owner"
                else value in candidate if document_field else value == candidate for value in values
            )
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
    if constraints.get("exact_title") and normalize_for_match(constraints["exact_title"]) != normalize_for_match(
        page.get("title", "")
    ):
        return False
    requested_status = normalize_for_match(constraints.get("status", ""))
    if requested_status == "진행":
        requested_status = "진행중"
    if requested_status and requested_status != normalize_for_match(page.get("status", "")):
        return False
    if constraints.get("owner") and normalize_for_match(constraints["owner"]) not in {
        normalize_for_match(name) for name in str(page.get("owner", "")).split(",")
    }:
        return False
    if constraints.get("unassigned_owner") and str(page.get("owner", "")).strip() not in {
        "",
        "담당자 미지정",
        "미지정",
    }:
        return False
    if any(
        normalize_for_match(status) in {
            normalize_for_match(value)
            for value in [page.get("status", ""), *(page.get("status_fields") or {}).values()]
        }
        for status in constraints.get("excluded_statuses", [])
    ):
        return False
    if not notion_page_matches_common_filters(page, constraints.get("common_filter_clauses", [])):
        return False
    return True


def notion_page_matches_and_enrich(page: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    """Apply cheap constraints first, then lazily resolve relation-backed owners."""
    if constraints.get("status"):
        # 같은 페이지의 구상태/상태 중 검색 조건에 사용한 속성을 표시한다.
        # 필터는 시작전인데 답변에는 구상태=종료된이슈를 표시하는 일을 막는다.
        target = normalize_for_match(constraints["status"])
        for name, value in (page.get("status_fields") or {}).items():
            if normalize_for_match(value) == target:
                page["status"] = value
                page["status_property"] = name
                break
    without_owner = {
        **constraints, "owner": "", "unassigned_owner": False,
        "common_filter_clauses": [
            clause for clause in constraints.get("common_filter_clauses", [])
            if clause.get("field") != "owner"
        ],
    }
    if not notion_page_matches_constraints(page, without_owner):
        return False
    if (constraints.get("owner") or constraints.get("unassigned_owner")
            or constraints.get("group_by") == "owner"
            or any(clause.get("field") == "owner" for clause in constraints.get("common_filter_clauses", []))):
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
        or constraints.get("excluded_statuses")
        or constraints.get("unassigned_owner")
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
    target = normalize_for_match(status)
    filters: List[Dict[str, Any]] = []
    supported = False
    for prop_name, prop in properties.items():
        prop_type = prop.get("type")
        if prop_type not in {"status", "select"} or not any(
            key in normalize_for_match(prop_name) for key in ("상태", "status", "진행")
        ):
            continue
        supported = True
        for option in (prop.get(prop_type) or {}).get("options", []):
            option_name = str(option.get("name") or "")
            if normalize_for_match(option_name) == target:
                filters.append({"property": prop_name, prop_type: {"equals": option_name}})
    if not filters:
        return supported, None
    return True, filters[0] if len(filters) == 1 else {"or": filters}


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
    if notion_constraints.get("owner"):
        # 담당 조건으로 소비한 '김지은이 맡은'의 서술어가 본문 검색어로
        # 남으면 담당자가 맞는 문서도 '맡은'이라는 단어가 없어 탈락한다.
        owner_predicates = {
            normalize_search_token(match.group(1))
            for match in re.finditer(
                rf"(?<![가-힣A-Za-z]){re.escape(notion_constraints['owner'])}(?:이|가|은|는)?"
                rf"\s+({_OWNER_PREDICATE})(?=\s|$)",
                remove_quoted_value(query_text) if exact_title else query_text,
            )
        }
        terms = [term for term in terms if normalize_search_token(term) not in owner_predicates]
    # Status/owner are structured filters. Leaving them in free-text terms makes
    # every field participate in an accidental AND query and hides valid rows.
    structured_values = {
        normalize_for_match(value)
        for value in (notion_constraints.get("status"), notion_constraints.get("owner"))
        if value
    }
    structured_values.update(
        normalize_for_match(value) for value in notion_constraints.get("excluded_statuses", [])
    )
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
    search_complete = True
    incomplete_reasons: set[str] = set()
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
                        "error_kind": "internal",
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
                title_filter: Dict[str, Any] = {
                    "property": title_property, "title": {"equals": exact_title},
                }
                exact_payload: Dict[str, Any] = {
                    "page_size": page_size,
                    "filter": {"and": [title_filter, status_api_filter]} if status_api_filter else title_filter,
                    "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                }
                for _ in range(max_pages):
                    try:
                        exact_response = notion_post_json(
                            f"{NOTION_API_BASE}/databases/{database_id}/query",
                            exact_payload,
                            timeout=45,
                            retries=2,
                        )
                        exact_response.raise_for_status()
                    except requests.RequestException as exc:
                        response_status = getattr(exc.response, "status_code", None)
                        errors.append({
                            "database_id": database_id,
                            "database_label": database_label,
                            "error_kind": "internal" if response_status and 400 <= response_status < 500
                            and response_status != 429 else "dependency",
                            "reason": f"Notion exact-title query failed: {exc}",
                        })
                        break
                    exact_data = exact_response.json()
                    exact_results = exact_data.get("results", [])
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
                    if not exact_data.get("has_more"):
                        break
                    next_cursor = exact_data.get("next_cursor")
                    if not next_cursor or next_cursor == exact_payload.get("start_cursor"):
                        search_complete = False
                        incomplete_reasons.add("invalid_cursor")
                        break
                    exact_payload["start_cursor"] = next_cursor
                else:
                    search_complete = False
                    incomplete_reasons.add("page_limit")
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
                            "error_kind": "dependency" if isinstance(exc, requests.RequestException) else "internal",
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
                            "error_kind": "dependency",
                            "reason": f"Notion database scan failed: {exc}",
                        }
                    )
                    break
                if response.status_code >= 400:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "error_kind": (
                                "dependency"
                                if response.status_code == 429 or response.status_code >= 500
                                else "internal"
                            ),
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
                next_cursor = data.get("next_cursor")
                if not next_cursor or next_cursor == cursor:
                    search_complete = False
                    incomplete_reasons.add("invalid_cursor")
                    break
                cursor = next_cursor
            else:
                search_complete = False
                incomplete_reasons.add("page_limit")

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
            "error_kind": "dependency",
            "reason": f"Notion connection failed: {exc}",
            "notion_pages": [],
            "meta": {"displayed_count": 0},
        }

    pages.sort(
        key=lambda page: (page.get("last_edited_time", ""), page.get("match_score", 0)),
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
        "error_kind": (
            "dependency"
            if errors and all(error.get("error_kind") == "dependency" for error in errors)
            else "internal" if errors else None
        ),
        "reason": "; ".join(
            f"{error['database_label']}: {error['reason']}" for error in errors
        )[:500]
        if errors
        else None,
        "meta": {
            "displayed_count": len(limited_pages),
            "candidate_count": len(deduplicated_pages),
            "scanned_count": scanned_count,
            "search_complete": search_complete and not errors,
            "incomplete_reasons": sorted(incomplete_reasons | ({"query_error"} if errors else set())),
            "scan_limit_per_database": max_pages * page_size,
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
