"""인증된 감사 기록의 확정 조건을 후속 질문 문맥으로 복원한다."""

from backend.config import STATUS_ALIASES


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
