"""인증된 감사 기록의 확정 조건을 후속 질문 문맥으로 복원한다."""

from backend.config import STATUS_ALIASES
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

CONFIRMED_CONSTRAINTS: ContextVar[tuple | None] = ContextVar("confirmed_constraints", default=None)
CONFIRMED_TERMS_VERSION = 1


@contextmanager
def confirmed_constraints_scope(question: str, constraints: dict):
    token = CONFIRMED_CONSTRAINTS.set((question, deepcopy(constraints)))
    try:
        yield
    finally:
        CONFIRMED_CONSTRAINTS.reset(token)


def restore_confirmed_constraints(plan: dict) -> dict:
    """확인 대기 중인 해석 결과를 새 검색 조건으로 승격하지 않는다."""
    constraints = deepcopy(plan.get("resolved_constraints") or {})
    resolution = plan.get("status_resolution") or constraints.get("status_resolution") or {}
    terms = constraints.get("inherited_terms") or []
    if terms and constraints.get("confirmed_terms_version") != CONFIRMED_TERMS_VERSION:
        # 구버전은 미확정 표현과 확정 검색어를 같은 필드에 저장했다. 이미
        # 상태를 선택한 턴도 오염됐을 수 있으므로 상태 확인 여부로 추측하지 않는다.
        constraints["unconfirmed_search_terms"] = deepcopy(terms)
    if not resolution.get("needs_clarification"):
        terms = plan.get("terms") or terms
    # 확인 질문의 terms에는 '아직 손봐야 할' 같은 미확정 상태 표현도 있다.
    # 이전 검색에서 이미 확정된 검색어와 명시적 keyword/title 절은 그대로
    # 보존하되, 이번에 해석하지 못한 표현을 keyword 필터로 만들지는 않는다.
    constraints["inherited_terms"] = deepcopy(terms)
    if not constraints.get("unconfirmed_search_terms"):
        constraints["confirmed_terms_version"] = CONFIRMED_TERMS_VERSION
    return constraints


def confirmed_keyword_text(constraints: dict) -> str:
    """실행할 확정 검색어를 검색 기준 문장에도 표시한다."""
    if constraints.get("unconfirmed_search_terms"):
        return ""
    parts = []
    clauses = list(constraints.get("filter_clauses") or [])
    if constraints.get("inherited_terms"):
        clauses.append({"field": "keyword", "operator": "contains", "value": constraints["inherited_terms"]})
    for clause in clauses:
        label = {"title": "제목", "keyword": "키워드"}.get(clause.get("field"))
        if not label or clause.get("operator") not in {
            "equals", "in", "contains", "not_equals", "not_in", "not_contains",
        }:
            continue
        value = clause.get("value")
        values = value if isinstance(value, list) else [value]
        words = ", ".join(f"'{item}'" for item in values if item not in (None, ""))
        if words:
            direction = "제외" if clause["operator"].startswith("not_") else "포함"
            parts.append(f"{label} {words} {direction}")
    return " ".join(dict.fromkeys(parts))


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
    if result.get("unconfirmed_search_terms") and any(
        clause.get("field") in {"keyword", "title"}
        for clause in current.get("filter_clauses") or []
    ):
        # 사용자가 명시한 새 검색어로만 복구한다. 추측한 단어를 삭제하고
        # 조회를 강행하지 않으며, 새 독립 질문은 기존 조건을 처음부터 상속하지 않는다.
        replaced.update({"keyword", "title"})
        result.pop("unconfirmed_search_terms", None)
        result["inherited_terms"] = []
        result["confirmed_terms_version"] = CONFIRMED_TERMS_VERSION
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
    # '확인/해결/검토'를 일반 동사로 재해석하지 않도록 상태임을 명시한다.
    parts.extend(f"상태 {STATUS_ALIASES.get(s, [s])[0]}" for s in constraints.get("statuses") or [])
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
    keyword_text = confirmed_keyword_text(constraints)
    if keyword_text:
        parts.append(keyword_text)
    return " ".join(parts)
