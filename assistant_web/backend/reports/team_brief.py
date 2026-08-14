"""매주 기준본으로 보존되는 인앱 팀 브리핑."""

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import date, timedelta
from typing import Any, Dict, List, Tuple

from backend.config import CHROMA_DB_PATH
from backend.sources.mantis import fetch_mantis_recent_issues, normalize_issue
from backend.tools.mantis_snapshot import scan_mantis_documents
from backend.common import normalize_chroma_issue_document
from backend.workspace import _issue_summary, _is_open, _now, init_workspace_db, workspace_db_path


TEAM_BRIEF_FETCH_LIMIT = 1000
TEAM_BRIEF_AGING_DAYS = 30
TEAM_BRIEF_SPIKE_MIN = 2
TEAM_BRIEF_SPIKE_RATIO = 2.0


def _week_bounds(today: date) -> Tuple[date, date, date, date, date]:
    current_start = today - timedelta(days=today.weekday())
    previous_end = current_start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=6)
    baseline_end = previous_start - timedelta(days=1)
    baseline_start = baseline_end - timedelta(days=6)
    return current_start, previous_start, previous_end, baseline_start, baseline_end


def _inside(issue: Dict[str, Any], start: date, end: date) -> bool:
    try:
        created = date.fromisoformat(str(issue.get("created_at") or "")[:10])
    except ValueError:
        return False
    return start <= created <= end


def _load_issues() -> Tuple[List[Dict[str, Any]], Dict[str, str]]:
    try:
        raw = fetch_mantis_recent_issues(total_limit=TEAM_BRIEF_FETCH_LIMIT, request_timeout=8)
        return [normalize_issue(issue) for issue in raw], {"status": "live", "reason": ""}
    except Exception as exc:
        snapshot = scan_mantis_documents(
            CHROMA_DB_PATH,
            matches=lambda _text: True,
            normalize=normalize_chroma_issue_document,
            example_limit=TEAM_BRIEF_FETCH_LIMIT,
            scan_limit=max(TEAM_BRIEF_FETCH_LIMIT, 3000),
        )
        status = "snapshot" if snapshot.get("available") else "error"
        return snapshot.get("issues") or [], {
            "status": status,
            "reason": str(snapshot.get("reason") or type(exc).__name__)[:300],
        }


def _spikes(
    recent: List[Dict[str, Any]],
    baseline: List[Dict[str, Any]],
    field: str,
) -> List[Dict[str, Any]]:
    recent_counts = Counter(str(issue.get(field) or "미상") for issue in recent)
    baseline_counts = Counter(str(issue.get(field) or "미상") for issue in baseline)
    rows: List[Dict[str, Any]] = []
    for name, count in recent_counts.items():
        if name in {"", "미상", "기타/미상"}:
            continue
        previous = baseline_counts.get(name, 0)
        ratio = count / previous if previous else None
        if count < TEAM_BRIEF_SPIKE_MIN or (previous and ratio is not None and ratio < TEAM_BRIEF_SPIKE_RATIO):
            continue
        rows.append({
            "name": name,
            "count": count,
            "previousCount": previous,
            "ratio": round(ratio, 1) if ratio is not None else None,
        })
    return sorted(rows, key=lambda row: (-row["count"], row["name"]))[:5]


def _generate_team_brief(today: date) -> Dict[str, Any]:
    current_start, previous_start, previous_end, baseline_start, baseline_end = _week_bounds(today)
    issues, source = _load_issues()
    previous = [issue for issue in issues if _inside(issue, previous_start, previous_end)]
    baseline = [issue for issue in issues if _inside(issue, baseline_start, baseline_end)]
    open_issues = [issue for issue in issues if _is_open(str(issue.get("status") or ""))]
    summaries = [_issue_summary(issue) for issue in open_issues]
    carried = [item for item in summaries if str(item.get("createdAt") or "") < current_start.isoformat()]
    overdue = [item for item in summaries if item.get("overdue")]
    aging = [item for item in summaries if int(item.get("ageDays") or 0) >= TEAM_BRIEF_AGING_DAYS]
    unassigned = [
        item for item in summaries
        if str(item.get("handler") or "").strip() in {"", "담당자 미지정", "미지정", "미상"}
    ]
    priorities: Dict[Any, Dict[str, Any]] = {}
    for item in overdue + aging + unassigned:
        priorities[item.get("id")] = item

    new_count = len(previous)
    previous_count = len(baseline)
    change_rate = None if previous_count == 0 else round((new_count - previous_count) / previous_count * 100, 1)
    return {
        "weekStart": current_start.isoformat(),
        "title": f"{current_start.month}월 {current_start.day}일 주간 팀 브리핑",
        "summaryPeriod": f"{previous_start.isoformat()} ~ {previous_end.isoformat()}",
        "comparisonPeriod": f"{baseline_start.isoformat()} ~ {baseline_end.isoformat()}",
        "summary": {
            "newIssues": new_count,
            "previousNewIssues": previous_count,
            "changeRate": change_rate,
            "closedNow": sum(not _is_open(str(issue.get("status") or "")) for issue in previous),
            "carriedOpen": len(carried),
            "aging": len(aging),
            "overdue": len(overdue),
            "unassigned": len(unassigned),
        },
        "customerSpikes": _spikes(previous, baseline, "project"),
        "screenSpikes": _spikes(previous, baseline, "screen_number"),
        "priorities": list(priorities.values())[:10],
        "source": source,
        "generatedAt": _now(),
    }


def get_weekly_team_brief(user_uid: str, *, refresh: bool = False, today: date | None = None) -> Dict[str, Any]:
    """이번 주 기준본을 반환하고 사용자별 확인 여부를 덧붙인다."""
    base = today or date.today()
    week_start = _week_bounds(base)[0].isoformat()
    init_workspace_db()
    with closing(sqlite3.connect(workspace_db_path())) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = None if refresh else connection.execute(
            "SELECT payload_json FROM weekly_team_briefs WHERE week_start = ?", (week_start,)
        ).fetchone()
    if row:
        payload = json.loads(row["payload_json"])
    else:
        generated = _generate_team_brief(base)
        serialized = json.dumps(generated, ensure_ascii=False)
        with closing(sqlite3.connect(workspace_db_path())) as connection, connection:
            if refresh:
                connection.execute(
                    """INSERT INTO weekly_team_briefs (week_start, payload_json, generated_at)
                       VALUES (?, ?, ?) ON CONFLICT(week_start) DO UPDATE SET
                       payload_json = excluded.payload_json, generated_at = excluded.generated_at""",
                    (week_start, serialized, generated["generatedAt"]),
                )
            else:
                connection.execute(
                    "INSERT OR IGNORE INTO weekly_team_briefs (week_start, payload_json, generated_at) VALUES (?, ?, ?)",
                    (week_start, serialized, generated["generatedAt"]),
                )
            stored = connection.execute(
                "SELECT payload_json FROM weekly_team_briefs WHERE week_start = ?", (week_start,)
            ).fetchone()
        payload = json.loads(stored[0]) if stored else generated
    with closing(sqlite3.connect(workspace_db_path())) as connection:
        viewed = connection.execute(
            "SELECT 1 FROM weekly_team_brief_reads WHERE user_uid = ? AND week_start = ?",
            (user_uid, week_start),
        ).fetchone() is not None
    return {**payload, "viewed": viewed}


def mark_weekly_team_brief_read(user_uid: str, week_start: str) -> None:
    init_workspace_db()
    with closing(sqlite3.connect(workspace_db_path())) as connection, connection:
        connection.execute(
            """INSERT INTO weekly_team_brief_reads (user_uid, week_start, read_at)
               VALUES (?, ?, ?) ON CONFLICT(user_uid, week_start) DO UPDATE SET read_at = excluded.read_at""",
            (user_uid, week_start, _now()),
        )
