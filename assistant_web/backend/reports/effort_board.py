"""팀 대시보드 › 공수·처리시간 탭의 집계."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import median
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from backend.display import customer_name
from backend.domain.team import TEAM_SCOPE_LABEL, filter_team_issues
from backend.effort_fields import EFFORT_FIELD_LABELS, issue_effort_fields, parse_effort_field_value
from backend.reports.customer import _is_closed
from backend.reports.dashboard import DASHBOARD_PERIOD_OPTIONS, _period_bounds, _summary_rows
from backend.reports.team_brief import TEAM_BRIEF_AGING_DAYS, _load_issues, _parse_issue_date
from backend.sources.mantis_fields import mantis_issue_effort_date
from backend.workspace import _is_open, _now


EFFORT_BOARD_WEEKS = 8
EFFORT_BOARD_CUSTOMER_LIMIT = 6
EFFORT_BOARD_CUSTOMER_MIN_CLOSED = 3
EFFORT_BOARD_AGING_LIMIT = 10
# 앞으로 7일 안에 장기 미종료 기준을 넘길 이슈도 미리 보여 준다.
EFFORT_BOARD_AGING_SOON_DAYS = 7
HANDLING_BUCKETS: Tuple[Tuple[str, str, int, float], ...] = (
    ("same-day", "당일", 0, 0),
    ("1-2", "1–2일", 1, 2),
    ("3-7", "3–7일", 3, 7),
    ("8-14", "8–14일", 8, 14),
    ("15-30", "15–30일", 15, 30),
    ("31+", "30일 초과", 31, math.inf),
)


def _handling(issue: Dict[str, Any]) -> Optional[Tuple[date, int]]:
    """해결·종료 이슈의 종료일(마지막 수정일)과 처리일을 돌려준다."""
    if not _is_closed(issue.get("status")):
        return None
    created = _parse_issue_date(issue.get("created_at"))
    closed = _parse_issue_date(issue.get("updated_at"))
    if not created or not closed or closed < created:
        return None
    return closed, (closed - created).days


def _closed_between(issues: Iterable[Dict[str, Any]], start: date, end: date) -> List[Tuple[Dict[str, Any], int]]:
    rows: List[Tuple[Dict[str, Any], int]] = []
    for issue in issues:
        handled = _handling(issue)
        if handled and start <= handled[0] <= end:
            rows.append((issue, handled[1]))
    return rows


def _percentile(values: List[int], ratio: float) -> Optional[int]:
    """nearest-rank 백분위수. 값이 없으면 None."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(ratio * len(ordered)) - 1)]


def _median(values: List[int]) -> Optional[float]:
    return round(float(median(values)), 1) if values else None


def _distribution(days: List[int]) -> List[Dict[str, Any]]:
    return [
        {"key": key, "label": label, "count": sum(low <= value <= high for value in days)}
        for key, label, low, high in HANDLING_BUCKETS
    ]


def _weekly_medians(issues: List[Dict[str, Any]], today: date) -> List[Dict[str, Any]]:
    this_week = today - timedelta(days=today.weekday())
    rows: List[Dict[str, Any]] = []
    for offset in range(EFFORT_BOARD_WEEKS - 1, -1, -1):
        start = this_week - timedelta(weeks=offset)
        end = min(start + timedelta(days=6), today)
        days = [value for _, value in _closed_between(issues, start, end)]
        rows.append({
            "weekStart": start.isoformat(),
            "label": f"{start.month}/{start.day}",
            "closedCount": len(days),
            "medianDays": _median(days),
            "partialWeek": end < start + timedelta(days=6),
        })
    return rows


def _customer_rows(closed: List[Tuple[Dict[str, Any], int]]) -> List[Dict[str, Any]]:
    grouped: Dict[str, List[int]] = defaultdict(list)
    for issue, days in closed:
        customer = str(issue.get("project") or "").strip()
        if customer and customer not in {"미상", "기타/미상"}:
            grouped[customer].append(days)
    ranked = sorted(
        ((customer, days) for customer, days in grouped.items() if len(days) >= EFFORT_BOARD_CUSTOMER_MIN_CLOSED),
        key=lambda item: (-float(median(item[1])), -len(item[1]), item[0]),
    )
    return [
        {"customer": customer, "name": customer_name(customer), "closedCount": len(days), "medianDays": _median(days)}
        for customer, days in ranked[:EFFORT_BOARD_CUSTOMER_LIMIT]
    ]


def _effort_rows(issues: Iterable[Dict[str, Any]], start: date, end: date) -> Dict[str, Any]:
    """고객사전달일이 기간 안인 이슈의 공수를 유형별로 합산한다."""
    totals: Dict[str, float] = {field: 0.0 for field in EFFORT_FIELD_LABELS}
    issue_ids: Dict[str, set] = {field: set() for field in EFFORT_FIELD_LABELS}
    counted = set()
    for issue in issues:
        delivered = _parse_issue_date(mantis_issue_effort_date(issue))
        if not delivered or not start <= delivered <= end:
            continue
        for field, value in issue_effort_fields(issue).items():
            amounts = [amount for _, amount in parse_effort_field_value(value)]
            if not amounts:
                continue
            totals[field] += sum(amounts)
            issue_ids[field].add(issue.get("id"))
            counted.add(issue.get("id"))
    types = [
        {
            "field": field,
            "label": label,
            "total": round(totals[field], 2),
            "issueCount": len(issue_ids[field]),
            "averagePerIssue": round(totals[field] / len(issue_ids[field]), 2) if issue_ids[field] else None,
        }
        for field, label in EFFORT_FIELD_LABELS.items()
    ]
    return {"total": round(sum(totals.values()), 2), "issueCount": len(counted), "types": types}


def build_effort_board(
    *,
    days: int = 30,
    today: Optional[date] = None,
    refresh: bool = False,
    live: bool = False,
) -> Dict[str, Any]:
    """팀 명단 담당 이슈의 처리일 분포·적체와 기록된 공수를 같은 기간 기준으로 계산한다."""
    if days not in DASHBOARD_PERIOD_OPTIONS:
        raise ValueError("조회 기간은 7일, 14일, 30일, 90일 중 하나여야 합니다.")
    base = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    current_start, current_end, previous_start, previous_end = _period_bounds(base, days)
    issues, source = _load_issues(prefer_snapshot=not (refresh or live), force_refresh=refresh)
    complete = source.get("search_complete") is True
    team_issues = filter_team_issues(issues)

    closed = _closed_between(team_issues, current_start, current_end)
    previous_closed = _closed_between(team_issues, previous_start, previous_end)
    handling_days = [value for _, value in closed]
    open_rows = [row for row in _summary_rows(team_issues, base) if _is_open(str(row.get("status") or ""))]
    soon_from = TEAM_BRIEF_AGING_DAYS - EFFORT_BOARD_AGING_SOON_DAYS
    watch_rows = sorted(
        (row for row in open_rows if int(row.get("ageDays") or 0) >= soon_from),
        key=lambda row: (-int(row.get("ageDays") or 0), str(row.get("id"))),
    )
    aging_count = sum(int(row.get("ageDays") or 0) >= TEAM_BRIEF_AGING_DAYS for row in open_rows)
    previous_median = _median([value for _, value in previous_closed])

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
            "agingDays": TEAM_BRIEF_AGING_DAYS,
        },
        "summary": {
            "closedCount": len(closed),
            "previousClosedCount": len(previous_closed) if complete else None,
            "medianHandlingDays": _median(handling_days),
            "previousMedianHandlingDays": previous_median if complete else None,
            "p90HandlingDays": _percentile(handling_days, 0.9),
            "agingOpen": aging_count,
            "agingSoon": len(watch_rows) - aging_count,
        },
        "distribution": _distribution(handling_days),
        "weekly": _weekly_medians(team_issues, base),
        "customers": _customer_rows(closed),
        "effort": _effort_rows(team_issues, current_start, current_end),
        "agingIssues": [
            {
                "id": row.get("id"),
                "title": row.get("title"),
                "project": row.get("project"),
                "customerName": row.get("customer_name") or customer_name(row.get("project")),
                "handler": row.get("handler"),
                "status": row.get("status"),
                "ageDays": row.get("ageDays"),
                "aging": int(row.get("ageDays") or 0) >= TEAM_BRIEF_AGING_DAYS,
            }
            for row in watch_rows[:EFFORT_BOARD_AGING_LIMIT]
        ],
        "agingIssuesTotal": len(watch_rows),
    }
