"""고객사·플랫폼·담당자·상태·식별자 추출 진입점."""

from backend.intent.core import (
    extract_excluded_issue_ids,
    extract_issue_ids,
    extract_mantis_keyword,
    extract_people_constraints,
    extract_screen_numbers,
    extract_status_constraints,
    extract_unknown_customers,
    get_customer_alias_groups,
    infer_person_role,
    load_known_people,
    load_project_alias_groups,
)


__all__ = [
    "extract_excluded_issue_ids",
    "extract_issue_ids",
    "extract_mantis_keyword",
    "extract_people_constraints",
    "extract_screen_numbers",
    "extract_status_constraints",
    "extract_unknown_customers",
    "get_customer_alias_groups",
    "infer_person_role",
    "load_known_people",
    "load_project_alias_groups",
]
