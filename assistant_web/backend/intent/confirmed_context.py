"""인증된 감사 기록의 확정 조건을 후속 질문 문맥으로 복원한다."""

from backend.config import STATUS_ALIASES
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

CONFIRMED_CONSTRAINTS: ContextVar[tuple | None] = ContextVar("confirmed_constraints", default=None)


@contextmanager
def confirmed_constraints_scope(question: str, constraints: dict):
    token = CONFIRMED_CONSTRAINTS.set((question, deepcopy(constraints)))
    try:
        yield
    finally:
        CONFIRMED_CONSTRAINTS.reset(token)


def merge_confirmed_constraints(previous: dict, current: dict, question: str) -> dict:
    """서버가 확정한 조건을 보존하고 이번 턴에서 명시한 차원만 교체한다.

    절 사이 AND, in/not_in 값 사이 OR 계약을 문자열로 재해석하지 않는다.
    """
    from backend.intent.status_semantics import ALL_STATUSES
    result = deepcopy(previous)
    result.pop("status_resolution", None)
    groups = {
        "customer": ("customer_key", "customer_exact", "customer_aliases", "excluded_customer_keys", "excluded_customer_aliases", "unknown_customers"),
        "platform": ("platform_key", "platform_aliases", "excluded_platform_keys", "excluded_platform_aliases"),
        "status": ("statuses", "excluded_statuses"),
        "owner": ("people", "unassigned_people"),
        "issue_id": ("issue_ids", "exclude_issue_ids", "max_issue_id_exclusive"),
        "screen_number": ("screen_numbers",),
        "created_at": ("created_at",),
    }
    replaced = set()
    for field, keys in groups.items():
        explicit = any(bool(current.get(key)) for key in keys if key not in {"people", "customer_aliases", "platform_aliases"})
        if field == "owner":
            explicit = bool((current.get("people") or {}).get("names") or current.get("unassigned_people"))
        if field == "status":
            explicit |= bool(ALL_STATUSES.search(question))
        if explicit:
            replaced.add(field)
            if field == "owner":
                replaced.add("reporter")
            for key in keys:
                result.pop(key, None)
                if key in current:
                    result[key] = deepcopy(current[key])
    for key in ("mantis_team_members", "mantis_team_label"):
        if current.get(key):
            result[key] = deepcopy(current[key])
    if current.get("date_fields"):
        result["date_fields"] = {**result.get("date_fields", {}), **deepcopy(current["date_fields"])}
    clauses = [deepcopy(c) for c in previous.get("filter_clauses") or [] if c["field"] not in replaced]
    for clause in current.get("filter_clauses") or []:
        if clause not in clauses:
            clauses.append(deepcopy(clause))
    if result.get("inherited_terms"):
        keyword_clause = {"field": "keyword", "operator": "contains", "value": deepcopy(result["inherited_terms"])}
        if keyword_clause not in clauses:
            clauses.append(keyword_clause)
    result["filter_clauses"] = clauses
    return result


def confirmed_scope_text(constraints: dict) -> str:
    parts = []
    if constraints.get("mantis_team_members"):
        parts.append("우리 팀")
    if constraints.get("customer_exact") or constraints.get("customer_key"):
        parts.append(constraints.get("customer_exact") or constraints["customer_key"])
    if constraints.get("platform_key"):
        parts.append(constraints["platform_key"])
    parts.extend(STATUS_ALIASES.get(s, [s])[0] for s in constraints.get("statuses") or [])
    names = (constraints.get("people") or {}).get("names") or []
    if names:
        role = "요청자" if (constraints.get("people") or {}).get("role") == "reporter" else "담당자"
        parts.append(role + " " + ", ".join(names))
    if constraints.get("unassigned_people"):
        parts.append("담당자 미지정")
    parts.extend(f"#{value}" for value in constraints.get("issue_ids") or [])
    parts.extend(f"화면번호 {value}" for value in constraints.get("screen_numbers") or [])
    period = constraints.get("created_at") or {}
    if period.get("from") and period.get("to"):
        parts.append(f"{period['from']}부터 {period['to']}까지 등록")
    parts.extend(f"{key} {value}" for key, value in (constraints.get("date_fields") or {}).items())
    for key in ("excluded_customer_keys", "excluded_platform_keys", "excluded_statuses"):
        for value in constraints.get(key) or []:
            if key == "excluded_statuses":
                value = STATUS_ALIASES.get(value, [value])[0]
            parts.append(f"{value} 제외")
    parts.extend(f"#{value} 제외" for value in constraints.get("exclude_issue_ids") or [])
    return " ".join(parts)
