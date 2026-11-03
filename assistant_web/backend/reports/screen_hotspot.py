"""팀 대시보드 › 화면번호 탭. 화면번호별 주간 신규 이슈와 반복 화면을 집계한다."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from backend.display import customer_name
from backend.domain.team import TEAM_SCOPE_LABEL, filter_team_issues
from backend.playbooks import list_playbooks
from backend.reports.customer_detail import _related_playbooks
from backend.reports.team_brief import TEAM_BRIEF_SPIKE_MIN, TEAM_BRIEF_SPIKE_RATIO, _load_issues, _parse_issue_date
from backend.workspace import _is_open, _now


SCREEN_HOTSPOT_WEEKS = 8
SCREEN_HOTSPOT_ROW_LIMIT = 10
SCREEN_HOTSPOT_BASELINE_WEEKS = 4
SCREEN_HOTSPOT_RECENT_ISSUES = 8
SCREEN_HOTSPOT_CUSTOMER_OPTIONS = 30
SCREEN_MEDIA_OPTIONS = ("HTS", "MTS")
UNKNOWN_VALUES = {"", "미상", "기타/미상"}


def _week_starts(today: date) -> List[date]:
    this_week = today - timedelta(days=today.weekday())
    return [this_week - timedelta(weeks=offset) for offset in range(SCREEN_HOTSPOT_WEEKS - 1, -1, -1)]


def _week_index(created: Optional[date], starts: List[date], today: date) -> Optional[int]:
    if not created or created < starts[0] or created > today:
        return None
    return min((created - starts[0]).days // 7, len(starts) - 1)


def _screen(issue: Dict[str, Any]) -> str:
    value = str(issue.get("screen_number") or "").strip()
    return "" if value in UNKNOWN_VALUES else value


def _spike(screen: str, counts: List[int]) -> Optional[Dict[str, Any]]:
    """지난주(마지막 완성 주)를 그 앞 4주 평균과 비교한다. 이번 주는 덜 찼으므로 쓰지 않는다."""
    last_week = counts[-2]
    baseline = counts[-2 - SCREEN_HOTSPOT_BASELINE_WEEKS:-2]
    average = sum(baseline) / len(baseline)
    if last_week < TEAM_BRIEF_SPIKE_MIN or (average and last_week / average < TEAM_BRIEF_SPIKE_RATIO):
        return None
    return {
        "screen": screen,
        "count": last_week,
        "baselineAverage": round(average, 1),
        "ratio": round(last_week / average, 1) if average else None,
    }


def _detail(screen: str, issues: List[Dict[str, Any]], playbook_loader) -> Dict[str, Any]:
    customers = Counter(str(issue.get("project") or "").strip() for issue in issues)
    customers.pop("", None)
    recent = sorted(issues, key=lambda issue: (str(issue.get("created_at") or ""), str(issue.get("id"))), reverse=True)
    return {
        "openCount": sum(_is_open(str(issue.get("status") or "")) for issue in issues),
        "customers": [
            {"customer": name, "name": customer_name(name), "count": count}
            for name, count in sorted(customers.items(), key=lambda item: (-item[1], item[0]))
            if name not in UNKNOWN_VALUES
        ],
        "recentIssues": [
            {
                "id": issue.get("id"),
                "title": issue.get("summary") or "제목 없음",
                "customerName": customer_name(issue.get("project")),
                "status": issue.get("status") or "",
                "isOpen": _is_open(str(issue.get("status") or "")),
                "createdAt": str(issue.get("created_at") or "")[:10],
                "handler": issue.get("handler") or "",
            }
            for issue in recent[:SCREEN_HOTSPOT_RECENT_ISSUES]
        ],
        "playbooks": _related_playbooks({str(issue.get("id")) for issue in issues}, playbook_loader),
    }


def build_screen_hotspot(
    *,
    media: str = "",
    customer: str = "",
    today: Optional[date] = None,
    refresh: bool = False,
    live: bool = False,
    playbook_loader=list_playbooks,
) -> Dict[str, Any]:
    """팀 명단 담당 이슈를 등록 주별로 묶어 신규가 많은 화면번호를 보여 준다."""
    media = str(media or "").strip().upper()
    customer = str(customer or "").strip()
    if media and media not in SCREEN_MEDIA_OPTIONS:
        raise ValueError("매체는 HTS 또는 MTS만 선택할 수 있습니다.")
    base = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    starts = _week_starts(base)
    issues, source = _load_issues(prefer_snapshot=not (refresh or live), force_refresh=refresh)
    complete = source.get("search_complete") is True
    team_issues = filter_team_issues(issues)

    in_window: List[tuple[Dict[str, Any], int]] = []
    customer_options: Counter[str] = Counter()
    for issue in team_issues:
        week = _week_index(_parse_issue_date(issue.get("created_at")), starts, base)
        if week is None:
            continue
        if media and str(issue.get("media_type") or "").strip().upper() != media:
            continue
        project = str(issue.get("project") or "").strip()
        if project and project not in UNKNOWN_VALUES:
            customer_options[project] += 1
        if customer and project != customer:
            continue
        in_window.append((issue, week))

    weekly: Dict[str, List[int]] = defaultdict(lambda: [0] * SCREEN_HOTSPOT_WEEKS)
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    unknown_screen = 0
    for issue, week in in_window:
        screen = _screen(issue)
        if not screen:
            unknown_screen += 1
            continue
        weekly[screen][week] += 1
        grouped[screen].append(issue)

    ranked = sorted(weekly, key=lambda screen: (-sum(weekly[screen]), -weekly[screen][-2], screen))
    rows = [
        {
            "screen": screen,
            "weeks": weekly[screen],
            "total": sum(weekly[screen]),
            **_detail(screen, grouped[screen], playbook_loader),
        }
        for screen in ranked[:SCREEN_HOTSPOT_ROW_LIMIT]
    ]
    spikes = [spike for screen in ranked if (spike := _spike(screen, weekly[screen]))] if complete else []
    spikes.sort(key=lambda spike: (-spike["count"], spike["screen"]))
    return {
        "meta": {
            "source": source.get("status") or "error",
            "sourceReason": source.get("reason") or "",
            "generatedAt": _now(),
            "fetchedAt": source.get("fetchedAt"),
            "cacheUntil": source.get("cacheUntil"),
            "search_complete": source.get("search_complete", False),
            "refreshPending": source.get("refreshPending", False),
            "scopeLabel": TEAM_SCOPE_LABEL,
            "media": media,
            "customer": customer,
            "customerName": customer_name(customer) if customer else "",
        },
        "weeks": [
            {"start": start.isoformat(), "label": f"{start.month}/{start.day}", "partial": index == len(starts) - 1}
            for index, start in enumerate(starts)
        ],
        "rows": rows,
        "spikes": spikes[:3],
        "summary": {
            "issueCount": len(in_window),
            "screenCount": len(weekly),
            "unknownScreenCount": unknown_screen,
        },
        "options": {
            "media": list(SCREEN_MEDIA_OPTIONS),
            "customers": [
                {"customer": name, "name": customer_name(name)}
                for name, _ in customer_options.most_common(SCREEN_HOTSPOT_CUSTOMER_OPTIONS)
            ],
        },
    }
