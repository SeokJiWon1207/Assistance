"""Mantis 원문에 대한 조건·필드 일치와 키워드 점수 규칙."""
from typing import Any, Dict, List, Optional
from backend.sources.mantis_fields import get_issue_custom_field_value, issue_to_search_text, mantis_issue_effort_date
from backend.utils.dates import format_mantis_custom_date
from backend.utils.text import normalize_for_match, text_matches_any_alias
from backend.customer_identity import text_matches_customer_aliases
from backend.query.dates import plan_created_at_matches
from backend.query_filters import matches_source_filters, screen_filter_values
from backend.intent.matching import extract_reporter_names, text_matches_status, text_matches_people, field_matches_screen_number, title_has_screen_number
from backend.effort_fields import issue_effort_people
from backend.timing import measured

def issue_matches_date_fields(issue: Dict[str, Any], date_fields: Dict[str, str]) -> bool:
    if not date_fields:
        return True
    return all(
        format_mantis_custom_date(get_issue_custom_field_value(issue, field_name)) == expected_date
        for field_name, expected_date in date_fields.items()
    )


def issue_matches_created_at(issue: Dict[str, Any], created_at: Optional[Dict[str, str]]) -> bool:
    return plan_created_at_matches(str(issue.get("created_at") or ""), created_at)


def issue_matches_platform(
    issue: Dict[str, Any], platform_aliases: List[str], *, search_text: Optional[str] = None,
) -> bool:
    """플랫폼 조건은 원문의 '매체구분' 필드로만 판정한다.

    제목·본문의 비교 언급으로 매체 미지정 이슈의 플랫폼을 추측하지 않는다.
    매체 미지정 이슈도 플랫폼 제한이 없는 검색에서는 계속 조회할 수 있다.
    """
    if not platform_aliases:
        return True
    media_type = get_issue_custom_field_value(issue, "매체구분")
    return text_matches_any_alias(media_type, platform_aliases)


def mantis_keyword_match_score(issue: Dict[str, Any], keywords: List[str]) -> int:
    if not keywords:
        return 0
    title = normalize_for_match(str(issue.get("summary", "")))
    project = normalize_for_match(str((issue.get("project") or {}).get("name", "")))
    custom_fields = normalize_for_match(
        " ".join(
            f"{field.get('field', {}).get('name', '')} {field.get('value', '')}"
            for field in issue.get("custom_fields", [])
        )
    )
    body = normalize_for_match(
        " ".join(
            [
                str(issue.get("description", "")),
                str(issue.get("steps_to_reproduce", "")),
                " ".join(str(note.get("text") or "") for note in (issue.get("notes") or [])),
            ]
        )
    )
    score = 0
    for keyword in keywords:
        normalized = normalize_for_match(keyword)
        if normalized in title:
            score += 40
        elif normalized in custom_fields:
            score += 30
        elif normalized in project:
            score += 20
        elif normalized in body:
            score += 10
    return score


def issue_matches_screen_numbers(issue: Dict[str, Any], screen_numbers: List[str]) -> bool:
    if not screen_numbers:
        return True
    screen_field = get_issue_custom_field_value(issue, "화면번호")
    title = str(issue.get("summary", ""))
    return any(
        field_matches_screen_number(screen_field, screen_number)
        or title_has_screen_number(title, screen_number)
        for screen_number in screen_numbers
    )


def issue_matches_constraints(
    issue: Dict[str, Any], constraints: Dict[str, Any], *, search_text: Optional[str] = None,
) -> bool:
    from backend.domain.team import matches_team_constraint

    if not matches_team_constraint(str((issue.get("handler") or {}).get("real_name") or ""), constraints):
        return False
    if issue.get("id") in constraints.get("exclude_issue_ids", []):
        return False
    max_issue_id = constraints.get("max_issue_id_exclusive")
    if max_issue_id and int(issue.get("id") or 0) >= int(max_issue_id):
        return False
    if constraints.get("customer_exact") and normalize_for_match(
        str((issue.get("project") or {}).get("name", ""))
    ) != normalize_for_match(constraints["customer_exact"]):
        return False
    if constraints.get("customer_aliases") and not text_matches_customer_aliases(
        str((issue.get("project") or {}).get("name", "")),
        constraints["customer_aliases"],
    ):
        return False
    project_name = str((issue.get("project") or {}).get("name", ""))
    if constraints.get("excluded_customer_aliases") and text_matches_customer_aliases(
        project_name,
        constraints["excluded_customer_aliases"],
    ):
        return False
    if constraints.get("platform_aliases") and not issue_matches_platform(
        issue,
        constraints["platform_aliases"],
        search_text=search_text,
    ):
        return False
    if constraints.get("excluded_platform_aliases") and issue_matches_platform(
        issue,
        constraints["excluded_platform_aliases"],
        search_text=search_text,
    ):
        return False
    combined_text = search_text if search_text is not None else issue_to_search_text(issue)
    status_text = str((issue.get("status") or {}).get("name") or (issue.get("status") or {}).get("label", ""))
    if not text_matches_status(
        status_text,
        constraints.get("statuses", []),
    ):
        return False
    if constraints.get("excluded_statuses") and text_matches_status(
        status_text,
        constraints["excluded_statuses"],
    ):
        return False
    if constraints.get("issue_ids") and issue.get("id") not in constraints["issue_ids"]:
        return False
    if not issue_matches_screen_numbers(issue, constraints.get("screen_numbers", [])):
        return False
    if not issue_matches_date_fields(issue, constraints.get("date_fields", {})):
        return False
    if not issue_matches_created_at(issue, constraints.get("created_at")):
        return False
    handler_text = str((issue.get("handler") or {}).get("real_name", ""))
    if constraints.get("unassigned_people") and handler_text.strip() not in {
        "",
        "담당자 미지정",
        "미지정",
        "미상",
    }:
        return False
    reporter_text = str((issue.get("reporter") or {}).get("real_name", ""))
    people = constraints.get("people", {})
    if people.get("role") == "effort_person":
        if not set(map(normalize_for_match, people.get("names", []))) & set(map(normalize_for_match, issue_effort_people(issue))):
            return False
    elif not text_matches_people(combined_text, people, handler_text, reporter_text):
        return False
    return matches_source_filters("mantis", {
        "customer": project_name,
        "platform": get_issue_custom_field_value(issue, "매체구분"),
        "status": status_text,
        "issue_id": str(issue.get("id") or ""),
        "screen_number": screen_filter_values(get_issue_custom_field_value(issue, "화면번호"), str(issue.get("summary") or "")),
        "created_at": issue.get("created_at", ""),
        "delivered_at": mantis_issue_effort_date(issue),
        "effort_person": issue_effort_people(issue),
        "title": str(issue.get("summary", "")), "keyword": combined_text,
        "owner": handler_text, "reporter": extract_reporter_names(combined_text, reporter_text),
        "registered_reporter": reporter_text,
    }, constraints.get("filter_clauses") or [])


@measured("filter")
def select_mantis_rows(rows, constraints, *, matcher=None):
    """목록·집계·보고서가 공유하는 원본 조건 검사."""
    match = matcher or issue_matches_constraints
    return [row for row in rows if match(row, constraints or {})]
