"""역할·조회 기간에 맞춘 운영 대시보드 통합 리포트."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

from backend.common import normalize_for_match
from backend.reports.customer import _health_content, _is_closed
from backend.reports.team_brief import (
    TEAM_BRIEF_AGING_DAYS,
    _decision_summary,
    _is_high_priority,
    _load_issues,
    _parse_issue_date,
    _priority_content,
    _spikes,
)
from backend.workspace import (
    _issue_summary,
    _is_open,
    _matches_person,
    _now,
    user_mantis_name,
)


DASHBOARD_PERIOD_OPTIONS = {7, 14, 30, 90}
DASHBOARD_CUSTOMER_LIMIT = 12
DASHBOARD_PRIORITY_LIMIT = 30
DASHBOARD_TODO_LIMIT = 6
HEALTH_LEVEL_ORDER = {"stable": 0, "attention": 1, "high": 2}


def _higher_health_level(current: str, candidate: str) -> str:
    return candidate if HEALTH_LEVEL_ORDER[candidate] > HEALTH_LEVEL_ORDER[current] else current


def _period_bounds(today: date, days: int) -> tuple[date, date, date, date]:
    current_start = today - timedelta(days=days - 1)
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    return current_start, today, previous_start, previous_end


def _inside(issue: Dict[str, Any], start: date, end: date) -> bool:
    created = _parse_issue_date(issue.get("created_at"))
    return bool(created and start <= created <= end)


def _summary_rows(issues: Iterable[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for issue in issues:
        item = _issue_summary(issue)
        created = _parse_issue_date(item.get("createdAt"))
        due = _parse_issue_date(item.get("dueDate"))
        item["ageDays"] = (today - created).days if created and created <= today else None
        item["overdue"] = bool(due and due < today and _is_open(str(item.get("status") or "")))
        rows.append(item)
    return rows


def _is_unassigned(item: Dict[str, Any]) -> bool:
    return str(item.get("handler") or "").strip() in {"", "담당자 미지정", "미지정", "미상"}


def _priorities(open_rows: List[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
    candidates = [
        row
        for row in open_rows
        if row.get("overdue")
        or int(row.get("ageDays") or 0) >= TEAM_BRIEF_AGING_DAYS
        or _is_unassigned(row)
        or _is_high_priority(row)
    ]
    enriched = [_priority_content(row, today) for row in candidates]
    enriched.sort(
        key=lambda row: (
            -int(row.get("riskScore") or 0),
            -int(bool(row.get("overdue"))),
            -int(row.get("ageDays") or 0),
            str(row.get("updatedAt") or ""),
        )
    )
    return enriched[:DASHBOARD_PRIORITY_LIMIT]


def _todo_rows(open_rows: List[Dict[str, Any]], priorities: List[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
    priority_ids = {str(row.get("id")) for row in priorities}
    remaining = [_priority_content(row, today) for row in open_rows if str(row.get("id")) not in priority_ids]
    remaining.sort(key=lambda row: str(row.get("updatedAt") or ""), reverse=True)
    return (priorities + remaining)[:DASHBOARD_TODO_LIMIT]


def _handling_days(issues: Iterable[Dict[str, Any]]) -> List[int]:
    values: List[int] = []
    for issue in issues:
        if not _is_closed(issue.get("status")):
            continue
        created = _parse_issue_date(issue.get("created_at"))
        updated = _parse_issue_date(issue.get("updated_at"))
        if created and updated and updated >= created:
            values.append((updated - created).days)
    return values


def _monthly_counts(issues: Iterable[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = defaultdict(int)
    for issue in issues:
        month = str(issue.get("created_at") or "")[:7]
        if len(month) == 7:
            counts[month] += 1
    return counts


def _customer_health(
    issues: List[Dict[str, Any]],
    today: date,
    current_start: date,
    previous_start: date,
    previous_end: date,
) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for issue in issues:
        customer = str(issue.get("project") or "").strip()
        if customer and customer not in {"미상", "기타/미상"}:
            grouped[customer].append(issue)

    labels = {"stable": "안정", "attention": "주의", "high": "위험"}
    rows: List[Dict[str, Any]] = []
    for customer, customer_issues in grouped.items():
        summaries = _summary_rows(customer_issues, today)
        open_rows = [row for row in summaries if _is_open(str(row.get("status") or ""))]
        aging_rows = [row for row in open_rows if int(row.get("ageDays") or 0) >= TEAM_BRIEF_AGING_DAYS]
        overdue_count = sum(bool(row.get("overdue")) for row in open_rows)
        unassigned_count = sum(_is_unassigned(row) for row in open_rows)
        handling_days = _handling_days(customer_issues)
        health = _health_content(
            customer_issues,
            open_rows,
            aging_rows,
            handling_days,
            _monthly_counts(customer_issues),
        )
        level = {"안정": "stable", "주의": "attention", "위험": "high"}.get(str(health.get("level") or ""), "stable")
        recent_count = sum(_inside(issue, current_start, today) for issue in customer_issues)
        previous_count = sum(_inside(issue, previous_start, previous_end) for issue in customer_issues)
        reasons = list(health.get("reasons") or [])

        if overdue_count:
            reasons.insert(0, f"일정 지연 {overdue_count}건")
            level = "high" if overdue_count >= 2 else _higher_health_level(level, "attention")
        if unassigned_count:
            reasons.append(f"담당자 미지정 {unassigned_count}건")
            level = _higher_health_level(level, "attention")
        growing = recent_count >= 2 and (previous_count == 0 or recent_count >= previous_count * 2)
        if growing:
            reasons.append(f"최근 이슈 {previous_count}건→{recent_count}건")
            level = _higher_health_level(level, "attention")

        average_handling = round(sum(handling_days) / len(handling_days), 1) if handling_days else None
        unique_reasons = list(dict.fromkeys(str(reason) for reason in reasons if reason))[:3]
        rows.append(
            {
                "customer": customer,
                "level": level,
                "label": labels[level],
                "openCount": len(open_rows),
                "overdueCount": overdue_count,
                "agingCount": len(aging_rows),
                "unassignedCount": unassigned_count,
                "averageHandlingDays": average_handling,
                "recentCount": recent_count,
                "previousCount": previous_count,
                "trend": "worsening"
                if recent_count > previous_count
                else ("improving" if recent_count < previous_count else "steady"),
                "reasons": unique_reasons,
            }
        )

    rows.sort(
        key=lambda row: (
            -HEALTH_LEVEL_ORDER[str(row["level"])],
            -int(row["overdueCount"]),
            -int(row["agingCount"]),
            -int(row["recentCount"]),
            str(row["customer"]),
        )
    )
    return rows[:DASHBOARD_CUSTOMER_LIMIT]


def _member_rows(users: List[Dict[str, Any]], issues: List[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
    representatives: Dict[str, Dict[str, Any]] = {}
    for user in (item for item in users if item.get("approved")):
        person = user_mantis_name(user)
        key = normalize_for_match(person) or f"uid:{user.get('uid')}"
        current = representatives.get(key)
        if not current or normalize_for_match(str(user.get("displayName") or "")) == normalize_for_match(person):
            representatives[key] = user

    members: List[Dict[str, Any]] = []
    for user in representatives.values():
        person = user_mantis_name(user)
        assigned = [issue for issue in issues if person and _matches_person(str(issue.get("handler") or ""), person)]
        open_rows = [row for row in _summary_rows(assigned, today) if _is_open(str(row.get("status") or ""))]
        priorities = _priorities(open_rows, today)
        members.append(
            {
                "displayName": user.get("displayName") or person or "이름 미등록",
                "mantisUsername": person,
                "role": user.get("role") or "member",
                "open": len(open_rows),
                "overdue": sum(bool(row.get("overdue")) for row in open_rows),
                "aging": sum(int(row.get("ageDays") or 0) >= TEAM_BRIEF_AGING_DAYS for row in open_rows),
                "highRisk": sum(row.get("riskLevel") == "high" for row in priorities),
            }
        )
    members.sort(
        key=lambda row: (
            -int(row["highRisk"]),
            -int(row["overdue"]),
            -int(row["open"]),
            str(row["displayName"]),
        )
    )
    return members


def _scope_issues(user: Dict[str, Any], issues: List[Dict[str, Any]]) -> tuple[str, str, List[Dict[str, Any]]]:
    person = user_mantis_name(user)
    return "team", person, issues


def _period_decision_summary(
    summary: Dict[str, Any],
    customer_spikes: List[Dict[str, Any]],
    screen_spikes: List[Dict[str, Any]],
    priorities: List[Dict[str, Any]],
    days: int,
) -> Dict[str, Any]:
    """주간 브리핑 문구를 선택한 비교 기간에 맞게 조정한다."""
    decision = _decision_summary(summary, customer_spikes, screen_spikes, priorities)

    def period_copy(value: Any) -> str:
        return str(value or "").replace("지난주", f"최근 {days}일").replace("직전 주", "직전 동일 기간")

    decision["headline"] = period_copy(decision.get("headline"))
    decision["recommendedActions"] = [
        {**action, "question": period_copy(action.get("question"))}
        for action in decision.get("recommendedActions") or []
    ]
    return decision


def build_operations_dashboard(
    user: Dict[str, Any],
    *,
    users: Optional[List[Dict[str, Any]]] = None,
    days: int = 30,
    today: Optional[date] = None,
    refresh: bool = False,
    live: bool = False,
) -> Dict[str, Any]:
    """대시보드 한 화면에 필요한 데이터를 같은 조회 기준으로 계산한다."""
    if days not in DASHBOARD_PERIOD_OPTIONS:
        raise ValueError("조회 기간은 7일, 14일, 30일, 90일 중 하나여야 합니다.")
    base = today or date.today()
    current_start, current_end, previous_start, previous_end = _period_bounds(base, days)
    issues, source = _load_issues(prefer_snapshot=not (refresh or live), force_refresh=refresh)
    scope, person, scoped_issues = _scope_issues(user, issues)
    scoped_open = [row for row in _summary_rows(scoped_issues, base) if _is_open(str(row.get("status") or ""))]
    priorities = _priorities(scoped_open, base)
    recent = [issue for issue in issues if _inside(issue, current_start, current_end)]
    previous = [issue for issue in issues if _inside(issue, previous_start, previous_end)]
    customer_spikes = _spikes(recent, previous, "project")
    screen_spikes = _spikes(recent, previous, "screen_number")
    scoped_recent = [issue for issue in scoped_issues if _inside(issue, current_start, current_end)]
    scoped_previous = [issue for issue in scoped_issues if _inside(issue, previous_start, previous_end)]
    change_rate = (
        None
        if not scoped_previous
        else round((len(scoped_recent) - len(scoped_previous)) / len(scoped_previous) * 100, 1)
    )
    summary = {
        "open": len(scoped_open),
        "overdue": sum(bool(row.get("overdue")) for row in scoped_open),
        "aging": sum(int(row.get("ageDays") or 0) >= TEAM_BRIEF_AGING_DAYS for row in scoped_open),
        "highRisk": sum(row.get("riskLevel") == "high" for row in priorities),
        "highPriority": sum(_is_high_priority(row) for row in scoped_open),
        "unassigned": sum(_is_unassigned(row) for row in scoped_open),
        "newIssues": len(scoped_recent),
        "previousNewIssues": len(scoped_previous),
        "changeRate": change_rate,
        "carriedOpen": sum((_parse_issue_date(row.get("createdAt")) or base) < current_start for row in scoped_open),
        "closedNow": sum(not _is_open(str(issue.get("status") or "")) for issue in scoped_recent),
    }
    decision = _period_decision_summary(summary, customer_spikes, screen_spikes, priorities, days)
    role = str(user.get("role") or "member")
    member_rows = _member_rows(users or [], issues, base)
    health = _customer_health(issues, base, current_start, previous_start, previous_end)
    health_counts = Counter(str(row.get("level") or "stable") for row in health)
    return {
        "meta": {
            "source": source.get("status") or "error",
            "sourceReason": source.get("reason") or "",
            "generatedAt": _now(),
            "periodDays": days,
            "periodFrom": current_start.isoformat(),
            "periodTo": current_end.isoformat(),
            "comparisonFrom": previous_start.isoformat(),
            "comparisonTo": previous_end.isoformat(),
            "scope": scope,
            "loadedIssues": len(issues),
        },
        "user": {
            "displayName": user.get("displayName") or user.get("email") or "사용자",
            "role": role,
            "mantisUsername": person,
            "linked": bool(person),
        },
        "summary": summary,
        "decisionSummary": decision,
        "todos": _todo_rows(scoped_open, priorities, base),
        "priorities": priorities,
        "customerSpikes": customer_spikes,
        "screenSpikes": screen_spikes,
        "customerHealth": health,
        "customerHealthSummary": {
            "high": health_counts.get("high", 0),
            "attention": health_counts.get("attention", 0),
            "stable": health_counts.get("stable", 0),
        },
        "members": member_rows,
    }
