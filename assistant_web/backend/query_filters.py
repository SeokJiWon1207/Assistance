"""공통 검색 필터 정규화와 실행 감사 유틸리티.

이 모듈은 특정 데이터 소스를 알지 못한다. 플래너가 만든 조건을 안전한
``field/operator/value`` 형태로 제한하고, 각 소스 어댑터가 지원하지 못한
조건을 사용자에게 숨기지 않도록 분류한다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from backend.common import normalize_for_match, text_matches_customer_aliases, text_matches_any_alias
from backend.config import CUSTOMER_ALIASES, PLATFORM_ALIASES, STATUS_MATCH_ALIASES
from backend.mantis_status import matches_mantis_status

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
    "registered_reporter",
    "issue_id",
    "screen_number",
    "created_at",
    "due_at",
    "deployed_at",
    "title",
    "keyword",
    "database",
}

NEGATIVE_OPERATORS = {"not_equals", "not_in", "not_contains", "not_exists"}

SOURCE_FILTER_FIELDS = {
    "mantis": {
        "customer",
        "platform",
        "status",
        "owner",
        "reporter",
        "registered_reporter",
        "issue_id",
        "screen_number",
        "created_at",
        "title",
        "keyword",
    },
    "notion": {
        "customer",
        "platform",
        "status",
        "owner",
        "created_at",
        "due_at",
        "deployed_at",
        "title",
        "keyword",
        "database",
    },
    "chroma": {
        "customer",
        "platform",
        "status",
        "owner",
        "reporter",
        "registered_reporter",
        "issue_id",
        "screen_number",
        "created_at",
        "title",
        "keyword",
    },
}


TEXT_OPERATORS = {"equals", "not_equals", "in", "not_in", "contains", "not_contains", "exists", "not_exists"}
SOURCE_FILTER_OPERATORS = {
    family: {
        field: (TEXT_OPERATORS | {"gte", "lte", "between"} if field in {"created_at", "due_at", "deployed_at"} else TEXT_OPERATORS)
        for field in fields
    }
    for family, fields in SOURCE_FILTER_FIELDS.items()
}


def clause_supported(source: str, clause: Dict[str, Any]) -> bool:
    field, operator = clause["field"], clause["operator"]
    if operator not in SOURCE_FILTER_OPERATORS.get(source, {}).get(field, set()):
        return False
    if field in {"created_at", "due_at", "deployed_at"} and operator in {"gte", "lte", "between"}:
        values = values_for_clause(clause.get("value"))
        if len(values) != (2 if operator == "between" else 1):
            return False
        try:
            dates = [date.fromisoformat(value) for value in values]
            return len(dates) == 1 or dates[0] <= dates[1]
        except ValueError:
            return False
    return True


def _canonical_filter_value(field: str, value: Any) -> str:
    normalized = normalize_for_match(str(value or ""))
    if field == "issue_id" and normalized.lstrip("#").isdigit():
        return str(int(normalized.lstrip("#")))
    aliases = {"customer": CUSTOMER_ALIASES, "platform": PLATFORM_ALIASES, "status": STATUS_MATCH_ALIASES}.get(
        field, {}
    )
    for key, variants in aliases.items():
        if normalized in {normalize_for_match(str(item)) for item in [key, *variants]} or (
            field == "customer" and text_matches_customer_aliases(str(value), [key, *variants])
        ):
            return normalize_for_match(key)
    return normalized


def matches_source_filters(source: str, fields: Dict[str, Any], clauses: Any) -> bool:
    """실행기와 감사가 같은 필드/연산자 계약을 사용한다."""
    for clause in normalize_filter_clauses(clauses):
        if not clause_supported(source, clause):
            continue
        field, operator = clause["field"], clause["operator"]
        raw = fields.get(field, "")
        candidates = raw if isinstance(raw, list) else [raw]
        values = values_for_clause(clause.get("value"))
        if field in {"created_at", "due_at", "deployed_at"}:
            normalized_dates = []
            for item in candidates:
                raw_date = str(item or "")
                if source == "notion" and field == "created_at" and "T" in raw_date:
                    try:
                        parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)))
                        raw_date = parsed.astimezone(timezone(timedelta(hours=9))).date().isoformat()
                    except ValueError:
                        pass
                normalized_dates.append(raw_date[:10])
            candidates = normalized_dates
        raw_candidates, raw_values = candidates, values
        candidates = [_canonical_filter_value(field, item) for item in candidates if item not in (None, "")]
        values = [_canonical_filter_value(field, item) for item in values]
        present = any(candidates)
        equal = any(candidate == value for candidate in candidates for value in values)
        contains = any(
            normalize_for_match(str(value)) in normalize_for_match(str(candidate))
            for candidate in raw_candidates
            for value in raw_values
        )
        if field == "status":
            equal = any(
                (text_matches_any_alias(str(candidate), STATUS_MATCH_ALIASES.get(value, [value]))
                 if source == "notion" else matches_mantis_status(str(candidate), str(value)))
                for candidate in raw_candidates
                for value in raw_values
            )
        elif field == "customer":
            # 양쪽을 같은 고객사 키로 정규화한 비교를 보존한다. 표면형만
            # 재검사하면 DB증권과 101PopcornRenewal(동부증권)이 갈라진다.
            equal = equal or any(
                text_matches_customer_aliases(str(candidate), [value, _canonical_filter_value(field, value)])
                for candidate in raw_candidates
                for value in raw_values
            )
        elif field == "platform":
            equal = any(
                text_matches_any_alias(str(candidate), PLATFORM_ALIASES.get(value, [value]))
                for candidate in raw_candidates
                for value in raw_values
            )
        if operator in {"equals", "in"} and not equal:
            return False
        if operator in {"not_equals", "not_in"} and equal:
            return False
        if operator == "contains" and not contains:
            return False
        if operator == "not_contains" and contains:
            return False
        if operator == "exists" and not present:
            return False
        if operator == "not_exists" and present:
            return False
        if operator == "gte" and (not present or not any(candidate >= values[0] for candidate in candidates)):
            return False
        if operator == "lte" and (not present or not any(candidate <= values[0] for candidate in candidates)):
            return False
        if operator == "between" and (
            not present or not any(values[0] <= candidate <= values[1] for candidate in candidates)
        ):
            return False
    return True


def screen_filter_values(value: str, title: str) -> List[str]:
    return [value, *re.findall(r"\[\s*(\d+)\s*\]", title)]


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
    field_label = {"due_at": "기한", "deployed_at": "운영반영일"}.get(str(clause.get("field")), str(clause.get("field")))
    return " ".join(part for part in (field_label, suffix, value_text) if part)


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
    applied: List[Dict[str, Any]] = []
    unapplied: List[Dict[str, Any]] = []
    for clause in normalize_filter_clauses(list(clauses)):
        (applied if clause_supported(family, clause) else unapplied).append(clause)
    return {
        "source_family": family,
        "applied_filters": applied,
        "unapplied_filters": unapplied,
        "fully_applied": not unapplied,
    }


def notion_temporal_field(question: str) -> str:
    """명시된 기한/반영일만 등록일과 구분한다."""
    from backend.common import remove_quoted_value

    text = remove_quoted_value(question)

    # 날짜 필드 이름은 독립된 어절일 때만 인정한다. ``배포일정``처럼
    # 같은 글자로 시작하는 업무 명사를 배포일 조건으로 바꾸면, 질문의
    # 상대 날짜까지 잘못된 필드에 적용된다. 조사와 서술형 어미는 명시적으로
    # 허용해 ``기한이``, ``마감인``, ``운영 반영된`` 같은 표현은 유지한다.
    suffix = r"(?:은|는|이|가|을|를|에|의|까지|부터|으로|로|인|된|되는|될|한|했던)?"
    boundary = r"(?=\s|$|[,.;!?])"

    created_marker = re.search(
        rf"(?:등록일|접수일|생성일|작성일){suffix}{boundary}"
        rf"|(?:등록|접수|생성|작성)(?:된|한|했|되는|될){boundary}"
        rf"|만든{boundary}",
        text,
    )
    due_marker = re.search(rf"(?:마감일?|기한){suffix}{boundary}", text)
    deployed_marker = re.search(rf"(?:운영\s*반영일?|배포일){suffix}{boundary}", text)

    # 날짜가 비어 있는 항목을 찾는 표현은 기간 기준을 고르는 요청이 아니다.
    absence = r"(?:없는|없음|없이|미지정|정해지지\s*않은|안\s*정해진)"
    due_absence = re.search(rf"(?:마감일?|기한)(?:은|는|이|가)?\s*{absence}", text)
    deployed_absence = re.search(
        rf"(?:운영\s*반영일?|배포일)(?:은|는|이|가)?\s*{absence}", text,
    )

    if created_marker:
        return "created_at"
    if due_marker and not due_absence:
        return "due_at"
    if deployed_marker and not deployed_absence:
        return "deployed_at"
    return "created_at"
