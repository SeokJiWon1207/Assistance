"""고객사·월·플랫폼 기준 월간 운영 리포트."""

import calendar
import re
from collections import Counter
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from backend.reports.effort import parse_effort_field_value
from backend.sources.mantis import (
    build_sources_from_issue_rows,
    fetch_mantis_recent_issues,
    issue_matches_constraints,
    normalize_issue,
)
from backend.understanding import extract_query_constraints


MONTHLY_CUSTOMER_PATTERN = re.compile(r"월간\s*고객사\s*리포트|고객사\s*월간\s*리포트", re.IGNORECASE)
MONTHLY_CUSTOMER_FETCH_LIMIT = 1000
MONTHLY_CUSTOMER_AGING_DAYS = 30
MONTHLY_CUSTOMER_TOP_N = 5


def _is_closed(status: Any) -> bool:
    label = str(status or "")
    return "종료" in label or "closed" in label.casefold()


def _parse_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _month_bounds(constraints: Dict[str, Any]) -> Optional[Tuple[date, date]]:
    created_at = constraints.get("created_at") or {}
    start = _parse_date(created_at.get("from"))
    end = _parse_date(created_at.get("to"))
    if not start or not end or start.year != end.year or start.month != end.month:
        return None
    if start.day != 1 or end.day != calendar.monthrange(start.year, start.month)[1]:
        return None
    return start, end


def _previous_month(start: date) -> Tuple[date, date]:
    end = start - timedelta(days=1)
    return end.replace(day=1), end


def _customer_label(constraints: Dict[str, Any], issues: List[Dict[str, Any]]) -> str:
    for key in ("customer_exact", "customer_key"):
        value = str(constraints.get(key) or "").strip()
        if value:
            return value
    return Counter(str(issue.get("project") or "미상") for issue in issues).most_common(1)[0][0]


def _effort_total(issue: Dict[str, Any]) -> float:
    return round(sum(
        amount
        for value in (issue.get("effort_fields") or {}).values()
        for _person, amount in parse_effort_field_value(value)
    ), 4)


def _failure(text: str, constraints: Dict[str, Any], message: str) -> Dict[str, Any]:
    return {
        "content": message,
        "sources": [],
        "retrieval": {
            "query": text,
            "constraints": constraints,
            "stages": [{"source": "mantis_monthly_customer", "status": "error", "reason": message}],
        },
        "raw": {"mode": "deterministic_monthly_customer_report"},
    }


def build_monthly_customer_report(text: str) -> Optional[Dict[str, Any]]:
    """월간 고객사 요청을 전월 비교와 운영 지표를 포함한 보고서로 만든다."""
    if not MONTHLY_CUSTOMER_PATTERN.search(str(text or "")):
        return None
    constraints = extract_query_constraints(text)
    if not (constraints.get("customer_aliases") or constraints.get("customer_exact")):
        return _failure(text, constraints, "월간 리포트를 만들 고객사명을 입력해 주세요.")
    bounds = _month_bounds(constraints)
    if not bounds:
        return _failure(text, constraints, "보고할 월을 '2026년 8월' 또는 '지난달'처럼 입력해 주세요.")
    start, end = bounds
    previous_start, previous_end = _previous_month(start)
    today = date.today()
    effective_end = min(end, today) if start <= today <= end else end
    if effective_end < end:
        previous_end = previous_end.replace(
            day=min(effective_end.day, calendar.monthrange(previous_end.year, previous_end.month)[1])
        )

    try:
        raw_issues = fetch_mantis_recent_issues(total_limit=MONTHLY_CUSTOMER_FETCH_LIMIT)
    except Exception as exc:
        return _failure(text, constraints, f"Mantis 월간 고객사 조회에 실패했습니다: {exc}")

    scope_constraints = {
        key: value for key, value in constraints.items()
        if key not in {"created_at", "filter_clauses"}
    }
    scoped = [
        normalize_issue(issue)
        for issue in raw_issues
        if issue_matches_constraints(issue, scope_constraints)
    ]
    current = [
        issue for issue in scoped
        if start <= (_parse_date(issue.get("created_at")) or date.min) <= effective_end
    ]
    previous = [
        issue for issue in scoped
        if previous_start <= (_parse_date(issue.get("created_at")) or date.min) <= previous_end
    ]
    customer = _customer_label(constraints, current or scoped)
    closed = [issue for issue in current if _is_closed(issue.get("status"))]
    open_rows = [issue for issue in current if not _is_closed(issue.get("status"))]

    handling_days = []
    for issue in closed:
        created = _parse_date(issue.get("created_at"))
        updated = _parse_date(issue.get("updated_at"))
        if created and updated and updated >= created:
            handling_days.append((updated - created).days)

    aging = []
    for issue in scoped:
        created = _parse_date(issue.get("created_at"))
        if created and created <= effective_end and not _is_closed(issue.get("status")):
            age_days = (today - created).days
            if age_days >= MONTHLY_CUSTOMER_AGING_DAYS:
                aging.append({**issue, "age_days": age_days})
    aging.sort(key=lambda issue: issue["age_days"], reverse=True)

    current_with_effort = [{**issue, "effort_total": _effort_total(issue)} for issue in current]
    total_effort = sum(issue["effort_total"] for issue in current_with_effort)
    screen_counts = Counter(str(issue.get("screen_number") or "미상") for issue in current)
    platform_counts = Counter(str(issue.get("media_type") or "기타/미상") for issue in current)
    trend = Counter(
        str(issue.get("created_at") or "")[:7]
        for issue in scoped
        if (_parse_date(issue.get("created_at")) or date.max) <= effective_end
    )
    trend_months = sorted(trend)[-6:]

    delta = len(current) - len(previous)
    rate = None if not previous else delta / len(previous) * 100
    comparison = f"{delta:+d}건 ({rate:+.1f}%)" if rate is not None else f"{delta:+d}건 (전월 0건)"
    lines = [
        f"{start.year}년 {start.month}월 {customer} 월간 고객사 리포트",
        f"기준: 등록일 {start.isoformat()}~{effective_end.isoformat()} · 최근 라이브 {len(raw_issues)}건 범위",
        "",
        "요약",
        f"- 등록 이슈: **{len(current)}건** · 전월{' 동기' if effective_end < end else ''} {len(previous)}건 대비 **{comparison}**",
        f"- 현재 상태: 미종료 **{len(open_rows)}건** · 종료 **{len(closed)}건**",
        f"- 총 공수: **{total_effort:.2f}**",
        f"- 평균 처리기간: **{sum(handling_days) / len(handling_days):.1f}일** (종료 {len(handling_days)}건 기준)"
        if handling_days else "- 평균 처리기간: 계산 가능한 종료 이슈 없음",
        f"- 현재 장기 미종료: **{len(aging)}건** (보고 종료일까지 등록된 이슈 중 {MONTHLY_CUSTOMER_AGING_DAYS}일 이상)",
        "",
        "최근 월별 추이",
    ]
    lines.extend(f"- {month}: {trend[month]}건" for month in trend_months)
    if not trend_months:
        lines.append("- 추이를 계산할 등록일 데이터가 없습니다.")

    lines.extend(["", "플랫폼·화면번호"])
    lines.append("- 플랫폼: " + (" · ".join(f"{name} {count}건" for name, count in platform_counts.most_common()) or "데이터 없음"))
    lines.append("- 빈발 화면: " + (" · ".join(f"{name} {count}건" for name, count in screen_counts.most_common(5)) or "데이터 없음"))

    ranked = sorted(
        current_with_effort,
        key=lambda issue: (issue["effort_total"], int(issue.get("id") or 0)),
        reverse=True,
    )
    lines.extend(["", "주요 이슈"])
    if ranked:
        lines.extend(
            f"- #{issue['id']} {issue['summary']} — {issue['status']} · 공수 {issue['effort_total']:.2f}"
            for issue in ranked[:MONTHLY_CUSTOMER_TOP_N]
        )
    else:
        lines.append("- 해당 월에 등록된 이슈가 없습니다.")

    lines.extend(["", "다음 확인 항목"])
    if aging:
        lines.extend(
            f"- #{issue['id']} {issue['summary']} — 현재 {issue['age_days']}일 경과 · {issue['handler']}"
            for issue in aging[:MONTHLY_CUSTOMER_TOP_N]
        )
    else:
        lines.append("- 현재 장기 미종료 이슈가 없습니다.")
    lines.extend([
        "",
        "※ 상태는 현재 Mantis 상태이며 과거 월말 상태를 복원한 값이 아닙니다.",
        "※ 평균 처리기간은 정확한 종료 시각 대신 등록일→최종수정일을 사용한 근사값입니다.",
    ])

    source_rows = ranked[:MONTHLY_CUSTOMER_TOP_N]
    known_ids = {issue.get("id") for issue in source_rows}
    source_rows.extend(issue for issue in aging[:MONTHLY_CUSTOMER_TOP_N] if issue.get("id") not in known_ids)
    sources = build_sources_from_issue_rows(source_rows[:10])
    return {
        "content": "\n".join(lines),
        "sources": sources,
        "sourcesForFollowUps": sources,
        "retrieval": {
            "query": text,
            "constraints": constraints,
            "stages": [{
                "source": "mantis_monthly_customer",
                "status": "ok",
                "issues_list": source_rows[:10],
                "meta": {
                    "current_count": len(current),
                    "previous_count": len(previous),
                    "open_count": len(open_rows),
                    "closed_count": len(closed),
                    "aging_count": len(aging),
                    "total_effort": round(total_effort, 2),
                },
            }],
        },
        "raw": {"mode": "deterministic_monthly_customer_report"},
    }
