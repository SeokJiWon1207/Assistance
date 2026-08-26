"""고객사 프로필 — 반복 조회 항목을 한 번에 묶는 결정형 Mantis 리포트."""

import re
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Dict, List, Optional

from backend.sources.mantis import (
    build_sources_from_issue_rows,
    fetch_mantis_recent_issues,
    issue_matches_constraints,
    normalize_issue,
)
from backend.understanding import extract_query_constraints


CUSTOMER_PROFILE_PATTERN = re.compile(r"고객사?\s*프로필|프로필\s*(?:카드|리포트)", re.IGNORECASE)
CUSTOMER_PROFILE_FETCH_LIMIT = 1000
CUSTOMER_PROFILE_AGING_DAYS = 30
CUSTOMER_PROFILE_TOP_N = 5


def _health_content(
    issues: List[Dict[str, Any]],
    open_issues: List[Dict[str, Any]],
    aging_rows: List[Dict[str, Any]],
    handling_days: List[int],
    monthly: Dict[str, int],
) -> Dict[str, Any]:
    """고객사 지표를 설명 가능한 건강도와 권장 확인 항목으로 변환한다."""
    total = len(issues)
    open_ratio = len(open_issues) / total if total else 0
    aging_ratio = len(aging_rows) / len(open_issues) if open_issues else 0
    average_handling = sum(handling_days) / len(handling_days) if handling_days else None
    reasons: List[str] = []
    actions: List[str] = []

    high_risk = len(aging_rows) >= 3 or aging_ratio >= 0.5 or (
        average_handling is not None and average_handling >= 30
    )
    needs_attention = bool(aging_rows) or open_ratio >= 0.5 or (
        average_handling is not None and average_handling >= 14
    )
    level = "위험" if high_risk else "주의" if needs_attention else "안정"

    reasons.append(f"미종료 비중 {open_ratio * 100:.0f}%")
    if aging_rows:
        reasons.append(f"장기 미종료 {len(aging_rows)}건")
        actions.append("장기 이슈의 종료 조건과 담당자별 다음 조치를 확인하세요.")
    if average_handling is not None:
        reasons.append(f"평균 처리기간 {average_handling:.1f}일")
    months = sorted(monthly)
    if len(months) >= 2:
        previous_count = monthly[months[-2]]
        latest_count = monthly[months[-1]]
        if latest_count > previous_count:
            reasons.append(f"최근 월 이슈 {previous_count}건→{latest_count}건")
            actions.append("최근 증가한 화면·증상의 공통 패턴을 점검하세요.")
    if not actions and open_issues:
        actions.append("미종료 이슈의 최신 상태와 완료 예정일을 확인하세요.")
    if not actions:
        actions.append("현재 수준을 유지하며 신규 이슈 추이를 모니터링하세요.")
    return {"level": level, "reasons": reasons, "actions": actions[:3]}


def _is_closed(status: Any) -> bool:
    label = str(status or "")
    return "종료" in label or "closed" in label.casefold()


def _parse_date(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def _scope_label(constraints: Dict[str, Any]) -> str:
    created_at = constraints.get("created_at") or {}
    date_from = created_at.get("from")
    date_to = created_at.get("to")
    if date_from and date_to:
        return f"{date_from}~{date_to}"
    return "조회된 최근 이슈"


def _customer_label(constraints: Dict[str, Any], issues: List[Dict[str, Any]]) -> str:
    """요청에서 확인한 고객사명을 우선하고, 없을 때만 조회 결과의 대표 프로젝트를 쓴다."""
    for key in ("customer_exact", "customer_key"):
        value = str(constraints.get(key) or "").strip()
        if value:
            return value
    return Counter(str(issue.get("project") or "미상") for issue in issues).most_common(1)[0][0]


def _failure(text: str, constraints: Dict[str, Any], message: str) -> Dict[str, Any]:
    return {
        "content": message,
        "sources": [],
        "retrieval": {
            "query": text,
            "constraints": constraints,
            "stages": [{"source": "mantis_customer_profile", "status": "error", "reason": message}],
        },
        "raw": {"mode": "deterministic_customer_profile"},
    }


def build_customer_profile_report(text: str) -> Optional[Dict[str, Any]]:
    """'고객사 프로필' 요청이면 추이·적체·담당자·처리시간을 함께 반환한다."""
    if not CUSTOMER_PROFILE_PATTERN.search(str(text or "")):
        return None

    constraints = extract_query_constraints(text)
    if not (constraints.get("customer_aliases") or constraints.get("customer_exact")):
        return _failure(text, constraints, "고객사 프로필을 만들 고객사명을 입력해 주세요.")
    try:
        raw_issues = fetch_mantis_recent_issues(total_limit=CUSTOMER_PROFILE_FETCH_LIMIT)
    except Exception as exc:
        return _failure(text, constraints, f"Mantis 고객사 프로필 조회에 실패했습니다: {exc}")

    issues = [
        normalize_issue(issue)
        for issue in raw_issues
        if issue_matches_constraints(issue, constraints)
    ]
    if not issues:
        return _failure(
            text,
            constraints,
            "지정한 고객사·기간·플랫폼·상태 조건에 맞는 이슈가 없습니다.",
        )

    today = date.today()
    customer = _customer_label(constraints, issues)
    open_issues = [issue for issue in issues if not _is_closed(issue.get("status"))]
    closed_issues = [issue for issue in issues if _is_closed(issue.get("status"))]
    aging_rows: List[Dict[str, Any]] = []
    for issue in open_issues:
        created = _parse_date(issue.get("created_at"))
        if created and (today - created).days >= CUSTOMER_PROFILE_AGING_DAYS:
            aging_rows.append({**issue, "age_days": (today - created).days})
    aging_rows.sort(key=lambda issue: issue["age_days"], reverse=True)

    handling_days: List[int] = []
    for issue in closed_issues:
        created = _parse_date(issue.get("created_at"))
        updated = _parse_date(issue.get("updated_at"))
        if created and updated and updated >= created:
            handling_days.append((updated - created).days)

    monthly: Dict[str, int] = defaultdict(int)
    for issue in issues:
        created_at = str(issue.get("created_at") or "")[:7]
        if re.fullmatch(r"\d{4}-\d{2}", created_at):
            monthly[created_at] += 1
    platforms = Counter(str(issue.get("media_type") or "기타/미상") for issue in issues)
    owners = Counter(str(issue.get("handler") or "담당자 미지정") for issue in issues)
    health = _health_content(issues, open_issues, aging_rows, handling_days, monthly)

    lines = [
        f"{customer} 고객사 프로필",
        f"기준: {_scope_label(constraints)} · 조건에 맞는 이슈 {len(issues)}건",
        "",
        f"운영 건강도: **{health['level']}**",
        "- 판단 근거: " + " · ".join(health["reasons"]),
        "",
        "핵심 지표",
        f"- 전체: **{len(issues)}건** · 미종료: **{len(open_issues)}건** · 종료: **{len(closed_issues)}건**",
        f"- {CUSTOMER_PROFILE_AGING_DAYS}일 이상 장기 미종료: **{len(aging_rows)}건**",
        "- 플랫폼: " + " · ".join(f"{name} {count}건" for name, count in platforms.most_common()),
    ]
    if handling_days:
        lines.append(f"- 평균 처리기간: **{sum(handling_days) / len(handling_days):.1f}일** (종료 {len(handling_days)}건 기준)")
    else:
        lines.append("- 평균 처리기간: 계산 가능한 종료 이슈 없음")

    lines.extend(["", "월별 이슈 추이"])
    if monthly:
        lines.extend(f"- {month}: {count}건" for month, count in sorted(monthly.items()))
    else:
        lines.append("- 등록일이 있는 이슈가 없습니다.")

    lines.extend(["", "주요 담당자"])
    lines.extend(f"- {owner}: {count}건" for owner, count in owners.most_common(CUSTOMER_PROFILE_TOP_N))

    lines.extend(["", f"{CUSTOMER_PROFILE_AGING_DAYS}일 이상 장기 미종료"])
    if aging_rows:
        lines.extend(
            f"- #{issue['id']} {issue['summary']} — {issue['age_days']}일 · {issue['handler']}"
            for issue in aging_rows[:CUSTOMER_PROFILE_TOP_N]
        )
    else:
        lines.append("- 해당 이슈가 없습니다.")

    recent = sorted(issues, key=lambda issue: (str(issue.get("created_at") or ""), int(issue.get("id") or 0)), reverse=True)
    lines.extend(["", "최근 주요 이슈"])
    lines.extend(
        f"- #{issue['id']} {issue['summary']} — {issue['status']} · {issue['handler']} · {issue['created_at']}"
        for issue in recent[:CUSTOMER_PROFILE_TOP_N]
    )
    lines.extend(["", "권장 확인 항목"])
    lines.extend(f"- {action}" for action in health["actions"])
    lines.extend(["", "※ 평균 처리기간은 정확한 종료 시각 대신 등록일→최종수정일을 사용한 근사값입니다."])

    source_rows = recent[:CUSTOMER_PROFILE_TOP_N]
    for issue in aging_rows[:CUSTOMER_PROFILE_TOP_N]:
        if issue.get("id") not in {row.get("id") for row in source_rows}:
            source_rows.append(issue)
    sources = build_sources_from_issue_rows(source_rows[:10])
    return {
        "content": "\n".join(lines),
        "sources": sources,
        "sourcesForFollowUps": sources,
        "retrieval": {
            "query": text,
            "constraints": constraints,
            "stages": [{
                "source": "mantis_customer_profile",
                "status": "ok",
                "issues_list": source_rows[:10],
                "meta": {
                    "matched_count": len(issues),
                    "open_count": len(open_issues),
                    "closed_count": len(closed_issues),
                    "aging_count": len(aging_rows),
                    "health_level": health["level"],
                },
            }],
        },
        "raw": {"mode": "deterministic_customer_profile"},
    }
