"""조회 조건을 Notion 속성 필터로 변환하는 순수 규칙."""
from typing import Any, Dict, Optional

from backend.query.notion_constraints import _OWNER_SUFFIXES
from backend.query_filters import normalize_filter_clauses, values_for_clause
from backend.utils.text import normalize_for_match


def merge_common_notion_constraints(
    notion_constraints: Dict[str, Any],
    constraints: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """공통 필터 계획을 Notion 검색/후처리 조건으로 연결한다."""
    merged = {
        **notion_constraints,
        "common_filter_clauses": normalize_filter_clauses((constraints or {}).get("filter_clauses") or []),
        "customer_aliases": list((constraints or {}).get("customer_aliases") or []),
        "excluded_statuses": list(
            dict.fromkeys(
                [
                    *(notion_constraints.get("excluded_statuses") or []),
                    *((constraints or {}).get("excluded_statuses") or []),
                ]
            )
        ),
    }
    if notion_constraints.get("native_status_constraint"):
        # Notion 원문 상태를 Mantis의 open/closed로 재해석한 공통 조건과
        # 교집합하면 실제 DB 옵션과 모순되어 정상 문서도 모두 사라진다.
        native_status = normalize_for_match(str(merged.get("status") or ""))
        merged["common_filter_clauses"] = [
            clause for clause in merged["common_filter_clauses"]
            if clause["field"] != "status" or (
                clause["operator"] == "in"
                and len(values_for_clause(clause.get("value"))) > 1
                and native_status in {
                    normalize_for_match(value) for value in values_for_clause(clause.get("value"))
                }
            )
        ]
        merged["excluded_statuses"] = list(notion_constraints.get("excluded_statuses") or [])
    for clause in merged["common_filter_clauses"]:
        values = values_for_clause(clause.get("value"))
        if clause["field"] == "customer" and clause["operator"] in {"equals", "in"}:
            aliases = [
                str(alias)
                for alias in merged.get("customer_aliases", [])
                if len(normalize_for_match(str(alias))) >= 3
            ]
            expanded = list(dict.fromkeys([*values, *aliases]))
            clause["operator"] = "in"
            clause["value"] = expanded
            values = expanded
        if clause["field"] == "owner" and merged.get("owner"):
            owner = str(merged["owner"])
            corrected = [owner if value in {owner + suffix for suffix in _OWNER_SUFFIXES} else value for value in values]
            if corrected != values:
                clause["value"] = corrected if isinstance(clause.get("value"), list) else corrected[0]
                values = corrected
        if clause["field"] == "status":
            if clause["operator"] in {"not_equals", "not_in"}:
                merged["excluded_statuses"].extend(values)
            elif clause["operator"] == "in" and len(values) > 1:
                # 원문 파서가 잡은 첫 값이 OR 후보에 포함돼도 단일 AND 조건으로 남기지 않는다.
                if normalize_for_match(str(merged.get("status") or "")) in {
                    normalize_for_match(value) for value in values
                }:
                    merged["status"] = ""
            elif clause["operator"] in {"equals", "in"} and values and not merged.get("status"):
                merged["status"] = values[0]
        elif clause["field"] == "owner" and clause["operator"] in {"equals", "in"}:
            if len(values) > 1:
                if normalize_for_match(str(merged.get("owner") or "")) in {
                    normalize_for_match(value) for value in values
                }:
                    merged["owner"] = ""
            elif values and not merged.get("owner"):
                merged["owner"] = values[0]
        elif clause["field"] == "database" and clause["operator"] in {"equals", "contains"} and values and not merged.get("database_label"):
            merged["database_label"] = values[0]
        elif clause["field"] == "title" and clause["operator"] == "equals" and values and not merged.get("exact_title"):
            merged["exact_title"] = values[0]
    merged["excluded_statuses"] = list(dict.fromkeys(merged["excluded_statuses"]))
    return merged
