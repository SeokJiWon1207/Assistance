"""월별 이슈 유형 추이 리포트.

멘티스의 ``severity`` 필드가 실제로는 이슈 유형(오류/개선/문의 등)을 담고 있어
그 라벨을 시리즈로 삼아 월별 처리건수와 처리공수를 함께 집계한다.

데이터 출처는 로컬 구조화 인덱스다. 멘티스 REST API는 페이지당 응답이 매우 느려
(측정값 기준 100건당 수십 초 이상) 20개월 범위를 실시간으로 훑는 것은 대화형
응답에 쓸 수 없다. 인덱스는 이미 등록일 기준으로 정렬돼 있어 즉시 집계된다.
"""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import MANTIS_INDEX_DB_PATH
from backend.intent.matching import chroma_text_matches_constraints
from backend.reports.effort import EFFORT_FIELD_LABELS, parse_effort_field_value
from backend.understanding import extract_query_constraints

# 차트에 그릴 이슈 유형. 멘티스 severity 8종 가운데 실제 처리 업무에 해당하는
# 5종만 사용한다. 제외/중복·기각·정리/기타는 집계상 의미가 없어 제외한다.
TREND_ISSUE_TYPES = ("오류", "개선", "문의", "검토/보류", "추가")

DEFAULT_TREND_MONTHS = 12
MAX_TREND_MONTHS = 36

_EFFORT_SECTION_PATTERN = re.compile(r"\[공수\]\n(.*?)(?=\n\[|\Z)", re.S)
_EFFORT_LINE_PATTERN = re.compile(r"^-\s*(?P<field>[^:]+):\s*(?P<value>.*)$")

# 규칙 경로는 오탐이 없는 표현만 잡는다. 애매한 표현은 LLM 플래너가 판정한다.
# '이슈'만으로 매칭하면 "월별 회의 이슈 정리해줘" 같은 질문까지 차트로 빠진다.
_TREND_KEYWORDS = ("월별", "월간", "달별", "추이", "트렌드", "추세")
_TREND_SUBJECTS = ("처리", "공수", "건수", "유형", "통계", "집계", "현황")
# 1인칭·지시어는 이 리포트가 해석할 수 없다. 조건 없이 전사로 답하면
# 사용자는 자기 실적으로 오해하므로, 기존 공수 리포트에 넘긴다.
_UNRESOLVED_SCOPE_HINTS = ("내 ", "내가", "제 ", "나의", "본인", "우리", "그중", "거기서", "저건", "방금", "위에서")
# 플래너가 trend 로 분류해도 이 주제가 섞여 있으면 멘티스 이슈 집계가 아니다.
# 플래너 출력은 실행 전에 결정형 규칙으로 한 번 더 검증한다
# (build_effort_report_from_plan 이 인물 이름을 검증하는 것과 같은 방식).
_NON_ISSUE_SUBJECTS = ("회의", "회의록", "공지", "결재", "휴가", "회식", "일정", "계약", "출장", "교육")


def has_unresolved_scope(text: str) -> bool:
    """이 리포트가 해석할 수 없는 범위 표현이 있는지 본다."""
    return any(hint in str(text or "") for hint in _UNRESOLVED_SCOPE_HINTS)


def mentions_non_issue_subject(text: str) -> bool:
    """멘티스 이슈가 아닌 주제(회의·공지 등)를 묻는지 본다."""
    return any(subject in str(text or "") for subject in _NON_ISSUE_SUBJECTS)


def wants_effort_trend(text: str) -> bool:
    """월별 유형 추이 질문인지 판별한다."""
    if not text:
        return False
    lowered = str(text)
    if not any(keyword in lowered for keyword in _TREND_KEYWORDS):
        return False
    if not any(subject in lowered for subject in _TREND_SUBJECTS):
        return False
    # 회의·공지처럼 이슈 집계가 아닌 주제는 대상 어휘가 겹쳐도 넘긴다.
    if mentions_non_issue_subject(lowered):
        return False
    # "내 월별 공수 추이"처럼 화자를 가리키는 질문은 담당자 해석이 필요하다.
    return not has_unresolved_scope(lowered)


def parse_trend_year(text: str) -> Optional[int]:
    """'2025년 월별 추이'처럼 연도를 명시하면 그 해로 구간을 고정한다."""
    match = re.search(r"(20\d{2})\s*년", str(text or ""))
    return int(match.group(1)) if match else None


def parse_trend_months(text: str) -> int:
    """'최근 20개월' 같은 표현에서 기간을 읽는다. 없으면 기본값을 쓴다."""
    match = re.search(r"(\d{1,2})\s*개월", str(text or ""))
    if not match:
        return DEFAULT_TREND_MONTHS
    months = int(match.group(1))
    return max(1, min(months, MAX_TREND_MONTHS))


def _month_sequence(
    months: int,
    today: Optional[date] = None,
    end_year: Optional[int] = None,
    end_month: Optional[int] = None,
) -> List[str]:
    """끝 달에서 거꾸로 세어 'YYYYMM' 목록을 만든다. 기본 끝점은 이번 달이다."""
    current = today or date.today()
    year = end_year if end_year is not None else current.year
    month = end_month if end_month is not None else current.month
    periods: List[str] = []
    for _ in range(months):
        periods.append(f"{year:04d}{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(periods))


def resolve_trend_window(text: str, today: Optional[date] = None) -> tuple[int, Optional[int], Optional[int]]:
    """질문에서 (개월 수, 끝 연도, 끝 월)을 정한다.

    연도만 밝히고 개월 수를 안 적으면 그 해 전체(1~12월)를 본다. 이 경우
    오늘 기준 최근 12개월로 잡으면 사용자가 지정한 해의 앞부분이 잘려 나간다.
    """
    year = parse_trend_year(text)
    has_explicit_months = bool(re.search(r"\d{1,2}\s*개월", str(text or "")))
    months = parse_trend_months(text)
    if year is None:
        return months, None, None
    current = today or date.today()
    if has_explicit_months:
        # 연도와 개월 수를 함께 적었으면 그 해 끝을 기준으로 거꾸로 센다.
        end_month = 12 if year < current.year else current.month
        return months, year, end_month
    if year < current.year:
        return 12, year, 12
    # 올해를 지정하면 아직 오지 않은 달까지 그리지 않는다.
    return current.month, year, current.month


def extract_document_effort(document: str) -> float:
    """인덱스에 저장된 문서 본문의 [공수] 구간에서 총 공수를 뽑는다."""
    section = _EFFORT_SECTION_PATTERN.search(document or "")
    if not section:
        return 0.0
    total = 0.0
    for line in section.group(1).splitlines():
        matched = _EFFORT_LINE_PATTERN.match(line.strip())
        if not matched:
            continue
        if matched.group("field").strip() not in EFFORT_FIELD_LABELS:
            continue
        for _person, amount in parse_effort_field_value(matched.group("value")):
            total += amount
    return round(total, 4)


def scope_label(constraints: Dict[str, Any]) -> str:
    """집계 범위를 사람이 읽는 문구로 만든다. 조건이 없으면 전사다."""
    parts: List[str] = []
    if constraints.get("customer_key"):
        parts.append(str(constraints["customer_key"]))
    if constraints.get("platform_key"):
        parts.append(str(constraints["platform_key"]).upper())
    names = (constraints.get("people") or {}).get("names") or []
    if names:
        parts.append(" · ".join(str(name) for name in names))
    return " · ".join(parts) if parts else "전사"


def has_scope_constraints(constraints: Dict[str, Any]) -> bool:
    if constraints.get("customer_key") or constraints.get("platform_key"):
        return True
    if (constraints.get("people") or {}).get("names"):
        return True
    return bool(constraints.get("statuses") or constraints.get("screen_numbers"))


def run_effort_trend_report(
    text: str,
    months: int = DEFAULT_TREND_MONTHS,
    index_path: Optional[Path] = None,
    today: Optional[date] = None,
    constraints: Optional[Dict[str, Any]] = None,
    end_year: Optional[int] = None,
    end_month: Optional[int] = None,
) -> Dict[str, Any]:
    """월별 × 이슈 유형으로 건수와 공수를 집계한다.

    고객사·매체·담당자 조건이 있으면 그 범위로 좁혀서 집계한다. 조건을 무시하고
    전사 합계를 돌려주면 사용자는 자기가 물어본 범위의 답으로 오해하게 된다.
    """
    active_constraints = constraints if constraints is not None else {}
    periods = _month_sequence(months, today=today, end_year=end_year, end_month=end_month)
    start_date = f"{periods[0][:4]}-{periods[0][4:]}-01"

    counts: Dict[str, Dict[str, int]] = {
        issue_type: {period: 0 for period in periods} for issue_type in TREND_ISSUE_TYPES
    }
    efforts: Dict[str, Dict[str, float]] = {
        issue_type: {period: 0.0 for period in periods} for issue_type in TREND_ISSUE_TYPES
    }
    scanned = 0
    matched = 0
    typed = 0
    untyped = 0

    database_path = Path(index_path) if index_path else MANTIS_INDEX_DB_PATH
    if not database_path.exists():
        return _empty_trend(text, periods, reason="구조화 인덱스가 아직 생성되지 않았습니다.")

    try:
        with closing(sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)) as connection:
            rows = connection.execute(
                """
                SELECT created_at, issue_type, document
                FROM issues
                WHERE created_at >= ?
                """,
                (start_date,),
            ).fetchall()
    except sqlite3.Error as exc:
        return _empty_trend(text, periods, reason=f"구조화 인덱스를 읽지 못했습니다: {exc}")

    for created_at, issue_type, document in rows:
        scanned += 1
        period = str(created_at or "")[:7].replace("-", "")
        if period not in counts.get(TREND_ISSUE_TYPES[0], {}):
            continue
        if active_constraints and not chroma_text_matches_constraints(document, active_constraints):
            continue
        matched += 1
        label = str(issue_type or "").strip()
        if not label:
            # 이슈 유형이 아직 인덱스에 반영되지 않은 과거 문서.
            untyped += 1
            continue
        if label not in counts:
            continue
        typed += 1
        counts[label][period] += 1
        efforts[label][period] += extract_document_effort(document)

    series = [
        {
            "label": issue_type,
            "counts": [counts[issue_type][period] for period in periods],
            "efforts": [round(efforts[issue_type][period], 2) for period in periods],
        }
        for issue_type in TREND_ISSUE_TYPES
    ]

    trend_chart = {
        "periods": periods,
        "series": series,
        "totalIssues": typed,
        "monthCount": len(periods),
        "scopeLabel": scope_label(active_constraints),
        "untypedCount": untyped,
        "periodLabel": f"{periods[0][:4]}년 {int(periods[0][4:])}월 ~ {periods[-1][:4]}년 {int(periods[-1][4:])}월",
    }

    lines = _summary_lines(trend_chart, series, periods, typed, matched, untyped)
    return {
        "content": "\n".join(lines),
        "sources": [],
        "effortTrend": trend_chart,
        "raw": {"mode": "deterministic_effort_trend"},
    }


# 인덱스에 이슈 유형이 없는 문서 비율이 이 값을 넘으면 추세가 왜곡된 것으로 본다.
UNTYPED_WARNING_RATIO = 0.05


def _rebuild_notice() -> List[str]:
    return [
        "이슈 유형은 전체 재구축을 해야 과거 이슈까지 채워집니다.",
        "기본 실행(`python3 scripts/make_vector_db.py`)은 증분이라 신규 이슈만 반영됩니다.",
        "`python3 scripts/make_vector_db.py --full` 로 재구축한 뒤 다시 시도해 주세요.",
    ]


def _summary_lines(
    trend_chart: Dict[str, Any],
    series: List[Dict[str, Any]],
    periods: List[str],
    typed: int,
    matched: int,
    untyped: int,
) -> List[str]:
    scope = trend_chart.get("scopeLabel") or "전사"
    if not typed:
        # 조건에 맞는 이슈 자체가 없는 정상적인 빈 결과와,
        # 인덱스에 이슈 유형이 없어 못 그리는 경우를 구분해서 안내한다.
        if not matched:
            return [
                f"{trend_chart['periodLabel']} {scope} 구간에 해당하는 이슈가 없습니다.",
                "",
                "기간이나 조건을 바꿔서 다시 물어봐 주세요.",
            ]
        return [
            f"{trend_chart['periodLabel']} {scope} 구간의 {matched}건 모두 이슈 유형이 인덱스에 없습니다.",
            "",
            *_rebuild_notice(),
        ]

    lines = [
        f"{trend_chart['periodLabel']} {scope} 월별 이슈 유형 추이입니다.",
        "",
        f"- 집계 범위: {scope}",
        f"- 집계 기간: {len(periods)}개월",
        f"- 유형이 기록된 이슈: **{typed}건** (범위 내 {matched}건)",
        "- 기준: Mantis 이슈 **등록일** (처리·완료 시점 아님) · 이슈 유형(severity)",
    ]

    # 일부만 반영된 인덱스는 과거 달이 0으로 보여 없는 상승세를 만들어낸다.
    if matched and untyped / matched > UNTYPED_WARNING_RATIO:
        percent = untyped / matched * 100
        lines.extend([
            "",
            f"⚠️ 범위 내 {untyped}건({percent:.0f}%)은 이슈 유형이 인덱스에 없어 차트에서 빠졌습니다.",
            "과거 구간이 실제보다 낮게 보일 수 있습니다.",
            *_rebuild_notice(),
        ])

    lines.extend(["", "유형별 합계"])
    for item in series:
        total_count = sum(item["counts"])
        total_effort = sum(item["efforts"])
        lines.append(f"- {item['label']}: 등록 **{total_count}건** · 공수 **{total_effort:.2f}**")
    return lines


def _empty_trend(text: str, periods: List[str], reason: str) -> Dict[str, Any]:
    return {
        "content": f"월별 이슈 유형 추이를 만들지 못했습니다. {reason}",
        "sources": [],
        "effortTrend": None,
        "raw": {"mode": "deterministic_effort_trend", "error": reason},
    }


def build_effort_trend_report_from_plan(
    text: str,
    model_plan: Dict[str, Any],
    constraints: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """규칙 경로가 못 잡은 추이 질문을 LLM 플래너의 effort 분류로 실행한다.

    플래너는 "추이 질문인가"만 판정하고, 기간 해석과 실제 집계는 전부 기존
    결정형 코어가 수행한다. 플래너가 기간을 지어내도 집계에 영향이 없다.
    """
    effort = model_plan.get("effort") if isinstance(model_plan.get("effort"), dict) else None
    if not effort:
        return None
    if str(effort.get("type", "")).strip().lower() != "trend":
        return None
    # 화자·지시어가 섞인 질문은 담당자 해석이 필요하므로 여기서도 넘기지 않는다.
    if has_unresolved_scope(text):
        return None
    # 플래너가 trend 라고 해도 회의록·공지 같은 주제면 이슈 집계가 아니다.
    if mentions_non_issue_subject(text):
        return None
    months, end_year, end_month = resolve_trend_window(text)
    return run_effort_trend_report(
        text,
        months=months,
        constraints=constraints if constraints is not None else extract_query_constraints(text),
        end_year=end_year,
        end_month=end_month,
    )


def build_effort_trend_report(text: str) -> Optional[Dict[str, Any]]:
    """질문이 월별 유형 추이를 요구하면 리포트를 만든다.

    고객사·매체·담당자 조건은 그대로 집계에 반영한다.
    """
    if not wants_effort_trend(text):
        return None
    months, end_year, end_month = resolve_trend_window(text)
    return run_effort_trend_report(
        text,
        months=months,
        constraints=extract_query_constraints(text),
        end_year=end_year,
        end_month=end_month,
    )
