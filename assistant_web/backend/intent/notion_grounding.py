"""독립 진입점에서 공통 조건을 한 번 해석한 후 순수 Notion 근거 규칙을 적용한다."""
from typing import Any
from backend.intent.constraints import extract_query_constraints
from backend.query.notion_text import grounded_notion_body_constraints as merge_grounded_constraints, notion_structural_query


def grounded_notion_body_constraints(text: str, constraints: dict[str, Any]) -> dict[str, Any]:
    grounded = extract_query_constraints(notion_structural_query(text))
    return merge_grounded_constraints(text, constraints, grounded=grounded)
