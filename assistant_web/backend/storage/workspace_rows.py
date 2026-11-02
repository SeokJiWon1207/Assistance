"""업무 화면과 팀 브리핑이 공유하는 이슈 행 변환."""
from datetime import date
from typing import Any, Dict, Optional
from backend.display import customer_name


def _parse_date(value: Any) -> Optional[date]:
    raw = str(value or "")[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def _is_open(status: str) -> bool:
    from backend.mantis_status import mantis_status_bucket
    return mantis_status_bucket(status) == "open"


def _due_date(issue: Dict[str, Any]) -> str:
    date_fields = issue.get("date_fields") or {}
    preferred = ("완료예정일", "수정예정일", "배포예정일", "처리예정일", "마감일")
    for field_name in preferred:
        if date_fields.get(field_name):
            return str(date_fields[field_name])[:10]
    for field_name, value in date_fields.items():
        if any(keyword in str(field_name) for keyword in ("예정", "마감", "완료")) and value:
            return str(value)[:10]
    return ""


def _issue_summary(issue: Dict[str, Any]) -> Dict[str, Any]:
    due = _due_date(issue)
    today = date.today()
    due_value = _parse_date(due)
    created = _parse_date(issue.get("created_at"))
    age_days = (today - created).days if created else None
    return {
        "id": issue.get("id"),
        "title": issue.get("summary", "제목 없음"),
        "project": issue.get("project", "미상"),
        "customer_name": customer_name(issue.get("project")),
        "status": issue.get("status", "상태 미상"),
        "isOpen": _is_open(issue.get("status", "")),
        "priority": issue.get("priority", ""),
        "handler": issue.get("handler", "담당자 미지정"),
        "createdAt": issue.get("created_at", ""),
        "updatedAt": issue.get("updated_at", ""),
        "dueDate": due,
        "overdue": bool(due_value and due_value < today and _is_open(issue.get("status", ""))),
        "ageDays": age_days,
        "screenNumber": issue.get("screen_number", ""),
    }
