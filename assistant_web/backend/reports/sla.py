"""처리시간(SLA) 리포트 — 종료 이슈 리드타임과 미종료 이슈 적체 분석(상위 계층).

라이브 Mantis 이슈의 등록일/최종수정일/상태로 처리 성과를 근사한다. 로컬 스냅샷에는
최종수정일·상태 변경 이력이 없어 이 리포트는 라이브 API 조회를 기준으로 한다.

- 완료 모드: '종료' 상태 이슈의 리드타임(등록→최종수정, 근사)을 집계한다.
- 적체 모드: 아직 종료되지 않은 이슈의 경과일(오늘−등록일)을 랭킹한다.

주의: 이 시스템은 '해결'을 아직 열린 상태로 본다(config STATUS_MATCH_ALIASES). 완료의
기준은 '종료'다. 정확한 종료 시각 필드가 없어 최종수정일을 완료 시각의 근사로 쓴다.

의존: mantis(이슈 조회/정규화/조건매칭) + understanding(제약추출) + query_planning(그룹축).
"""

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from backend.query_planning import extract_group_by
from backend.sources.mantis import (
    build_sources_from_issue_rows,
    fetch_mantis_recent_issues,
    issue_matches_constraints,
    normalize_issue,
)
from backend.understanding import extract_query_constraints


# 처리시간을 묻는 질문. 완료(리드타임)와 적체(경과일)를 구분해 잡는다.
SLA_DURATION_MARKERS = (
    "처리시간", "처리 시간", "처리기간", "처리 기간", "소요", "리드타임", "리드 타임",
    "며칠 걸", "얼마나 걸", "평균 처리", "해결까지", "종료까지", "완료까지",
)
# 적체는 '오래 걸린다/방치된다'는 시간축 신호로만 잡는다. '미종료' 같은 순수 상태어는
# "미종료 이슈 몇 건" 같은 건수 질문과 겹치므로 넣지 않는다.
SLA_AGING_MARKERS = (
    "오래된", "오래 된", "오래 걸리는", "묵은", "방치", "적체",
    "지연", "장기", "밀린",
)

SLA_AGING_THRESHOLD_DAYS = 30   # 이 일수를 넘긴 미종료 이슈를 '적체'로 본다
SLA_TOP_N = 10                  # 랭킹/대표 노출 개수
SLA_FETCH_LIMIT = 800           # 라이브에서 훑을 최근 이슈 수(공수 체커와 동일 규모)


def _is_closed(status_label: str) -> bool:
    label = str(status_label or "")
    return "종료" in label or "closed" in label.casefold()


def _parse_date(value: str) -> Optional[date]:
    text = str(value or "")[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _median(values: List[int]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2


def _sla_mode(text: str) -> Optional[str]:
    normalized = str(text or "")
    has_duration = any(marker in normalized for marker in SLA_DURATION_MARKERS)
    has_aging = any(marker in normalized for marker in SLA_AGING_MARKERS)
    if has_aging and not has_duration:
        return "aging"
    if has_duration:
        return "duration"
    return None


def _group_axis(text: str) -> Optional[Tuple[str, Any, str]]:
    """SLA 집계를 고객사/담당자로 나눠 볼지 결정한다."""
    group_by = extract_group_by(text)
    if group_by == "customer":
        return "customer", (lambda issue: str(issue.get("project") or "미상")), "고객사"
    if group_by == "owner":
        return "owner", (lambda issue: str(issue.get("handler") or "담당자 미지정")), "담당자"
    return None


def _load_matching_issues(constraints: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_issues = fetch_mantis_recent_issues(total_limit=SLA_FETCH_LIMIT)
    matched = []
    for issue in raw_issues:
        normalized = normalize_issue(issue)
        if issue_matches_constraints(issue, constraints):
            matched.append(normalized)
    return matched


def _unavailable(text: str, constraints: Dict[str, Any], reason: str) -> Dict[str, Any]:
    return {
        "content": f"Mantis 처리시간 분석 도구를 사용할 수 없습니다.\n\n{reason}",
        "sources": [],
        "retrieval": {
            "query": text,
            "constraints": constraints,
            "stages": [{"source": "mantis_sla", "status": "error", "reason": reason}],
        },
        "raw": {"mode": "deterministic_mantis_sla"},
    }


def build_sla_report(text: str) -> Optional[Dict[str, Any]]:
    """처리시간/적체 질문에 라이브 이슈 기반 결정형 리포트로 답한다."""
    mode = _sla_mode(text)
    if not mode:
        return None

    constraints = extract_query_constraints(text)
    try:
        issues = _load_matching_issues(constraints)
    except Exception as exc:  # 네트워크/설정 오류는 조용히 흘리지 않고 사유를 남긴다.
        return _unavailable(text, constraints, f"Mantis 라이브 조회 실패: {exc}")

    if not issues:
        return _unavailable(
            text, constraints,
            "조건에 맞는 최근 이슈를 라이브에서 찾지 못했습니다. 조건을 바꿔 다시 시도해 주세요.",
        )

    axis = _group_axis(text)
    if mode == "duration":
        return _build_duration_report(text, constraints, issues, axis)
    return _build_aging_report(text, constraints, issues, axis)


def _build_duration_report(
    text: str,
    constraints: Dict[str, Any],
    issues: List[Dict[str, Any]],
    axis: Optional[Tuple[str, Any, str]],
) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for issue in issues:
        if not _is_closed(issue.get("status")):
            continue
        created = _parse_date(issue.get("created_at"))
        updated = _parse_date(issue.get("updated_at"))
        if not created or not updated:
            continue
        days = (updated - created).days
        if days < 0:
            continue
        rows.append({**issue, "handling_days": days})

    scope = f"최근 라이브 {len(issues)}건 중 종료 이슈 {len(rows)}건"
    if not rows:
        lines = [
            "처리시간(리드타임) 분석 결과",
            f"기준: {scope}",
            "",
            "종료된 이슈가 없어 리드타임을 계산할 수 없습니다.",
        ]
        return _finalize(text, constraints, lines, [], "duration")

    all_days = [row["handling_days"] for row in rows]
    avg = sum(all_days) / len(all_days)
    med = _median(all_days)
    lines = [
        "처리시간(리드타임) 분석 — 등록일→최종수정일 근사",
        f"기준: {scope}",
        "",
        f"- 평균 처리기간: **{avg:.1f}일**",
        f"- 중앙값: **{med:.1f}일**",
        f"- 최장: {max(all_days)}일 · 최단: {min(all_days)}일",
    ]

    if axis:
        _axis_field, axis_extract, axis_label = axis
        buckets: Dict[str, List[int]] = {}
        for row in rows:
            buckets.setdefault(str(axis_extract(row)), []).append(row["handling_days"])
        ranked = sorted(
            buckets.items(),
            key=lambda item: (sum(item[1]) / len(item[1])),
            reverse=True,
        )[:SLA_TOP_N]
        lines.extend(["", f"{axis_label}별 평균 처리기간 (느린 순)"])
        for name, days_list in ranked:
            lines.append(
                f"- {name} — 평균 {sum(days_list) / len(days_list):.1f}일 ({len(days_list)}건)"
            )

    rows.sort(key=lambda row: row["handling_days"], reverse=True)
    lines.extend(["", "가장 오래 걸린 이슈"])
    for row in rows[:5]:
        lines.append(
            f"- #{row['id']} {row['summary']} — {row['handling_days']}일 "
            f"({row['project']} · {row['created_at']}→{row['updated_at']})"
        )
    lines.extend([
        "",
        "※ 정확한 종료 시각 필드가 없어 최종수정일을 완료 시각의 근사로 사용했습니다.",
    ])
    return _finalize(text, constraints, lines, rows[:SLA_TOP_N], "duration")


def _build_aging_report(
    text: str,
    constraints: Dict[str, Any],
    issues: List[Dict[str, Any]],
    axis: Optional[Tuple[str, Any, str]],
) -> Dict[str, Any]:
    today = date.today()
    rows: List[Dict[str, Any]] = []
    for issue in issues:
        if _is_closed(issue.get("status")):
            continue
        created = _parse_date(issue.get("created_at"))
        if not created:
            continue
        age = (today - created).days
        if age < 0:
            continue
        rows.append({**issue, "age_days": age})

    over_threshold = [row for row in rows if row["age_days"] >= SLA_AGING_THRESHOLD_DAYS]
    scope = f"최근 라이브 {len(issues)}건 중 미종료 이슈 {len(rows)}건"
    lines = [
        f"장기 미종료(적체) 분석 — {SLA_AGING_THRESHOLD_DAYS}일 이상 경과",
        f"기준: {scope} (기준일 {today.isoformat()})",
        "",
        f"- {SLA_AGING_THRESHOLD_DAYS}일 이상 경과한 미종료 이슈: **{len(over_threshold)}건**",
    ]
    if rows:
        ages = [row["age_days"] for row in rows]
        lines.append(f"- 미종료 이슈 평균 경과일: **{sum(ages) / len(ages):.1f}일** (최장 {max(ages)}일)")

    if axis and over_threshold:
        _axis_field, axis_extract, axis_label = axis
        buckets: Dict[str, int] = {}
        for row in over_threshold:
            key = str(axis_extract(row))
            buckets[key] = buckets.get(key, 0) + 1
        ranked = sorted(buckets.items(), key=lambda item: (-item[1], item[0]))[:SLA_TOP_N]
        lines.extend(["", f"{axis_label}별 적체 건수 (많은 순)"])
        for name, count in ranked:
            lines.append(f"- {name} — {count}건")

    rows.sort(key=lambda row: row["age_days"], reverse=True)
    if rows:
        lines.extend(["", "가장 오래 방치된 미종료 이슈"])
        for row in rows[:SLA_TOP_N]:
            lines.append(
                f"- #{row['id']} {row['summary']} — {row['age_days']}일 경과 "
                f"({row['project']} · 등록 {row['created_at']})"
            )
    else:
        lines.extend(["", "조건에 맞는 미종료 이슈가 없습니다."])
    return _finalize(text, constraints, lines, rows[:SLA_TOP_N], "aging")


def _finalize(
    text: str,
    constraints: Dict[str, Any],
    lines: List[str],
    example_rows: List[Dict[str, Any]],
    mode: str,
) -> Dict[str, Any]:
    sources = build_sources_from_issue_rows(example_rows) if example_rows else []
    retrieval = {
        "query": text,
        "constraints": constraints,
        "stages": [{
            "source": "mantis_sla",
            "status": "ok",
            "issues_list": example_rows,
            "meta": {"mode": mode, "displayed_count": len(example_rows)},
        }],
    }
    return {
        "content": "\n".join(lines),
        "sources": sources,
        "sourcesForFollowUps": sources,
        "retrieval": retrieval,
        "raw": {"mode": "deterministic_mantis_sla"},
    }
