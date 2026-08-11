"""문서와 Chroma 이슈가 구조화된 제약 조건에 맞는지 판정한다."""

import re
from typing import Any, Dict, List, Optional

from backend.common import (
    extract_chroma_date,
    extract_chroma_field,
    extract_issue_id_from_text,
    extract_project_from_chroma_text,
    extract_title_from_chroma_text,
    format_mantis_custom_date,
    normalize_for_match,
    text_matches_any_alias,
    text_matches_customer_aliases,
)
from backend.config import STATUS_MATCH_ALIASES
from backend.query_filters import normalize_filter_clauses
from backend.query_planning import plan_created_at_matches


def text_matches_constraints(text: str, constraints: Dict[str, Any]) -> bool:
    if constraints.get("customer_exact") and normalize_for_match(constraints["customer_exact"]) not in normalize_for_match(text):
        return False
    if constraints.get("customer_aliases") and not text_matches_customer_aliases(
        text,
        constraints["customer_aliases"],
    ):
        return False
    if constraints.get("platform_aliases") and not text_matches_any_alias(
        text,
        constraints["platform_aliases"],
    ):
        return False
    if constraints.get("excluded_customer_aliases") and text_matches_customer_aliases(
        text,
        constraints["excluded_customer_aliases"],
    ):
        return False
    if constraints.get("excluded_platform_aliases") and text_matches_any_alias(
        text,
        constraints["excluded_platform_aliases"],
    ):
        return False
    normalized_text = normalize_for_match(text)
    for clause in normalize_filter_clauses(constraints.get("filter_clauses") or []):
        if clause["field"] not in {"title", "keyword"}:
            continue
        raw_value = clause.get("value")
        values = raw_value if isinstance(raw_value, list) else [raw_value]
        normalized_values = [normalize_for_match(str(value or "")) for value in values]
        if clause["operator"] == "contains" and not any(value in normalized_text for value in normalized_values):
            return False
        if clause["operator"] == "not_contains" and any(value in normalized_text for value in normalized_values):
            return False
    return True


def text_matches_status(text: str, statuses: List[str]) -> bool:
    if not statuses:
        return True
    return any(
        text_matches_any_alias(text, STATUS_MATCH_ALIASES.get(status, [status]))
        for status in statuses
    )


def text_matches_screen_numbers(text: str, screen_numbers: List[str]) -> bool:
    if not screen_numbers:
        return True
    # 감사/검증 단계에서도 숫자 부분 문자열(예: 9413이 194130에 포함됨)을
    # 화면번호 일치로 인정하지 않는다. 검색 단계의 필드/제목 정확 일치 규칙과
    # 동일하게 독립된 숫자 토큰만 허용한다.
    return any(
        re.search(rf"(?<!\d){re.escape(screen)}(?!\d)", str(text or ""))
        for screen in screen_numbers
    )


def chroma_text_matches_date_fields(text: str, date_fields: Dict[str, str]) -> bool:
    if not date_fields:
        return True
    return all(
        format_mantis_custom_date(extract_chroma_field(text, field_name)) == expected_date
        for field_name, expected_date in date_fields.items()
    )


def chroma_text_matches_created_at(text: str, created_at: Optional[Dict[str, str]]) -> bool:
    return plan_created_at_matches(extract_chroma_date(text), created_at)


def title_has_screen_number(title: str, screen_number: str) -> bool:
    return bool(re.search(rf"\[\s*{re.escape(screen_number)}\s*\]", title))


def field_matches_screen_number(value: str, screen_number: str) -> bool:
    return normalize_for_match(value) == normalize_for_match(screen_number)


def chroma_text_matches_screen_numbers(text: str, screen_numbers: List[str]) -> bool:
    if not screen_numbers:
        return True
    screen_field = extract_chroma_field(text, "화면번호")
    title = extract_title_from_chroma_text(text)
    return any(
        field_matches_screen_number(screen_field, screen_number)
        or title_has_screen_number(title, screen_number)
        for screen_number in screen_numbers
    )


def text_matches_issue_ids(text: str, issue_ids: List[int]) -> bool:
    if not issue_ids:
        return True
    normalized_text = normalize_for_match(text)
    return any(str(issue_id) in normalized_text for issue_id in issue_ids)


def text_matches_people(text: str, people: Dict[str, Any], role_text: str = "") -> bool:
    names = people.get("names", []) if people else []
    if not names:
        return True

    role = people.get("role") or "any"
    search_text = role_text or text
    if role == "handler":
        return any(text_matches_any_alias(search_text, [name]) for name in names)
    if role == "reporter":
        from_text = " ".join(
            match.group(0)
            for match in re.finditer(r"From\.\s*[가-힣A-Za-z]{2,6}", text, re.IGNORECASE)
        )
        reporter_context = " ".join([from_text, text])
        return any(text_matches_any_alias(reporter_context, [name]) for name in names)
    return any(text_matches_any_alias(text, [name]) for name in names)


def chroma_text_matches_constraints(text: str, constraints: Dict[str, Any]) -> bool:
    issue_id = extract_issue_id_from_text(text)
    if issue_id in constraints.get("exclude_issue_ids", []):
        return False
    max_issue_id = constraints.get("max_issue_id_exclusive")
    if max_issue_id and issue_id >= int(max_issue_id):
        return False
    if constraints.get("customer_exact") and normalize_for_match(
        extract_project_from_chroma_text(text)
    ) != normalize_for_match(constraints["customer_exact"]):
        return False
    if constraints.get("customer_aliases") and not text_matches_customer_aliases(
        extract_project_from_chroma_text(text),
        constraints["customer_aliases"],
    ):
        return False
    if constraints.get("excluded_customer_aliases") and text_matches_customer_aliases(
        extract_project_from_chroma_text(text),
        constraints["excluded_customer_aliases"],
    ):
        return False
    if constraints.get("platform_aliases"):
        # 재구축된 스냅샷 문서에는 '매체구분' 헤더가 있다. 있으면 그 값으로만 판정해
        # 본문의 "MTS에서는 됩니다" 같은 비교 언급 오탐을 막는다(라이브 검색의
        # issue_matches_platform과 동일한 규칙). 구버전 문서(필드 없음)는 기존처럼
        # 전문 검색으로 폴백한다.
        media_type = extract_chroma_field(text, "매체구분")
        platform_source = media_type if media_type and media_type != "기타/미상" else text
        if not text_matches_any_alias(platform_source, constraints["platform_aliases"]):
            return False
    if constraints.get("excluded_platform_aliases"):
        media_type = extract_chroma_field(text, "매체구분")
        platform_source = media_type if media_type and media_type != "기타/미상" else text
        if text_matches_any_alias(platform_source, constraints["excluded_platform_aliases"]):
            return False
    if not text_matches_status(
        extract_chroma_field(text, "현재 상태"),
        constraints.get("statuses", []),
    ):
        return False
    if constraints.get("excluded_statuses") and text_matches_status(
        extract_chroma_field(text, "현재 상태"),
        constraints["excluded_statuses"],
    ):
        return False
    if constraints.get("unassigned_people"):
        handler = extract_chroma_field(text, "담당자").strip()
        if handler and handler not in {"담당자 미지정", "미지정", "미상"}:
            return False
    if not text_matches_issue_ids(text, constraints.get("issue_ids", [])):
        return False
    if not chroma_text_matches_screen_numbers(text, constraints.get("screen_numbers", [])):
        return False
    if not chroma_text_matches_date_fields(text, constraints.get("date_fields", {})):
        return False
    if not chroma_text_matches_created_at(text, constraints.get("created_at")):
        return False
    if not text_matches_people(
        text,
        constraints.get("people", {}),
        extract_chroma_field(text, "담당자"),
    ):
        return False
    if not text_matches_constraints(text, {"filter_clauses": constraints.get("filter_clauses", [])}):
        return False
    return True


__all__ = [
    "chroma_text_matches_constraints",
    "chroma_text_matches_created_at",
    "chroma_text_matches_date_fields",
    "chroma_text_matches_screen_numbers",
    "field_matches_screen_number",
    "text_matches_constraints",
    "text_matches_issue_ids",
    "text_matches_people",
    "text_matches_screen_numbers",
    "text_matches_status",
    "title_has_screen_number",
]
