"""속성 조건을 먼저 확인한 뒤 필요한 원격 담당자만 조회한다."""
from typing import Any, Dict
from backend.utils.text import normalize_for_match
from backend.notion.mapping import resolve_notion_page_owner
from backend.notion.matching import notion_page_matches_constraints


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
