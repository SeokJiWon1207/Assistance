"""Mantis 결정형 집계 리포트 빌더.

건수, 급증, 피벗 집계와 이에 필요한 표시 헬퍼를 답변 포맷 계층에서 분리한다.
"""

import re
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from backend.common import normalize_chroma_issue_document, normalize_for_match
from backend.config import CHROMA_DB_PATH
from backend.query_filters import filter_clause_label, normalize_filter_clauses
from backend.query_planning import build_query_plan
from backend.retrieval import collect_retrieved_issues
from backend.sources.mantis import (
    build_sources_from_issue_rows,
    collect_mantis_live_ids_in_range,
    extract_snapshot_limit,
    fetch_mantis_recent_issues,
    issue_created_year_month,
    issue_matches_constraints,
    normalize_issue,
)
from backend.tools.mantis_snapshot import scan_mantis_documents
from backend.understanding import (
    chroma_text_matches_constraints,
    extract_query_constraints,
    has_constraint,
    merge_query_plan_constraints,
)

def extract_month_count_query(text: str) -> Optional[Dict[str, int]]:
    normalized_text = normalize_for_match(text)
    if not any(keyword in normalized_text for keyword in ("몇개", "몇건", "건수", "몇개야", "몇건이야")):
        return None
    month_match = re.search(r"(1[0-2]|0?[1-9])\s*월", text)
    if not month_match:
        return None
    year_match = re.search(r"(20\d{2})\s*년", text)
    year = int(year_match.group(1)) if year_match else date.today().year
    month = int(month_match.group(1))
    return {
        "year": year,
        "month": month,
        "snapshot_limit": extract_snapshot_limit(text),
    }

COUNT_GROUP_LABELS = {
    "customer": "고객사",
    "status": "상태",
    "owner": "담당자",
    "month": "월",
}


def count_group_key(group_by: str):
    """건수 집계에 쓸 그룹 값을 이슈에서 뽑는 함수를 돌려준다."""
    if group_by == "customer":
        return lambda issue: str(issue.get("project") or "")
    if group_by == "status":
        return lambda issue: display_status_label(issue.get("status"))
    if group_by == "owner":
        return lambda issue: str(issue.get("handler") or "")
    if group_by == "month":
        # 등록일 YYYY-MM-DD에서 월까지만 남긴다.
        return lambda issue: str(issue.get("created_at") or "")[:7]
    return None


def format_count_group_lines(group_counts: Dict[str, int], group_by: str) -> List[str]:
    """그룹별 건수를 많은 순으로 정리한다."""
    if not group_counts:
        return []
    label = COUNT_GROUP_LABELS.get(group_by, "그룹")
    if group_by == "month":
        # 추이는 많은 순이 아니라 시간순으로 봐야 의미가 있다. 단순 나열에 그치지
        # 않고 전월 대비 증감과 추세 요약까지 붙여 '추세 리포트'로 읽히게 한다.
        return format_month_trend_lines(group_counts, label)
    ordered = sorted(group_counts.items(), key=lambda item: (-item[1], item[0]))
    lines = ["", f"{label}별 건수 (많은 순, {len(ordered)}개 {label})"]
    lines.extend(f"- {name} — {count}건" for name, count in ordered)
    return lines


def format_month_trend_lines(group_counts: Dict[str, int], label: str) -> List[str]:
    """월별 건수를 시간순으로 나열하되 전월 대비 증감과 추세 요약을 덧붙인다."""
    ordered = sorted(group_counts.items(), key=lambda item: item[0])
    lines = ["", f"{label}별 건수 (시간순, {len(ordered)}개 {label})"]
    prev: Optional[int] = None
    for name, count in ordered:
        if prev is None:
            lines.append(f"- {name} — {count}건")
        else:
            diff = count - prev
            arrow = "▲" if diff > 0 else ("▼" if diff < 0 else "―")
            if diff == 0:
                lines.append(f"- {name} — {count}건 (― 변동 없음)")
            else:
                pct = (diff / prev * 100) if prev else 0.0
                lines.append(f"- {name} — {count}건 ({arrow}{abs(diff)}, {pct:+.1f}%)")
        prev = count
    counts = [count for _, count in ordered]
    if len(counts) >= 2:
        avg = sum(counts) / len(counts)
        peak_name, peak_count = max(ordered, key=lambda item: item[1])
        last_name, last_count = ordered[-1]
        vs_avg = ((last_count - avg) / avg * 100) if avg else 0.0
        lines.extend([
            "",
            f"요약: 최다 {peak_name}({peak_count}건), 월평균 {avg:.1f}건, "
            f"최근 {last_name}은 평균 대비 {vs_avg:+.1f}%.",
        ])
    return lines


TOP_CUSTOMER_QUESTION = re.compile(
    r"(?:문제|이슈|장애|오류)[^가-힣]{0,6}(?:가|이)?\s*(?:많은|많이|잦은|빈번)"
    r"[^가-힣]{0,4}(?:고객사|프로젝트|증권사)"
    r"|(?:고객사|프로젝트|증권사)[^가-힣]{0,4}(?:중에서?|중)?\s*"
    r"(?:문제|이슈|장애|오류)[^가-힣]{0,6}(?:가|이)?\s*(?:많은|많이|잦은|빈번)"
)


def build_top_customers_report(text: str) -> Optional[Dict[str, Any]]:
    """"문제 많은 고객사"처럼 기준이 빠진 질문에 기준을 밝히고 답한다.

    "많다"의 기준은 질문에 없다. 임의 기준으로 조용히 답하는 대신 무엇을
    세었는지(미종료 이슈 건수)와 데이터 범위를 함께 밝힌다."""
    if not TOP_CUSTOMER_QUESTION.search(str(text or "")):
        return None
    plan = {
        "operation": "count",
        "source": "mantis",
        "group_by": "customer",
        "filters": {"statuses": ["open"]},
        "filter_clauses": [{"field": "status", "operator": "in", "value": ["open"]}],
    }
    report = build_mantis_count_report("미종료 이슈", plan)
    if not report:
        return None
    note = (
        "기준을 정해서 답했어요: **미종료(열린) 이슈 건수**가 많은 순입니다. "
        "'요즘'의 범위는 따로 지정하지 않아 스냅샷 전체를 기준으로 셌습니다. "
        "특정 기간이나 다른 기준(예: 장애 건수, 고객사요청일)으로 다시 보시려면 말씀해 주세요.\n"
    )
    return {**report, "content": note + "\n" + report["content"]}


# 급증(이상탐지) 판정 기준. "많은 순"이 아니라 "평소 대비 튄 순"으로 답한다.
SPIKE_MIN_RECENT = 3          # 최근 월 최소 건수(노이즈 컷)
SPIKE_RATIO = 1.5             # 직전 평균 대비 몇 배 이상이면 급증으로 본다
SPIKE_BASELINE_MONTHS = 3     # 직전 몇 개월 평균을 기준선으로 쓸지
SPIKE_TOP_N = 10             # 최대 노출 개수

SPIKE_QUESTION = re.compile(
    r"급증|폭증|급등"
    r"|(?:평소|여느\s*때)\s*(?:보다|대비)"
    r"|갑자기\s*(?:늘|많|증가|생긴)"
    r"|최근\s*들어\s*(?:늘|많|증가)"
    r"|늘어난|많아진|잦아진|튄"
)


def _spike_axis(text: str) -> Tuple[str, Any, str]:
    """급증 집계 축을 질문에서 고른다. 기본은 고객사."""
    normalized = str(text or "")
    if re.search(r"화면\s*번호|화면", normalized):
        return "screen", (lambda issue: str(issue.get("screen_number") or "")), "화면번호"
    if re.search(r"담당자|담당", normalized):
        return "owner", (lambda issue: str(issue.get("handler") or "")), "담당자"
    return "customer", (lambda issue: str(issue.get("project") or "")), "고객사"


def build_mantis_spike_report(text: str) -> Optional[Dict[str, Any]]:
    """평소 대비 이슈가 급증한 축(고객사/화면번호/담당자)을 찾아 답한다.

    '많은 순'이 아니라 최근 월 건수를 직전 몇 개월 평균과 비교해 '튄 순'으로
    정렬한다. 기준선이 없는(직전 0건) 신규 급증도 함께 드러낸다.
    """
    if not SPIKE_QUESTION.search(str(text or "")):
        return None

    axis_key, axis_extract, axis_label = _spike_axis(text)
    constraints = extract_query_constraints(text)
    # 축값과 등록월을 한 번의 스캔으로 함께 세기 위해 복합 그룹키를 쓴다.
    sep = "\x1f"

    def group_key(issue: Dict[str, Any]) -> str:
        axis_value = str(axis_extract(issue) or "").strip() or "미지정"
        month = str(issue.get("created_at") or "")[:7]
        return f"{axis_value}{sep}{month}"

    snapshot = scan_mantis_documents(
        CHROMA_DB_PATH,
        lambda content: chroma_text_matches_constraints(content, constraints),
        normalize_chroma_issue_document,
        example_limit=1,
        group_key=group_key,
        constraints=constraints,
    )
    if not snapshot["available"]:
        return {
            "content": f"Mantis 집계 도구를 사용할 수 없습니다.\n\n{snapshot['reason']}",
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_spike", "status": "error", "reason": snapshot["reason"]}],
            },
            "raw": {"mode": "deterministic_mantis_spike"},
        }

    # 복합키를 축값 -> {월: 건수}로 되돌린다. 월이 비어 있는(등록일 없는 옛 문서)
    # 버킷은 시계열 비교에 쓸 수 없어 제외한다.
    per_axis: Dict[str, Dict[str, int]] = {}
    all_months: set = set()
    for composite, count in (snapshot.get("group_counts") or {}).items():
        axis_value, _, month = str(composite).partition(sep)
        if len(month) != 7:
            continue
        per_axis.setdefault(axis_value, {})[month] = per_axis.get(axis_value, {}).get(month, 0) + count
        all_months.add(month)

    ordered_months = sorted(all_months)
    if len(ordered_months) < 2:
        return {
            "content": (
                "급증 여부를 판단하려면 최소 2개 이상의 월 데이터가 필요한데, "
                "현재 스냅샷의 등록월 구간이 부족합니다. "
                "`scripts/make_vector_db.py --full`로 등록일을 채워 재구축하면 정확해집니다."
            ),
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_spike", "status": "insufficient_history",
                            "meta": {"months": ordered_months}}],
            },
            "raw": {"mode": "deterministic_mantis_spike"},
        }

    recent_month = ordered_months[-1]
    baseline_months = ordered_months[-(SPIKE_BASELINE_MONTHS + 1):-1]

    flagged: List[Dict[str, Any]] = []
    for axis_value, monthly in per_axis.items():
        if axis_value in {"미지정", "미상", "담당자 미지정"}:
            continue
        recent = monthly.get(recent_month, 0)
        if recent < SPIKE_MIN_RECENT:
            continue
        base_vals = [monthly.get(month, 0) for month in baseline_months]
        baseline_avg = (sum(base_vals) / len(base_vals)) if base_vals else 0.0
        if baseline_avg == 0:
            ratio = float("inf")  # 직전 구간에 없던 신규 급증
        elif recent / baseline_avg >= SPIKE_RATIO:
            ratio = recent / baseline_avg
        else:
            continue
        flagged.append({
            "axis": axis_value,
            "recent": recent,
            "baseline_avg": baseline_avg,
            "ratio": ratio,
        })

    flagged.sort(key=lambda item: (item["ratio"], item["recent"]), reverse=True)
    flagged = flagged[:SPIKE_TOP_N]

    baseline_desc = (
        f"직전 {len(baseline_months)}개월({baseline_months[0]}~{baseline_months[-1]}) 평균"
        if baseline_months else "직전 구간"
    )
    lines = [
        f"급증 판정 기준: 최근 월({recent_month}) 건수 vs {baseline_desc}, "
        f"최소 {SPIKE_MIN_RECENT}건 이상, {SPIKE_RATIO}배 이상.",
        f"기준 데이터: Mantis 인덱스 전체 {snapshot['scanned_count']}건",
        "",
    ]
    if not flagged:
        lines.append(f"현재 기준으로 평소 대비 급증한 {axis_label}는 감지되지 않았습니다.")
    else:
        lines.append(f"평소 대비 급증한 {axis_label} ({len(flagged)}개, 급증 강도 순)")
        for item in flagged:
            if item["ratio"] == float("inf"):
                lines.append(
                    f"- {item['axis']} — 최근 {recent_month} {item['recent']}건 "
                    f"(신규 급증, 직전 0건) ▲"
                )
            else:
                lines.append(
                    f"- {item['axis']} — 최근 {recent_month} {item['recent']}건 "
                    f"(직전 평균 {item['baseline_avg']:.1f}건, {item['ratio']:.1f}배) ▲"
                )

    retrieval = {
        "query": text,
        "constraints": constraints,
        "stages": [{
            "source": "mantis_spike",
            "status": "ok",
            "meta": {
                "axis": axis_key,
                "recent_month": recent_month,
                "baseline_months": baseline_months,
                "scanned_count": snapshot["scanned_count"],
                "flagged_count": len(flagged),
                "min_recent": SPIKE_MIN_RECENT,
                "ratio_threshold": SPIKE_RATIO,
            },
        }],
    }
    return {
        "content": "\n".join(lines),
        "sources": [],
        "retrieval": retrieval,
        "raw": {"mode": "deterministic_mantis_spike"},
    }


# 피벗(두 축 교차) 표. "고객사별 월별"처럼 축과 월이 함께 지정될 때만 잡는다.
PIVOT_MONTH_PATTERN = re.compile(r"(?:월|달)\s*(?:별(?:로)?|마다|기준(?:으)?로)")
PIVOT_AXIS_PATTERNS = {
    "customer": (re.compile(r"(?:고객사|프로젝트|증권사)\s*(?:별(?:로)?|마다)"),
                 lambda issue: str(issue.get("project") or ""), "고객사"),
    "screen": (re.compile(r"화면\s*번호?\s*(?:별(?:로)?|마다)"),
               lambda issue: str(issue.get("screen_number") or ""), "화면번호"),
    "owner": (re.compile(r"(?:담당자|담당)\s*(?:별(?:로)?|마다)"),
              lambda issue: str(issue.get("handler") or ""), "담당자"),
    "status": (re.compile(r"상태\s*(?:별(?:로)?|마다)"),
               lambda issue: display_status_label(issue.get("status")), "상태"),
}

PIVOT_MAX_MONTHS = 6   # 표 가독성을 위해 최근 N개월만 열로 보인다
PIVOT_MAX_ROWS = 15    # 합계 상위 N개 행만 보인다


def _detect_pivot_axis(text: str) -> Optional[Tuple[str, Any, str]]:
    """'축별 월별' 두 축 요청이면 (field, extractor, label)을 돌려준다."""
    normalized = str(text or "")
    if not PIVOT_MONTH_PATTERN.search(normalized):
        return None
    for field, (pattern, extractor, label) in PIVOT_AXIS_PATTERNS.items():
        if pattern.search(normalized):
            return field, extractor, label
    return None


def build_mantis_pivot_report(text: str) -> Optional[Dict[str, Any]]:
    """축(고객사/화면번호/담당자/상태) × 월 교차 건수를 표로 보여준다.

    단일 축 집계(build_mantis_count_report)와 달리 두 축을 동시에 요청했을 때만
    동작한다. 한 번의 스캔으로 (축값×월) 복합 집계를 낸 뒤 피벗 표로 조립한다.
    """
    detected = _detect_pivot_axis(text)
    if not detected:
        return None
    axis_field, axis_extract, axis_label = detected

    constraints = extract_query_constraints(text)
    sep = "\x1f"

    def group_key(issue: Dict[str, Any]) -> str:
        axis_value = str(axis_extract(issue) or "").strip() or "미지정"
        month = str(issue.get("created_at") or "")[:7]
        return f"{axis_value}{sep}{month}"

    snapshot = scan_mantis_documents(
        CHROMA_DB_PATH,
        lambda content: chroma_text_matches_constraints(content, constraints),
        normalize_chroma_issue_document,
        example_limit=1,
        group_key=group_key,
        constraints=constraints,
    )
    if not snapshot["available"]:
        return {
            "content": f"Mantis 집계 도구를 사용할 수 없습니다.\n\n{snapshot['reason']}",
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_pivot", "status": "error", "reason": snapshot["reason"]}],
            },
            "raw": {"mode": "deterministic_mantis_pivot"},
        }

    # 복합키를 축값 -> {월: 건수}로 되돌린다. 등록월이 없는(옛 포맷) 버킷은 제외한다.
    per_axis: Dict[str, Dict[str, int]] = {}
    all_months: set = set()
    for composite, count in (snapshot.get("group_counts") or {}).items():
        axis_value, _, month = str(composite).partition(sep)
        if len(month) != 7 or axis_value in {"미지정", "미상", "담당자 미지정"}:
            continue
        per_axis.setdefault(axis_value, {})[month] = per_axis.get(axis_value, {}).get(month, 0) + count
        all_months.add(month)

    if not per_axis or not all_months:
        return {
            "content": (
                "교차 집계할 등록월 데이터가 부족합니다. "
                "`scripts/make_vector_db.py --full`로 등록일을 채워 재구축하면 정확해집니다."
            ),
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_pivot", "status": "insufficient_history"}],
            },
            "raw": {"mode": "deterministic_mantis_pivot"},
        }

    ordered_months = sorted(all_months)
    shown_months = ordered_months[-PIVOT_MAX_MONTHS:]
    months_truncated = len(ordered_months) > len(shown_months)

    # 행은 표시 구간 내 합계가 큰 순으로 정렬해 상위 N개만 보인다.
    rows = []
    for axis_value, monthly in per_axis.items():
        row_total = sum(monthly.get(month, 0) for month in shown_months)
        if row_total == 0:
            continue
        rows.append((axis_value, monthly, row_total))
    rows.sort(key=lambda item: (item[2], item[0]), reverse=True)
    rows_truncated = len(rows) > PIVOT_MAX_ROWS
    rows = rows[:PIVOT_MAX_ROWS]

    header = f"| {axis_label} | " + " | ".join(shown_months) + " | 합계 |"
    divider = "| --- | " + " | ".join(["---"] * len(shown_months)) + " | --- |"
    table_lines = [header, divider]
    col_totals = {month: 0 for month in shown_months}
    grand_total = 0
    for axis_value, monthly, row_total in rows:
        cells = []
        for month in shown_months:
            value = monthly.get(month, 0)
            col_totals[month] += value
            cells.append(str(value))
        grand_total += row_total
        table_lines.append(f"| {axis_value} | " + " | ".join(cells) + f" | {row_total} |")
    total_cells = " | ".join(str(col_totals[month]) for month in shown_months)
    table_lines.append(f"| 합계 | {total_cells} | {grand_total} |")

    lines = [
        f"{axis_label}별 × 월별 이슈 건수 "
        f"(표시 {len(shown_months)}개월, 상위 {len(rows)}개 {axis_label})",
        f"기준 데이터: Mantis 인덱스 전체 {snapshot['scanned_count']}건",
        "",
        *table_lines,
    ]
    notes = []
    if months_truncated:
        notes.append(f"※ 최근 {PIVOT_MAX_MONTHS}개월만 표시했습니다(전체 {len(ordered_months)}개월).")
    if rows_truncated:
        notes.append(f"※ 합계 상위 {PIVOT_MAX_ROWS}개 {axis_label}만 표시했습니다.")
    if notes:
        lines.extend(["", *notes])

    retrieval = {
        "query": text,
        "constraints": constraints,
        "stages": [{
            "source": "mantis_pivot",
            "status": "ok",
            "meta": {
                "axis": axis_field,
                "shown_months": shown_months,
                "row_count": len(rows),
                "scanned_count": snapshot["scanned_count"],
            },
        }],
    }
    return {
        "content": "\n".join(lines),
        "sources": [],
        "retrieval": retrieval,
        "raw": {"mode": "deterministic_mantis_pivot"},
    }


def build_mantis_count_report(
    text: str,
    query_plan: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Execute a deterministic count plan instead of formatting a top-N list."""
    if query_plan.get("operation") != "count" or query_plan.get("source") not in {"mantis", "both"}:
        return None

    constraints = merge_query_plan_constraints(extract_query_constraints(text), query_plan)
    group_by = str(query_plan.get("group_by") or "")
    # "상태별 이슈 개수"처럼 다른 조건이 없어도 그룹 집계 자체가 요청이다.
    # 예전에는 여기서 빠져나가 Notion 검색으로 흘러갔다.
    if not has_constraint(constraints) and not count_group_key(group_by):
        return None

    # "최신 N건 기준"은 반드시 집계 범위를 제한한다. 등록일 조건이 함께 있어도
    # 전체 로컬 스냅샷을 합치면 N건 창 밖의 과거 이슈가 섞여 결과가 부풀려진다.
    # 라이브 API를 사용할 수 있을 때는 해당 최신 창을 직접 순회한 결과를 기준으로
    # 집계한다. 로컬 스냅샷은 라이브 조회 실패 시의 보조 수단으로만 남긴다.
    date_range = constraints.get("created_at") if isinstance(constraints.get("created_at"), dict) else None
    explicit_snapshot = bool(re.search(r"최신\s*\d+\s*건\s*기준", text))
    snapshot_limit = extract_snapshot_limit(text) if explicit_snapshot else None
    snapshot = scan_mantis_documents(
        CHROMA_DB_PATH,
        lambda content: chroma_text_matches_constraints(content, constraints),
        normalize_chroma_issue_document,
        example_limit=10,
        scan_limit=snapshot_limit,
        group_key=count_group_key(group_by),
        constraints=constraints,
    )
    if not snapshot["available"]:
        return {
            "content": f"Mantis 집계 도구를 사용할 수 없습니다.\n\n{snapshot['reason']}",
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "intent_plan": query_plan,
                "query_plan": query_plan,
                "stages": [{"source": "mantis_count", "status": "error", "reason": snapshot["reason"], "meta": {"operation": "count"}}],
            },
            "raw": {"mode": "deterministic_mantis_count"},
        }
    normalized_issues = snapshot["issues"]
    total_count = int(snapshot["matched_count"])
    scan_complete = not explicit_snapshot
    merged_live = False
    if date_range:
        # 스냅샷 등록일 누락으로 새는 건수를 라이브 결과로 보완한다. 스냅샷 매칭 ID와
        # 라이브 매칭 ID를 합집합해 중복 없이 정확한 총계를 낸다.
        # 질문에 "최신 N건 기준"이 있으면 그 창(페이지 수)을 그대로 따른다. 없으면
        # 기본 8페이지(=800건, 체커 기준과 동일)로 최근 구간을 커버한다.
        window_pages = 8
        window_match = re.search(r"최신\s*(\d+)\s*건\s*기준", text)
        if window_match:
            window_pages = max(1, min((int(window_match.group(1)) + 99) // 100, 30))
        live_map = collect_mantis_live_ids_in_range(constraints, max_pages=window_pages)
        if live_map is not None:
            if explicit_snapshot:
                # 최신 N건 창을 명시한 요청은 현재 API가 반환한 그 창만 사용한다.
                # 오래된 로컬 인덱스와 합집합하면 요청 범위를 벗어난 이슈가 다시
                # 포함되므로, 라이브 결과가 성공한 경우 이를 완전한 기준으로 본다.
                total_count = len(live_map)
                normalized_issues = sorted(
                    (normalize_issue(issue) for issue in live_map.values()),
                    key=lambda issue: int(issue.get("id") or 0),
                    reverse=True,
                )
            else:
                snapshot_ids = {str(i) for i in snapshot.get("matched_ids") or set()}
                union_ids = snapshot_ids | set(live_map.keys())
                total_count = len(union_ids)
                # 대표 이슈: 라이브에만 있는 건 중 최신 ID를 스냅샷 예시와 함께 노출한다.
                live_only = [live_map[i] for i in live_map if i not in snapshot_ids]
                live_only_norm = [normalize_issue(i) for i in live_only]
                combined = {int(i["id"]): i for i in normalized_issues}
                for i in live_only_norm:
                    combined.setdefault(int(i["id"]), i)
                normalized_issues = sorted(
                    combined.values(), key=lambda i: int(i.get("id") or 0), reverse=True
                )
            scan_complete = True
            merged_live = True
    criteria = format_issue_list_criteria(constraints, normalized_issues)
    if merged_live and explicit_snapshot:
        scope = f"Mantis 실시간 최신 {snapshot_limit}건 기준"
    elif merged_live:
        scope = "Mantis 스냅샷 + 실시간 병합 기준"
    elif explicit_snapshot:
        scope = f"최신 {snapshot_limit}건 기준"
    else:
        scope = f"Mantis 인덱스 전체 {snapshot['scanned_count']}건 기준"
    lines = [
        f"검색 기준: {criteria}, {scope}",
        "",
        f"총 {total_count}건입니다.",
    ]
    group_lines = format_count_group_lines(snapshot.get("group_counts") or {}, group_by)
    if group_lines:
        # 그룹 집계를 요청했으면 총계보다 그룹별 건수가 답의 본문이다.
        lines.extend(group_lines)
    if total_count:
        lines.extend(["", "대표 이슈"])
        for issue in normalized_issues[:5]:
            lines.append(
                f"- #{issue['id']} {issue['summary']} "
                f"({issue['project']} · {display_status_label(issue['status'])} · {issue['created_at'] or '미상'})"
            )
    representative_issues = normalized_issues[:10]
    retrieval = {
        "query": text,
        "keyword": "",
        "constraints": constraints,
        "intent_plan": query_plan,
        "query_plan": query_plan,
        "limit": len(representative_issues),
        "stages": [
            {
                "source": "mantis_count",
                "status": "ok",
                "meta": {
                    "operation": "count",
                    "snapshot_limit": snapshot_limit,
                    "scanned_count": snapshot["scanned_count"],
                    "scan_complete": scan_complete,
                    "total_count": total_count,
                    "displayed_count": len(representative_issues),
                    "applied_filters": constraints,
                    "tool": "mantis_chroma_snapshot",
                },
                "issues_list": representative_issues,
            }
        ],
    }
    sources = build_sources_from_issue_rows(collect_retrieved_issues(retrieval, 10))
    return {
        "content": "\n".join(lines),
        "sources": sources,
        "sourcesForFollowUps": sources,
        "retrieval": retrieval,
        "raw": {"mode": "deterministic_mantis_count"},
    }


def build_mantis_month_count_report(text: str) -> Optional[Dict[str, Any]]:
    month_query = extract_month_count_query(text)
    if not month_query:
        return None

    constraints = extract_query_constraints(text)
    query_plan = build_query_plan(
        text,
        {"source": "mantis", "planner": "rule", "confidence": 1.0},
        constraints,
    )
    target_month = f"{month_query['year']}-{month_query['month']:02d}"
    snapshot_limit = month_query["snapshot_limit"]
    recent_issues = fetch_mantis_recent_issues(snapshot_limit)
    matched_issues = [
        issue
        for issue in recent_issues
        if issue_created_year_month(issue) == target_month
        and issue_matches_constraints(issue, constraints)
    ]
    matched_issues.sort(key=lambda issue: int(issue.get("id") or 0), reverse=True)
    normalized_issues = [normalize_issue(issue) for issue in matched_issues]
    criteria = format_issue_list_criteria(constraints, normalized_issues)
    lines = [
        f"검색 기준: {criteria}, 기간={target_month}, 기준=최신 {snapshot_limit}건",
        "",
        f"총 {len(matched_issues)}건입니다.",
    ]
    if matched_issues:
        lines.extend(["", "대표 이슈"])
        for issue in normalized_issues[:5]:
            lines.append(
                f"- #{issue['id']} {issue['summary']} "
                f"({issue['project']} · {display_status_label(issue['status'])} · {issue['created_at'] or '미상'})"
            )

    retrieval = {
        "query": text,
        "keyword": "",
        "constraints": constraints,
        "intent_plan": query_plan,
        "query_plan": query_plan,
        "limit": min(len(normalized_issues), 10) or 5,
        "stages": [
            {
                "source": "mantis_month_count",
                "status": "ok",
                "meta": {
                    "snapshot_limit": snapshot_limit,
                    "year": month_query["year"],
                    "month": month_query["month"],
                    "total_count": len(matched_issues),
                    "displayed_count": min(len(normalized_issues), 10),
                },
                "issues_list": normalized_issues[:10],
            }
        ],
    }
    return {
        "content": "\n".join(lines),
        "sources": build_sources_from_issue_rows(collect_retrieved_issues(retrieval, 10)),
        "retrieval": retrieval,
        "raw": {"mode": "deterministic_mantis_month_count"},
    }

def format_issue_list_criteria(
    constraints: Dict[str, Any],
    issues: List[Dict[str, Any]],
) -> str:
    criteria = []
    if constraints.get("similar_base_issue_ids"):
        criteria.append(
            "기준 이슈="
            + ", ".join(f"#{issue_id}" for issue_id in constraints["similar_base_issue_ids"])
        )
    if constraints.get("customer_key"):
        project = issues[0].get("project") if issues else ""
        customer = project or constraints["customer_key"]
        criteria.append(f"고객사={customer}")
    if constraints.get("platform_key"):
        criteria.append(f"플랫폼={constraints['platform_key'].upper()}")
    if constraints.get("excluded_platform_keys"):
        criteria.append(
            "플랫폼 제외=" + ", ".join(value.upper() for value in constraints["excluded_platform_keys"])
        )
    if constraints.get("excluded_customer_keys"):
        criteria.append("고객사 제외=" + ", ".join(constraints["excluded_customer_keys"]))
    if constraints.get("people", {}).get("names"):
        role = constraints.get("people", {}).get("role") or "any"
        role_label = "담당자" if role == "handler" else "요청자/본문" if role == "reporter" else "인물"
        criteria.append(f"{role_label}={', '.join(constraints['people']['names'])}")
    elif constraints.get("unassigned_people"):
        criteria.append("담당자=미지정")
    if constraints.get("statuses"):
        criteria.append(f"상태={', '.join(constraints['statuses'])}")
    if constraints.get("excluded_statuses"):
        criteria.append(f"상태 제외={', '.join(constraints['excluded_statuses'])}")
    if constraints.get("screen_numbers"):
        criteria.append(f"화면번호={', '.join(constraints['screen_numbers'])}")
    if constraints.get("issue_ids"):
        criteria.append(f"이슈 ID={', '.join(str(issue_id) for issue_id in constraints['issue_ids'])}")
    for field_name, field_value in constraints.get("date_fields", {}).items():
        criteria.append(f"{field_name}={field_value}")
    created_at = constraints.get("created_at") or {}
    if created_at.get("from") and created_at.get("to"):
        criteria.append(f"등록일={created_at['from']}~{created_at['to']}")
    for clause in normalize_filter_clauses(constraints.get("filter_clauses") or []):
        if clause["field"] in {"title", "keyword"}:
            criteria.append(filter_clause_label(clause))
    return ", ".join(criteria) or "요청 조건"


def display_status_label(status: str) -> str:
    normalized = str(status or "").strip()
    status_labels = {
        "open": "미종료",
        "closed": "종료된 이슈",
        "confirmed": "확인된 이슈",
        "assigned": "할당된 이슈",
        "acknowledged": "이슈 검토",
        "resolved": "해결된 이슈",
        "feedback": "정보 부족",
        "developed": "개발완료",
        "new": "신규",
    }
    return status_labels.get(normalized.casefold(), normalized or "상태 미상")
