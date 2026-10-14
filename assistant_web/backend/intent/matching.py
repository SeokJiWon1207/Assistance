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
from backend.mantis_status import matches_mantis_status
from backend.query_filters import normalize_filter_clauses, matches_source_filters, screen_filter_values
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
        matches_mantis_status(text, status)
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


def extract_reporter_names(text: str, reporter_text: Optional[str] = None) -> List[str]:
    """Mantis 등록자와 명시된 고객 메일 발신자를 같은 요청자 경계로 읽는다."""
    reporters = [reporter_text if reporter_text is not None else extract_chroma_field(text, "요청자")]
    reporters.extend(match.group(1) for match in re.finditer(
        r"(?:^|[^가-힣A-Za-z0-9_]|\\[nr])From\.\s*([가-힣A-Za-z][가-힣A-Za-z0-9_-]*)",
        text, re.IGNORECASE,
    ))
    return list(dict.fromkeys(reporter for reporter in reporters if reporter))


def text_matches_people(
    text: str, people: Dict[str, Any], role_text: str = "", reporter_text: Optional[str] = None,
) -> bool:
    names = people.get("names", []) if people else []
    if not names:
        return True

    role = people.get("role") or "any"
    search_text = role_text or text
    if role == "handler":
        return any(text_matches_any_alias(search_text, [name]) for name in names)
    if role == "reporter":
        # Mantis 등록 계정과 원문 메일의 고객 요청자는 다를 수 있다. 요청자
        # 필드 또는 명시한 From. 발신자만 인정하고 담당자/본문 언급은 제외한다.
        reporters = extract_reporter_names(text, reporter_text)
        return any(normalize_for_match(reporter) == normalize_for_match(name)
                   for reporter in reporters if reporter for name in names)
    return any(text_matches_any_alias(text, [name]) for name in names)


def chroma_text_matches_constraints(text: str, constraints: Dict[str, Any]) -> bool:
    from backend.team_scope import matches_team_constraint

    if not matches_team_constraint(extract_chroma_field(text, "담당자"), constraints):
        return False
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
    media_type = extract_chroma_field(text, "매체구분")
    platform_source = media_type
    if constraints.get("platform_aliases"):
        # 라이브 원문과 동일하게 매체구분 헤더로만 판정한다.
        # 헤더가 없거나 매체 미지정이면 본문 언급으로 플랫폼을 추측하지 않는다.
        if not text_matches_any_alias(platform_source, constraints["platform_aliases"]):
            return False
    if constraints.get("excluded_platform_aliases"):
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
        extract_chroma_field(text, "요청자"),
    ):
        return False
    return matches_source_filters("chroma", {
        "customer": extract_project_from_chroma_text(text),
        "platform": platform_source,
        "status": extract_chroma_field(text, "현재 상태"),
        "owner": extract_chroma_field(text, "담당자"),
        "reporter": extract_reporter_names(text, extract_chroma_field(text, "요청자")),
        "issue_id": str(issue_id),
        "screen_number": screen_filter_values(extract_chroma_field(text, "화면번호"), extract_title_from_chroma_text(text)),
        "created_at": extract_chroma_date(text),
        "title": extract_title_from_chroma_text(text), "keyword": text,
    }, constraints.get("filter_clauses") or [])



__all__ = [
    "chroma_text_matches_constraints",
    "chroma_text_matches_created_at",
    "chroma_text_matches_date_fields",
    "chroma_text_matches_screen_numbers",
    "field_matches_screen_number",
    "extract_reporter_names",
    "text_matches_constraints",
    "text_matches_issue_ids",
    "text_matches_people",
    "text_matches_screen_numbers",
    "text_matches_status",
    "title_has_screen_number",
]
