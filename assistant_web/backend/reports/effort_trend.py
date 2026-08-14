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
from backend.reports.effort import EFFORT_FIELD_LABELS, parse_effort_field_value

# 차트에 그릴 이슈 유형. 멘티스 severity 8종 가운데 실제 처리 업무에 해당하는
# 5종만 사용한다. 제외/중복·기각·정리/기타는 집계상 의미가 없어 제외한다.
TREND_ISSUE_TYPES = ("오류", "개선", "문의", "검토/보류", "추가")

DEFAULT_TREND_MONTHS = 12
MAX_TREND_MONTHS = 36

_EFFORT_SECTION_PATTERN = re.compile(r"\[공수\]\n(.*?)(?=\n\[|\Z)", re.S)
_EFFORT_LINE_PATTERN = re.compile(r"^-\s*(?P<field>[^:]+):\s*(?P<value>.*)$")

_TREND_KEYWORDS = ("월별", "추이", "트렌드", "추세", "월간 추이")
_TREND_SUBJECTS = ("처리", "공수", "건수", "이슈", "유형")


def wants_effort_trend(text: str) -> bool:
    """월별 유형 추이 질문인지 판별한다."""
    if not text:
        return False
    lowered = str(text)
    if not any(keyword in lowered for keyword in _TREND_KEYWORDS):
        return False
    return any(subject in lowered for subject in _TREND_SUBJECTS)


def parse_trend_months(text: str) -> int:
    """'최근 20개월' 같은 표현에서 기간을 읽는다. 없으면 기본값을 쓴다."""
    match = re.search(r"(\d{1,2})\s*개월", str(text or ""))
    if not match:
        return DEFAULT_TREND_MONTHS
    months = int(match.group(1))
    return max(1, min(months, MAX_TREND_MONTHS))


def _month_sequence(months: int, today: Optional[date] = None) -> List[str]:
    """가장 오래된 달부터 이번 달까지 'YYYYMM' 목록을 만든다."""
    current = today or date.today()
    periods: List[str] = []
    year, month = current.year, current.month
    for _ in range(months):
        periods.append(f"{year:04d}{month:02d}")
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(periods))


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


def run_effort_trend_report(
    text: str,
    months: int = DEFAULT_TREND_MONTHS,
    index_path: Optional[Path] = None,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """월별 × 이슈 유형으로 건수와 공수를 집계한다."""
    periods = _month_sequence(months, today=today)
    start_date = f"{periods[0][:4]}-{periods[0][4:]}-01"

    counts: Dict[str, Dict[str, int]] = {
        issue_type: {period: 0 for period in periods} for issue_type in TREND_ISSUE_TYPES
    }
    efforts: Dict[str, Dict[str, float]] = {
        issue_type: {period: 0.0 for period in periods} for issue_type in TREND_ISSUE_TYPES
    }
    scanned = 0
    typed = 0

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
        label = str(issue_type or "").strip()
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
        "periodLabel": f"{periods[0][:4]}년 {int(periods[0][4:])}월 ~ {periods[-1][:4]}년 {int(periods[-1][4:])}월",
    }

    lines = _summary_lines(trend_chart, series, periods, typed, scanned)
    return {
        "content": "\n".join(lines),
        "sources": [],
        "effortTrend": trend_chart,
        "raw": {"mode": "deterministic_effort_trend"},
    }


def _summary_lines(
    trend_chart: Dict[str, Any],
    series: List[Dict[str, Any]],
    periods: List[str],
    typed: int,
    scanned: int,
) -> List[str]:
    if not typed:
        return [
            f"{trend_chart['periodLabel']} 구간에서 이슈 유형이 기록된 이슈를 찾지 못했습니다.",
            "",
            "구조화 인덱스에 이슈 유형이 아직 반영되지 않았을 수 있습니다.",
            "`python3 scripts/make_vector_db.py`로 인덱스를 갱신한 뒤 다시 시도해 주세요.",
        ]

    lines = [
        f"{trend_chart['periodLabel']} 월별 이슈 유형 추이입니다.",
        "",
        f"- 집계 기간: {len(periods)}개월",
        f"- 유형이 기록된 이슈: **{typed}건** (조회 {scanned}건)",
        "- 기준: Mantis 이슈 등록일 · 이슈 유형(severity)",
        "",
        "유형별 합계",
    ]
    for item in series:
        total_count = sum(item["counts"])
        total_effort = sum(item["efforts"])
        lines.append(f"- {item['label']}: **{total_count}건** · 공수 **{total_effort:.2f}**")
    return lines


def _empty_trend(text: str, periods: List[str], reason: str) -> Dict[str, Any]:
    return {
        "content": f"월별 이슈 유형 추이를 만들지 못했습니다. {reason}",
        "sources": [],
        "effortTrend": None,
        "raw": {"mode": "deterministic_effort_trend", "error": reason},
    }


def build_effort_trend_report(text: str) -> Optional[Dict[str, Any]]:
    """질문이 월별 유형 추이를 요구하면 리포트를 만든다."""
    if not wants_effort_trend(text):
        return None
    return run_effort_trend_report(text, months=parse_trend_months(text))
