"""고객사·기간 기준 주간 운영 보고서."""

import re
from collections import Counter
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from backend.display import korean_day_range, status_name
from backend.mantis_status import mantis_status_bucket
from backend.sources.mantis_collection import collection_metadata
from backend.reports.effort import parse_effort_field_value
from backend.sources.mantis import (
    build_sources_from_issue_rows,
    fetch_mantis_recent_issues,
    issue_matches_constraints,
    normalize_issue,
)
from backend.reports.report_data import load_report_issues, report_scope_label
from backend.sources.mantis_store import read_complete_store
from backend.understanding import extract_query_constraints


WEEKLY_REPORT_PATTERN = re.compile(r"주간\s*(?:보고서|리포트)", re.IGNORECASE)
WEEKLY_REPORT_AGING_DAYS = 30
WEEKLY_REPORT_TOP_N = 5
WEEKLY_REPORT_MAX_DAYS = 31


def _is_closed(status: Any) -> bool:
    return mantis_status_bucket(str(status or "")) == "done"


def _parse_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _window(constraints: Dict[str, Any]) -> Optional[Tuple[date, date]]:
    created_at = constraints.get("created_at") or {}
    start = _parse_date(created_at.get("from"))
    end = _parse_date(created_at.get("to"))
    if not start or not end or end < start:
        return None
    if (end - start).days + 1 > WEEKLY_REPORT_MAX_DAYS:
        return None
    return start, end


def _customer_label(constraints: Dict[str, Any], issues: List[Dict[str, Any]]) -> str:
    for key in ("customer_exact", "customer_key"):
        value = str(constraints.get(key) or "").strip()
        if value:
            return value
    if not issues:
        return "전체 고객사"
    return Counter(str(issue.get("project") or "미상") for issue in issues).most_common(1)[0][0]


def _effort_total(issue: Dict[str, Any]) -> float:
    return round(sum(
        amount
        for value in (issue.get("effort_fields") or {}).values()
        for _person, amount in parse_effort_field_value(value)
    ), 4)


def _failure(
    text: str,
    constraints: Dict[str, Any],
    message: str,
    *,
    error_kind: str = "internal",
) -> Dict[str, Any]:
    return {
        "content": message,
        "sources": [],
        "retrieval": {
            "query": text,
            "constraints": constraints,
            "stages": [{
                "source": "mantis_weekly_report",
                "status": "error",
                "error_kind": error_kind,
                "reason": message,
                "issues_list": [],
            }],
        },
        "raw": {"mode": "deterministic_weekly_report"},
    }


def build_weekly_report(text: str) -> Optional[Dict[str, Any]]:
    """주간 보고서 요청을 신규·미종료·적체·공수·처리시간 한 묶음으로 만든다."""
    if not WEEKLY_REPORT_PATTERN.search(str(text or "")):
        return None
    constraints = extract_query_constraints(text)
    bounds = _window(constraints)
    if not bounds:
        return _failure(
            text,
            constraints,
            f"보고할 기간을 '이번 주' 또는 '지난주'처럼 {WEEKLY_REPORT_MAX_DAYS}일 이내로 입력해 주세요.",
            error_kind="validation",
        )
    start, end = bounds
    today = date.today()
    effective_end = min(end, today) if start <= today <= end else end
    span = (effective_end - start).days + 1
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=span - 1)

    try:
        raw_issues = load_report_issues(read_complete_store, fetch_mantis_recent_issues)
    except Exception as exc:
        return _failure(
            text,
            constraints,
            f"Mantis 주간 보고 조회에 실패했습니다: {exc}",
            error_kind="dependency",
        )

    data_meta = collection_metadata(raw_issues)
    # 보고서가 신규·미종료·종료를 함께 세므로 질문의 상태·기간 표현으로 범위를
    # 좁히지 않는다. '미종료 이슈'라는 표현 때문에 종료 건이 빠지면 처리시간과
    # 종료 건수가 항상 0이 된다.
    scope_constraints = {
        key: value for key, value in constraints.items()
        if key not in {"created_at", "filter_clauses", "statuses", "excluded_statuses"}
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

    closed_in_window = [
        issue for issue in scoped
        if _is_closed(issue.get("status"))
        and start <= (_parse_date(issue.get("updated_at")) or date.min) <= effective_end
    ]
    open_rows = [issue for issue in scoped if mantis_status_bucket(str(issue.get("status") or "")) == "open"]
    open_in_window = [issue for issue in current if mantis_status_bucket(str(issue.get("status") or "")) == "open"]
    unknown_count = sum(mantis_status_bucket(str(issue.get("status") or "")) == "unknown" for issue in scoped)

    handling_days = []
    for issue in closed_in_window:
        created = _parse_date(issue.get("created_at"))
        updated = _parse_date(issue.get("updated_at"))
        if created and updated and updated >= created:
            handling_days.append((updated - created).days)

    aging = []
    for issue in open_rows:
        created = _parse_date(issue.get("created_at"))
        if created and created <= effective_end:
            age_days = (today - created).days
            if age_days >= WEEKLY_REPORT_AGING_DAYS:
                aging.append({**issue, "age_days": age_days})
    aging.sort(key=lambda issue: issue["age_days"], reverse=True)

    current_with_effort = [{**issue, "effort_total": _effort_total(issue)} for issue in current]
    total_effort = sum(issue["effort_total"] for issue in current_with_effort)
    effort_by_person: Dict[str, float] = {}
    for issue in current:
        for value in (issue.get("effort_fields") or {}).values():
            for person, amount in parse_effort_field_value(value):
                effort_by_person[person] = effort_by_person.get(person, 0.0) + amount
    ranked_effort = sorted(effort_by_person.items(), key=lambda row: row[1], reverse=True)

    delta = len(current) - len(previous)
    rate = None if not previous else delta / len(previous) * 100
    comparison = f"{delta:+d}건 ({rate:+.1f}%)" if rate is not None else f"{delta:+d}건 (직전 기간 0건)"

    lines = [
        f"{korean_day_range(start.isoformat(), effective_end.isoformat())} {customer} 주간 보고서",
        f"기준: 등록일 {korean_day_range(start.isoformat(), effective_end.isoformat())} · {report_scope_label(raw_issues)}",
        "",
        "요약",
        f"- 신규 등록: **{len(current)}건** · 직전 {span}일 {len(previous)}건 대비 **{comparison}**",
        f"- 기간 내 해결·종료: **{len(closed_in_window)}건**",
        f"- 현재 미종료: **{len(open_rows)}건** (이 중 이번 기간 등록 {len(open_in_window)}건)",
        f"- 장기 적체: **{len(aging)}건** ({WEEKLY_REPORT_AGING_DAYS}일 이상 미종료)",
        f"- 기간 공수 합계: **{total_effort:.2f}**",
        f"- 평균 처리기간: **{sum(handling_days) / len(handling_days):.1f}일** (기간 내 해결·종료 {len(handling_days)}건 기준)"
        if handling_days else "- 평균 처리기간: 기간 내 해결·종료 이슈가 없어 계산하지 않았습니다.",
        "",
        "신규 이슈",
    ]
    if unknown_count:
        lines.insert(lines.index("신규 이슈"), f"- 상태 미확인: **{unknown_count}건** (미종료·해결·종료 집계에서 제외)")
    if current_with_effort:
        lines.extend(
            f"- #{issue['id']} {issue['summary']} — {status_name(issue['status'])} · {issue['handler']}"
            for issue in sorted(current_with_effort, key=lambda row: int(row.get("id") or 0), reverse=True)[:WEEKLY_REPORT_TOP_N]
        )
    else:
        lines.append("- 이번 기간에 등록된 이슈가 없습니다.")

    lines.extend(["", "미종료 이슈"])
    if open_rows:
        lines.extend(
            f"- #{issue['id']} {issue['summary']} — {status_name(issue['status'])} · {issue['handler']}"
            for issue in sorted(open_rows, key=lambda row: int(row.get("id") or 0), reverse=True)[:WEEKLY_REPORT_TOP_N]
        )
    else:
        lines.append("- 현재 미종료 이슈가 없습니다.")

    lines.extend(["", "장기 적체"])
    if aging:
        lines.extend(
            f"- #{issue['id']} {issue['summary']} — 현재 {issue['age_days']}일 경과 · {issue['handler']}"
            for issue in aging[:WEEKLY_REPORT_TOP_N]
        )
    else:
        lines.append(f"- {WEEKLY_REPORT_AGING_DAYS}일 이상 미종료된 이슈가 없습니다.")

    lines.extend(["", "공수·처리시간"])
    lines.append(
        "- 담당자별 공수: " + (
            " · ".join(f"{person} {amount:.2f}" for person, amount in ranked_effort[:WEEKLY_REPORT_TOP_N])
            or "기간 내 입력된 공수가 없습니다."
        )
    )
    if handling_days:
        lines.append(
            f"- 처리기간: 평균 {sum(handling_days) / len(handling_days):.1f}일 · "
            f"최장 {max(handling_days)}일 · 최단 {min(handling_days)}일"
        )
    else:
        lines.append("- 처리기간: 기간 내 종료된 이슈가 없습니다.")

    lines.extend([
        "",
        "※ 상태와 미종료 건수는 조회 시점의 Mantis 상태입니다.",
        "※ 처리기간은 정확한 해결·종료 시각 대신 등록일→최종수정일을 사용한 근사값입니다.",
    ])

    # 근거 목록은 본문의 신규·미종료·장기 적체 목록과 같아야 한다. 하나라도 빠지면
    # 응답 검증이 본문의 이슈 번호를 근거 없는 번호로 보고 답변 전체를 보류한다.
    source_rows: List[Dict[str, Any]] = []
    known_ids = set()
    for issue in [
        *sorted(current_with_effort, key=lambda row: int(row.get("id") or 0), reverse=True)[:WEEKLY_REPORT_TOP_N],
        *sorted(open_rows, key=lambda row: int(row.get("id") or 0), reverse=True)[:WEEKLY_REPORT_TOP_N],
        *aging[:WEEKLY_REPORT_TOP_N],
    ]:
        if issue.get("id") not in known_ids:
            known_ids.add(issue.get("id"))
            source_rows.append(issue)
    sources = build_sources_from_issue_rows(source_rows)
    return {
        "content": "\n".join(lines),
        "sources": sources,
        "sourcesForFollowUps": sources,
        "retrieval": {
            "query": text,
            "constraints": constraints,
            "stages": [{
                "source": "mantis_weekly_report",
                "status": "ok",
                "issues_list": source_rows,
                "meta": {
                    **data_meta,
                    "current_count": len(current),
                    "previous_count": len(previous),
                    "closed_count": len(closed_in_window),
                    "unknown_status_count": unknown_count,
                    "open_count": len(open_rows),
                    "backlog_ids": [issue['id'] for issue in open_rows],
                    "aging_count": len(aging),
                    "total_effort": round(total_effort, 2),
                },
            }],
        },
        "raw": {"mode": "deterministic_weekly_report"},
    }
