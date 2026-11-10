"""같은 원천·범위의 분석 화면 집계. 수정일로 완료 이벤트를 만들지 않는다."""
from collections import defaultdict
from datetime import date, timedelta
import math
from typing import Any

from backend.chart_data import daily_registrations
from backend.effort_fields import issue_delivery_date, issue_effort_fields, parse_effort_field_value
from backend.mantis_status import canonical_mantis_status, mantis_status_bucket
from backend.storage.workspace_rows import _due_date


STAGES = ("review", "progress", "validation", "done", "unknown")


def _day(value):
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _stage(issue):
    status = canonical_mantis_status(issue.get("status", ""))
    if status in {"new", "feedback", "acknowledged", "confirmed"}:
        return "review"
    if status == "assigned":
        return "progress"
    if status == "developed":
        return "validation"
    return "done" if status in {"resolved", "closed"} else "unknown"


def _series(counts, start, end):
    return [{"date": (day := start + timedelta(days=i)).isoformat(), "count": round(counts.get(day, 0), 2)}
            for i in range((end - start).days + 1)]


def _rate(current, previous, complete):
    return round((current - previous) / previous * 100, 1) if complete and previous else None


def _aggregate(issues, today, days, complete):
    start = today - timedelta(days=days - 1)
    previous_end = start - timedelta(days=1)
    previous_start = start - timedelta(days=days)
    current, previous, opened = [], [], []
    deliveries: defaultdict[date, int] = defaultdict(int)
    efforts: defaultdict[date, float] = defaultdict(float)
    states = dict.fromkeys(STAGES, 0)
    weeks = {}
    week = start - timedelta(days=start.weekday())
    while week <= today:
        weeks[week] = {"weekStart": week.isoformat(), "from": max(start, week).isoformat(),
                       "to": min(today, week + timedelta(days=6)).isoformat(),
                       "total": 0, "states": dict.fromkeys(STAGES, 0)}
        week += timedelta(days=7)
    missing_created = 0
    for issue in issues:
        created = _day(issue.get("created_at"))
        stage = _stage(issue)
        states[stage] += 1
        if mantis_status_bucket(issue.get("status", "")) == "open":
            opened.append(issue)
        if created is None:
            missing_created += 1
        elif start <= created <= today:
            current.append(issue)
            row = weeks[created - timedelta(days=created.weekday())]
            row["total"] += 1
            row["states"][stage] += 1
        elif previous_start <= created <= previous_end:
            previous.append(issue)
        delivered = _day(issue_delivery_date(issue))
        if delivered and previous_start <= delivered <= today:
            deliveries[delivered] += 1
            amounts = [amount for value in issue_effort_fields(issue).values()
                       for _, amount in parse_effort_field_value(value) if math.isfinite(amount)]
            efforts[delivered] += sum(amounts)
    summary = {
        "open": len(opened),
        "overdue": sum(bool((due := _day(_due_date(row))) and due < today) for row in opened),
        "aging": sum(bool((created := _day(row.get("created_at"))) and (today - created).days >= 30) for row in opened),
        "carriedOpen": sum(bool((created := _day(row.get("created_at"))) and created < start) for row in opened),
        "newIssues": len(current), "previousNewIssues": len(previous),
        "changeRate": _rate(len(current), len(previous), complete),
        "closedNow": sum(mantis_status_bucket(row.get("status", "")) == "done" for row in current),
        "newOpen": sum(mantis_status_bucket(row.get("status", "")) == "open" for row in current),
        "unknownStatus": sum(mantis_status_bucket(row.get("status", "")) == "unknown" for row in current),
    }
    comparisons = {}
    for axis, field, unknown in (("customer", "project", "고객사 미상"), ("screen", "screen_number", "화면번호 미상"),
                                 ("member", "handler", "담당자 미지정")):
        grouped: dict[str, Any] = {}
        for key, rows in (("current", current), ("previous", previous)):
            for issue in rows:
                name = str(issue.get(field) or unknown)
                group = grouped.setdefault(name, {"name": name, "current": 0, "previous": 0})
                group[key] += 1
        comparisons[axis] = [{**row, "delta": row["current"] - row["previous"],
                              "changeRate": _rate(row["current"], row["previous"], complete)}
                             for row in sorted(grouped.values(), key=lambda row: (-row["current"], row["name"]))]
    delivery_points, effort_points = _series(deliveries, start, today), _series(efforts, start, today)
    previous_delivery = _series(deliveries, previous_start, previous_end)
    return {
        "summary": summary, "intake": daily_registrations(issues, start, today),
        "previousIntake": daily_registrations(issues, previous_start, previous_end),
        "delivery": {"dateBasis": "고객사전달일", "points": delivery_points,
                     "count": sum(row["count"] for row in delivery_points),
                     "previousCount": sum(row["count"] for row in previous_delivery),
                     "previousPoints": previous_delivery},
        "effort": {"dateBasis": "고객사전달일", "points": effort_points,
                   "total": round(sum(row["count"] for row in effort_points), 2),
                   "previousTotal": round(sum(row["count"] for row in _series(efforts, previous_start, previous_end)), 2)},
        "comparisons": comparisons, "states": states, "cohorts": list(weeks.values()),
        "missingCreated": missing_created, "historyAvailable": False,
    }


def build_dashboard_analytics(issues, *, today: date, days: int, complete: bool):
    """고객사 선택을 네트워크 재조회 없이 모든 차트에 일관되게 적용할 집계."""
    groups = defaultdict(list)
    for issue in issues:
        groups[str(issue.get("project") or "고객사 미상")].append(issue)
    return {**_aggregate(issues, today, days, complete),
            "scopes": {name: _aggregate(rows, today, days, complete) for name, rows in sorted(groups.items())}}
