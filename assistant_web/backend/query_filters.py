"""공통 검색 필터 정규화와 실행 감사 유틸리티.

이 모듈은 특정 데이터 소스를 알지 못한다. 플래너가 만든 조건을 안전한
``field/operator/value`` 형태로 제한하고, 각 소스 어댑터가 지원하지 못한
조건을 사용자에게 숨기지 않도록 분류한다.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


SUPPORTED_OPERATORS = {
    "equals",
    "not_equals",
    "in",
    "not_in",
    "contains",
    "not_contains",
    "exists",
    "not_exists",
    "gte",
    "lte",
    "between",
}

SUPPORTED_FIELDS = {
    "customer",
    "platform",
    "status",
    "owner",
    "reporter",
    "issue_id",
    "screen_number",
    "created_at",
    "title",
    "keyword",
    "database",
}

NEGATIVE_OPERATORS = {"not_equals", "not_in", "not_contains", "not_exists"}

SOURCE_FILTER_FIELDS = {
    "mantis": {
        "customer", "platform", "status", "owner", "reporter", "issue_id",
        "screen_number", "created_at", "title", "keyword",
    },
    "notion": {
        "customer", "platform", "status", "owner", "created_at", "title",
        "keyword", "database",
    },
    "chroma": {
        "customer", "platform", "status", "owner", "issue_id", "screen_number",
        "created_at", "title", "keyword",
    },
}


def _normalized_values(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [item for item in value if item not in (None, "")]
    return [value]


def normalize_filter_clause(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    field = str(raw.get("field") or "").strip().lower()
    operator = str(raw.get("operator") or raw.get("op") or "").strip().lower()
    if field not in SUPPORTED_FIELDS or operator not in SUPPORTED_OPERATORS:
        return None
    value = raw.get("value")
    if operator in {"in", "not_in", "between"}:
        value = _normalized_values(value)
        if not value:
            return None
    elif operator not in {"exists", "not_exists"} and value in (None, ""):
        return None
    return {"field": field, "operator": operator, "value": value}


def normalize_filter_clauses(raw: Any) -> List[Dict[str, Any]]:
    clauses: List[Dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        clause = normalize_filter_clause(item)
        if clause and clause not in clauses:
            clauses.append(clause)
    return clauses


def add_filter_clause(
    clauses: List[Dict[str, Any]],
    field: str,
    operator: str,
    value: Any = None,
) -> None:
    clause = normalize_filter_clause({"field": field, "operator": operator, "value": value})
    if clause and clause not in clauses:
        clauses.append(clause)


def filter_clause_label(clause: Dict[str, Any]) -> str:
    operator_labels = {
        "equals": "일치",
        "not_equals": "제외",
        "in": "포함",
        "not_in": "제외",
        "contains": "포함",
        "not_contains": "미포함",
        "exists": "값 있음",
        "not_exists": "값 없음",
        "gte": "이상",
        "lte": "이하",
        "between": "범위",
    }
    value = clause.get("value")
    if isinstance(value, list):
        value_text = ", ".join(str(item) for item in value)
    else:
        value_text = str(value) if value not in (None, "") else ""
    suffix = operator_labels.get(str(clause.get("operator")), str(clause.get("operator")))
    return " ".join(part for part in (str(clause.get("field")), suffix, value_text) if part)


def partition_supported_clauses(
    clauses: Iterable[Dict[str, Any]],
    supported_fields: Sequence[str],
    supported_operators: Optional[Sequence[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    fields = set(supported_fields)
    operators = set(supported_operators or SUPPORTED_OPERATORS)
    applied: List[Dict[str, Any]] = []
    unapplied: List[Dict[str, Any]] = []
    for clause in normalize_filter_clauses(list(clauses)):
        target = applied if clause["field"] in fields and clause["operator"] in operators else unapplied
        target.append(clause)
    return applied, unapplied


def values_for_clause(value: Any) -> List[str]:
    return [str(item) for item in _normalized_values(value)]


def filter_audit_for_source(source: str, clauses: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    family = "chroma" if "chroma" in source else "notion" if "notion" in source else "mantis"
    applied, unapplied = partition_supported_clauses(
        clauses,
        SOURCE_FILTER_FIELDS.get(family, ()),
    )
    return {
        "source_family": family,
        "applied_filters": applied,
        "unapplied_filters": unapplied,
        "fully_applied": not unapplied,
    }
