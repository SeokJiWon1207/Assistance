"""팀 대시보드 › 고객사 탭. 선택 목록(picker_names)의 고객사마다 미종료·최근 8주 신규를 집계한다."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from backend.domain.team import TEAM_ISSUE_SCOPE_LABEL, filter_team_issues
from backend.reports.dashboard import _summary_rows
from backend.reports.screen_hotspot import SCREEN_HOTSPOT_WEEKS, _spike, _week_index, _week_starts
from backend.reports.team_brief import TEAM_BRIEF_AGING_DAYS, _load_issues, _parse_issue_date
from backend.settings.catalogs import get_customer_picker
from backend.team_templates import customer_option_for_project
from backend.workspace import _now


def _empty_row(name: str) -> Dict[str, Any]:
    return {"name": name, "project": "", "open": 0, "overdue": 0, "aging": 0, "recent": 0,
            "lastCreatedAt": "", "weekly": [0] * SCREEN_HOTSPOT_WEEKS, "spike": None}


def build_customer_overview(*, today: Optional[date] = None, refresh: bool = False, live: bool = False) -> Dict[str, Any]:
    """미종료는 현재 상태, 주별 신규는 등록일 기준이다. 종료일이 없으므로 처리 건수는 만들지 않는다."""
    base = today or datetime.now(ZoneInfo("Asia/Seoul")).date()
    starts = _week_starts(base)
    issues, source = _load_issues(prefer_snapshot=not (refresh or live), force_refresh=refresh)
    complete = source.get("search_complete") is True
    names = list(get_customer_picker())
    rows = {name: _empty_row(name) for name in names}
    projects: Dict[str, Counter[str]] = defaultdict(Counter)
    option_by_project: Dict[str, Optional[str]] = {}
    for item in _summary_rows(filter_team_issues(issues), base):
        project = str(item.get("project") or "").strip()
        if project not in option_by_project:
            option_by_project[project] = customer_option_for_project(project) if project else None
        name = option_by_project[project]
        if name not in rows:
            continue
        row = rows[name]
        projects[name][project] += 1
        if item.get("isOpen"):
            row["open"] += 1
            row["overdue"] += bool(item.get("overdue"))
            row["aging"] += int(item.get("ageDays") or 0) >= TEAM_BRIEF_AGING_DAYS
        created = _parse_issue_date(item.get("createdAt"))
        if created and created <= base and created.isoformat() > row["lastCreatedAt"]:
            row["lastCreatedAt"] = created.isoformat()
        week = _week_index(created, starts, base)
        if week is not None:
            row["weekly"][week] += 1
            row["recent"] += 1
    for name, row in rows.items():
        # 고객사 상세는 프로젝트 하나 단위다. 이슈가 가장 많은 프로젝트로 연다.
        row["project"] = projects[name].most_common(1)[0][0] if projects[name] else ""
        # 일부만 확인한 결과로는 급증을 판단하지 않는다(화면번호 탭과 같은 기준).
        spike = _spike(name, row["weekly"]) if complete else None
        row["spike"] = {key: spike[key] for key in ("count", "baselineAverage", "ratio")} if spike else None
    ordered = [rows[name] for name in names]
    return {
        "meta": {
            "source": source.get("status") or "error",
            "sourceReason": source.get("reason") or "",
            "generatedAt": _now(),
            "fetchedAt": source.get("fetchedAt"),
            "cacheUntil": source.get("cacheUntil"),
            "search_complete": source.get("search_complete", False),
            "refreshPending": source.get("refreshPending", False),
            "scopeLabel": TEAM_ISSUE_SCOPE_LABEL,
            "dateBasis": "등록일",
        },
        "weeks": [
            {"start": start.isoformat(), "label": f"{start.month}/{start.day}", "partial": index == len(starts) - 1}
            for index, start in enumerate(starts)
        ],
        "customers": ordered,
        "summary": {
            "customers": len(ordered),
            "withOpen": sum(row["open"] > 0 for row in ordered),
            "spiking": sum(row["spike"] is not None for row in ordered),
            "withOverdue": sum(row["overdue"] > 0 for row in ordered),
        },
    }
