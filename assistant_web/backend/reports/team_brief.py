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
TEAM_BRIEF_CONTENT_VERSION = 2


def _parse_issue_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _priority_content(item: Dict[str, Any], today: date) -> Dict[str, Any]:
    """우선 확인 이슈를 위험 근거와 실행 권고가 있는 콘텐츠로 보강한다."""
    reasons: List[str] = []
    actions: List[str] = []
    score = 0
    age_days = int(item.get("ageDays") or 0)
    handler = str(item.get("handler") or "").strip()
    priority = str(item.get("priority") or "").casefold()
    updated = _parse_issue_date(item.get("updatedAt"))
    stale_days = (today - updated).days if updated and updated <= today else 0

    if item.get("overdue"):
        score += 4
        reasons.append("완료 예정일 경과")
        actions.append("지연 원인과 새로운 완료 예정일 확인")
    if age_days >= 60:
        score += 3
        reasons.append(f"{age_days}일 장기 미종료")
        actions.append("종료 조건과 남은 장애 요인 재검토")
    elif age_days >= TEAM_BRIEF_AGING_DAYS:
        score += 2
        reasons.append(f"{age_days}일 장기 미종료")
        actions.append("장기화 원인과 다음 처리 단계 확인")
    if handler in {"", "담당자 미지정", "미지정", "미상"}:
        score += 2
        reasons.append("담당자 미지정")
        actions.append("담당자 지정 후 최초 대응 계획 등록")
    if any(marker in priority for marker in ("urgent", "immediate", "high", "긴급", "높음")):
        score += 2
        reasons.append("높은 우선순위")
    if stale_days >= 14:
        score += 2
        reasons.append(f"최근 {stale_days}일간 업데이트 없음")
        actions.append("현재 진행 상태와 막힌 점 업데이트")
    elif stale_days >= 7:
        score += 1
        reasons.append(f"최근 {stale_days}일간 업데이트 없음")

    risk_level = "high" if score >= 5 else "attention" if score >= 3 else "watch"
    return {
        **item,
        "riskLevel": risk_level,
        "riskScore": score,
        "riskReasons": reasons or ["우선 확인 대상으로 분류"],
        "recommendedAction": actions[0] if actions else "현재 상태와 다음 처리 단계 확인",
    }


def _is_high_priority(item: Dict[str, Any]) -> bool:
    priority = str(item.get("priority") or "").casefold()
    return any(marker in priority for marker in ("urgent", "immediate", "high", "긴급", "높음"))


def _decision_summary(
    summary: Dict[str, Any],
    customer_spikes: List[Dict[str, Any]],
    screen_spikes: List[Dict[str, Any]],
    priorities: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """숫자 나열을 한 문장 해석과 실행 항목으로 변환한다."""
    change_rate = summary.get("changeRate")
    if change_rate is None:
        headline = f"지난주 신규 이슈 {summary['newIssues']}건이 등록됐습니다."
    elif change_rate > 0:
        headline = f"지난주 신규 이슈가 직전 주보다 {change_rate:g}% 증가했습니다."
    elif change_rate < 0:
        headline = f"지난주 신규 이슈가 직전 주보다 {abs(change_rate):g}% 감소했습니다."
    else:
        headline = "지난주 신규 이슈 규모가 직전 주와 같았습니다."

    signals: List[str] = []
    if customer_spikes:
        item = customer_spikes[0]
        signals.append(f"{item['name']}에서 신규 이슈 {item['count']}건이 집중됐습니다.")
    if screen_spikes:
        item = screen_spikes[0]
        signals.append(f"화면번호 {item['name']} 관련 이슈가 {item['count']}건으로 증가했습니다.")
    if summary.get("overdue"):
        signals.append(f"완료 예정일이 지난 이슈가 {summary['overdue']}건 있습니다.")
    if summary.get("unassigned"):
        signals.append(f"담당자 미지정 이슈가 {summary['unassigned']}건 있습니다.")
    if summary.get("highPriority"):
        signals.append(f"높은 우선순위의 미종료 이슈가 {summary['highPriority']}건 있습니다.")
    if not signals:
        signals.append("특별한 급증이나 일정 위험 신호는 확인되지 않았습니다.")

    actions: List[Dict[str, str]] = []
    if customer_spikes:
        name = customer_spikes[0]["name"]
        actions.append({
            "title": f"{name} 증가 원인 확인",
            "reason": "동일 고객사의 신규 이슈가 단기간에 집중됐습니다.",
            "question": f"지난주 {name} 이슈가 증가한 공통 원인과 영향 범위, 권장 조치를 분석해줘",
        })
    if screen_spikes:
        name = screen_spikes[0]["name"]
        actions.append({
            "title": f"화면 {name} 반복 패턴 점검",
            "reason": "동일 화면번호에서 반복 징후가 확인됐습니다.",
            "question": f"최근 화면번호 {name} 이슈의 공통 증상, 원인과 재발 방지 항목을 정리해줘",
        })
    if priorities:
        high = priorities[0]
        actions.append({
            "title": f"#{high.get('id')} 우선 처리",
            "reason": " · ".join(high.get("riskReasons") or []),
            "question": f"#{high.get('id')} 이슈의 현재 위험, 막힌 점과 다음 행동을 정리해줘",
        })

    overall_risk = "high" if any(item.get("riskLevel") == "high" for item in priorities) else (
        "attention" if priorities or customer_spikes or screen_spikes else "stable"
    )
    return {
        "riskLevel": overall_risk,
        "headline": headline,
        "signals": signals[:3],
        "recommendedActions": actions[:3],
    }


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
    summaries = []
    for issue in open_issues:
        item = _issue_summary(issue)
        created = _parse_issue_date(item.get("createdAt"))
        due = _parse_issue_date(item.get("dueDate"))
        item["ageDays"] = (today - created).days if created and created <= today else None
        item["overdue"] = bool(due and due < today)
        summaries.append(item)
    carried = [item for item in summaries if str(item.get("createdAt") or "") < current_start.isoformat()]
    overdue = [item for item in summaries if item.get("overdue")]
    aging = [item for item in summaries if int(item.get("ageDays") or 0) >= TEAM_BRIEF_AGING_DAYS]
    unassigned = [
        item for item in summaries
        if str(item.get("handler") or "").strip() in {"", "담당자 미지정", "미지정", "미상"}
    ]
    high_priority = [item for item in summaries if _is_high_priority(item)]
    priorities_by_id: Dict[Any, Dict[str, Any]] = {}
    for item in overdue + aging + unassigned + high_priority:
        priorities_by_id[item.get("id")] = item

    new_count = len(previous)
    previous_count = len(baseline)
    change_rate = None if previous_count == 0 else round((new_count - previous_count) / previous_count * 100, 1)
    priorities = sorted(
        (_priority_content(item, today) for item in priorities_by_id.values()),
        key=lambda item: (-int(item.get("riskScore") or 0), -int(item.get("ageDays") or 0)),
    )[:10]
    customer_spikes = _spikes(previous, baseline, "project")
    screen_spikes = _spikes(previous, baseline, "screen_number")
    summary = {
        "newIssues": new_count,
        "previousNewIssues": previous_count,
        "changeRate": change_rate,
        "closedNow": sum(not _is_open(str(issue.get("status") or "")) for issue in previous),
        "carriedOpen": len(carried),
        "aging": len(aging),
        "overdue": len(overdue),
        "unassigned": len(unassigned),
        "highPriority": len(high_priority),
    }
    return {
        "contentVersion": TEAM_BRIEF_CONTENT_VERSION,
        "weekStart": current_start.isoformat(),
        "title": f"{current_start.month}월 {current_start.day}일 주간 팀 브리핑",
        "summaryPeriod": f"{previous_start.isoformat()} ~ {previous_end.isoformat()}",
        "comparisonPeriod": f"{baseline_start.isoformat()} ~ {baseline_end.isoformat()}",
        "summary": summary,
        "decisionSummary": _decision_summary(summary, customer_spikes, screen_spikes, priorities),
        "customerSpikes": customer_spikes,
        "screenSpikes": screen_spikes,
        "priorities": priorities,
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
    stored_payload = json.loads(row["payload_json"]) if row else None
    if stored_payload and stored_payload.get("contentVersion") == TEAM_BRIEF_CONTENT_VERSION:
        payload = stored_payload
    else:
        generated = _generate_team_brief(base)
        serialized = json.dumps(generated, ensure_ascii=False)
        with closing(sqlite3.connect(workspace_db_path())) as connection, connection:
            if refresh or row:
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
