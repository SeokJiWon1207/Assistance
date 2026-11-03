"""팀 대시보드에서 연 고객사 한 곳의 상세 현황."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from backend.display import customer_name
from backend.domain.team import TEAM_SCOPE_LABEL, filter_team_issues
from backend.playbooks import list_playbooks
from backend.reports.customer import _is_closed
from backend.reports.dashboard import (
    DASHBOARD_PERIOD_OPTIONS,
    _customer_health,
    _handling_days,
    _inside,
    _period_bounds,
    _summary_rows,
)
from backend.reports.team_brief import TEAM_BRIEF_AGING_DAYS, _load_issues, _parse_issue_date, _priority_content
from backend.workspace import _is_open, _now


CUSTOMER_DETAIL_MONTHS = 6
CUSTOMER_DETAIL_SCREEN_LIMIT = 5
CUSTOMER_DETAIL_OPEN_LIMIT = 50
CUSTOMER_DETAIL_PLAYBOOK_LIMIT = 5
CUSTOMER_NAME_MAX_LENGTH = 120
UNKNOWN_VALUES = {"", "미상", "기타/미상"}


def _month_keys(today: date, months: int) -> List[str]:
    keys: List[str] = []
    year, month = today.year, today.month
    for _ in range(months):
        keys.append(f"{year:04d}-{month:02d}")
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    return list(reversed(keys))


def _monthly_flow(issues: Iterable[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
    """월별 신규는 등록일, 완료는 해결·종료 이슈의 마지막 수정일 기준이다."""
    keys = _month_keys(today, CUSTOMER_DETAIL_MONTHS)
    created: Counter[str] = Counter()
    closed: Counter[str] = Counter()
    for issue in issues:
        created[str(issue.get("created_at") or "")[:7]] += 1
        if _is_closed(issue.get("status")):
            closed[str(issue.get("updated_at") or "")[:7]] += 1
    return [
        {"month": key, "label": f"{int(key[5:])}월", "newCount": created.get(key, 0), "closedCount": closed.get(key, 0)}
        for key in keys
    ]


def _ranked_counts(issues: Iterable[Dict[str, Any]], field: str, unknown_label: str = "") -> List[Dict[str, Any]]:
    counts: Counter[str] = Counter()
    for issue in issues:
        value = str(issue.get(field) or "").strip()
        if value in UNKNOWN_VALUES:
            if not unknown_label:
                continue
            value = unknown_label
        counts[value] += 1
    return [{"name": name, "count": count} for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def _average(values: List[int]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def _related_playbooks(
    issue_ids: set[str], loader: Callable[..., List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """공개 플레이북 중 근거 이슈가 이 고객사 이슈와 겹치는 것만 보여 준다."""
    rows: List[Dict[str, Any]] = []
    for playbook in loader(limit=200):
        matched = [str(value) for value in playbook.get("sourceIssueIds") or [] if str(value) in issue_ids]
        if not matched:
            continue
        rows.append({
            "id": playbook.get("id"),
            "title": playbook.get("title") or "제목 없음",
            "symptom": playbook.get("symptom") or "",
            "matchedIssueIds": matched,
            "sourceIssueCount": len(playbook.get("sourceIssueIds") or []),
            "updatedAt": playbook.get("updatedAt") or "",
        })
    rows.sort(key=lambda row: str(row["updatedAt"]), reverse=True)
    rows.sort(key=lambda row: len(row["matchedIssueIds"]), reverse=True)
    return rows[:CUSTOMER_DETAIL_PLAYBOOK_LIMIT]


def build_customer_detail(
    customer: str,
    *,
    days: int = 30,
    today: Optional[date] = None,
    refresh: bool = False,
    live: bool = False,
    playbook_loader: Callable[..., List[Dict[str, Any]]] = list_playbooks,
) -> Dict[str, Any]:
    """팀 대시보드와 같은 명단·상태·건강도 기준으로 고객사 한 곳을 집계한다."""
    if days not in DASHBOARD_PERIOD_OPTIONS:
        raise ValueError("조회 기간은 7일, 14일, 30일, 90일 중 하나여야 합니다.")
    name = str(customer or "").strip()
    if not name or len(name) > CUSTOMER_NAME_MAX_LENGTH:
        raise ValueError("고객사 이름이 올바르지 않습니다.")
    base = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    current_start, current_end, previous_start, previous_end = _period_bounds(base, days)
    issues, source = _load_issues(prefer_snapshot=not (refresh or live), force_refresh=refresh)
    complete = source.get("search_complete") is True
    team_issues = filter_team_issues(issues)
    customer_issues = [issue for issue in team_issues if str(issue.get("project") or "").strip() == name]
    if not customer_issues:
        raise LookupError("팀 담당 이슈 중 해당 고객사를 찾지 못했습니다.")

    health_rows = _customer_health(customer_issues, base, current_start, previous_start, previous_end)
    health = health_rows[0] if health_rows else {}
    if not complete:
        # 대시보드와 같이 일부 표본으로는 추세·건강도를 확정하지 않는다.
        health = {**health, "level": "unknown", "label": "판단 보류", "trend": "unknown",
                  "reasons": ["일부 데이터만 확인해 고객사 전체 추세와 건강도는 미확정입니다."]}

    open_rows = [row for row in _summary_rows(customer_issues, base) if _is_open(str(row.get("status") or ""))]
    open_items = [_priority_content(row, base) for row in open_rows]
    open_items.sort(key=lambda row: (-int(row.get("ageDays") or 0), str(row.get("id"))))
    recent = [issue for issue in customer_issues if _inside(issue, current_start, current_end)]
    previous = [issue for issue in customer_issues if _inside(issue, previous_start, previous_end)]
    issue_ids = {str(issue.get("id")) for issue in customer_issues if issue.get("id") is not None}

    summary = {
        "open": len(open_rows),
        "overdue": sum(bool(row.get("overdue")) for row in open_rows),
        "aging": sum(int(row.get("ageDays") or 0) >= TEAM_BRIEF_AGING_DAYS for row in open_rows),
        "newIssues": len(recent),
        "previousNewIssues": len(previous) if complete else None,
        "carriedOpen": sum((_parse_issue_date(row.get("createdAt")) or base) < current_start for row in open_rows),
        "averageHandlingDays": _average(_handling_days(customer_issues)),
        "teamAverageHandlingDays": _average(_handling_days(team_issues)) if complete else None,
        "totalIssues": len(customer_issues),
    }
    return {
        "meta": {
            "source": source.get("status") or "error",
            "sourceReason": source.get("reason") or "",
            "generatedAt": _now(),
            "fetchedAt": source.get("fetchedAt"),
            "cacheUntil": source.get("cacheUntil"),
            "search_complete": source.get("search_complete", False),
            "refreshPending": source.get("refreshPending", False),
            "periodDays": days,
            "periodFrom": current_start.isoformat(),
            "periodTo": current_end.isoformat(),
            "comparisonFrom": previous_start.isoformat(),
            "comparisonTo": previous_end.isoformat(),
            "scopeLabel": TEAM_SCOPE_LABEL,
        },
        "customer": {"id": name, "name": customer_name(name)},
        "health": {
            "level": health.get("level") or "stable",
            "label": health.get("label") or "안정",
            "trend": health.get("trend") or "steady",
            "reasons": list(health.get("reasons") or []),
        },
        "summary": summary,
        "monthly": _monthly_flow(customer_issues, base),
        "screens": _ranked_counts(recent, "screen_number")[:CUSTOMER_DETAIL_SCREEN_LIMIT],
        "media": _ranked_counts(recent, "media_type", "미지정"),
        "openIssues": open_items[:CUSTOMER_DETAIL_OPEN_LIMIT],
        "openIssuesTotal": len(open_items),
        "playbooks": _related_playbooks(issue_ids, playbook_loader),
    }
