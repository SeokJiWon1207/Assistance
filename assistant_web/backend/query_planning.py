"""Structured query planning helpers for tool-backed chat requests.

The language model may enrich a plan, but these helpers keep critical intent
such as date ranges and count operations recoverable and independently
verifiable when the planner is unavailable or returns an incomplete payload.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from backend.query_filters import (
    NEGATIVE_OPERATORS,
    add_filter_clause,
    normalize_filter_clauses,
)


COUNT_MARKERS = ("몇개", "몇건", "건수", "총몇", "개수")
SUM_MARKERS = ("합계", "총합", "합이", "합은", "더하면", "공수합")
SIMILAR_MARKERS = ("비슷", "유사", "같은", "동일", "관련")


GROUP_BY_PATTERNS = {
    "customer": re.compile(r"(?:고객사|프로젝트)\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
    "platform": re.compile(r"(?:플랫폼|매체(?:구분)?)\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
    "owner": re.compile(r"(?:담당자|담당)\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
    "status": re.compile(r"상태\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
    "type": re.compile(r"(?:타입|유형|분류)\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
    # "월별 건수 추이"처럼 기간축으로 묶는 요청. 고객사/담당자 같은 다른 축이
    # 함께 있으면 그쪽이 먼저 잡히도록 마지막에 둔다.
    "month": re.compile(r"(?:월|달)\s*(?:별(?:로)?|기준(?:으)?로|마다)"),
}


def normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).lower()


def infer_operation(question: str) -> str:
    normalized = normalized_text(question)
    if "공수" in normalized and any(marker in normalized for marker in ("알고", "얼마", "집계", "합", "총")):
        return "aggregate"
    if any(marker in normalized for marker in SUM_MARKERS):
        return "aggregate"
    if any(marker in normalized for marker in COUNT_MARKERS):
        return "count"
    if extract_group_by(question) and "집계" in normalized:
        return "count"
    issue_reference = re.search(r"(?<!\d)(?:이슈\s*)?#?0*\d{5,7}(?!\d)(?:\b|(?=[의은는을를과와]))", question, re.IGNORECASE)
    if issue_reference and any(
        marker in normalized for marker in SIMILAR_MARKERS
    ):
        return "search"
    if issue_reference:
        return "get"
    return "search"


def extract_group_by(question: str) -> Optional[str]:
    """Return a deterministic presentation grouping requested by the user.

    Grouping phrases such as ``담당자별로`` describe how to organize results;
    they must never be interpreted as an owner/person filter.
    """
    for field, pattern in GROUP_BY_PATTERNS.items():
        if pattern.search(str(question or "")):
            return field
    return None


def has_count_intent(question: str) -> bool:
    return infer_operation(question) == "count"


def _safe_date(year: int, month: int, day: int) -> Optional[date]:
    """존재하지 않는 날짜(2월 30일 등)면 None을 돌려준다."""
    try:
        return date(year, month, day)
    except ValueError:
        return None


# ``2026-03-01`` / ``2026.3.1`` / ``2026/03/01``을 연·월·일 세 그룹으로 잡는다.
_ISO_DATE_PATTERN = r"(20\d{2})[-./](1[0-2]|0?[1-9])[-./](3[01]|[12]\d|0?[1-9])"


def has_invalid_month_expression(question: str) -> bool:
    """"2026년 13월"처럼 존재하지 않는 월을 가리키는 표현인지 판별한다."""
    return bool(re.search(r"(?<!\d)(1[3-9]|[2-9]\d)\s*월", str(question or "")))


def extract_created_at_range(question: str) -> Optional[Dict[str, str]]:
    """Extract a registration-date range from common Korean date expressions."""
    # 존재하지 않는 월은 어떤 규칙으로도 해석하지 않는다. 예전에는 월 규칙이
    # 빗나간 뒤 연도 규칙에 걸려 2026년 전체로 조용히 바뀌었는데, 잘못된
    # 조건을 그럴듯한 다른 범위로 대체하면 사용자가 결과를 오해하게 된다.
    if has_invalid_month_expression(question):
        return None

    # ``2026-03-01 ~ 2026-07-10``처럼 ISO 표기로 적은 일 단위 구간. 한글 날짜
    # 표현만 처리하던 때에는 이 형식이 어떤 규칙에도 걸리지 않아, 기간 조건이
    # 조용히 사라지고 연 단위 집계로 넓어졌다.
    iso_range_match = re.search(
        rf"{_ISO_DATE_PATTERN}\s*(?:부터|~|～|-|–|—)\s*{_ISO_DATE_PATTERN}\s*(?:까지)?",
        question,
    )
    if iso_range_match:
        start = _safe_date(*(int(value) for value in iso_range_match.group(1, 2, 3)))
        end = _safe_date(*(int(value) for value in iso_range_match.group(4, 5, 6)))
        if start and end and start <= end:
            return {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "precision": "day_range",
            }

    # 일 단위까지 명시한 구간은 월 단위 규칙보다 먼저 처리한다. 그렇지 않으면
    # "2025년 12월 31일부터 2026년 1월 2일까지"가 아래 월 규칙에 걸려
    # 12월 한 달로 뭉개진다.
    day_range_match = re.search(
        r"(20\d{2})\s*년\s*(1[0-2]|0?[1-9])\s*월\s*(3[01]|[12]\d|0?[1-9])\s*일\s*"
        r"(?:부터|~|～|-)\s*"
        r"(?:(20\d{2})\s*년\s*)?(?:(1[0-2]|0?[1-9])\s*월\s*)?"
        r"(3[01]|[12]\d|0?[1-9])\s*일\s*(?:까지)?",
        question,
    )
    if day_range_match:
        start = _safe_date(
            int(day_range_match.group(1)),
            int(day_range_match.group(2)),
            int(day_range_match.group(3)),
        )
        end = _safe_date(
            int(day_range_match.group(4) or day_range_match.group(1)),
            int(day_range_match.group(5) or day_range_match.group(2)),
            int(day_range_match.group(6)),
        )
        if start and end and start <= end:
            return {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "precision": "day_range",
            }

    # ``올해 3월 1일부터 7월 10일까지`` 또는 ``3월 1일~7월 10일``처럼
    # 시작 연도를 숫자로 쓰지 않은 일 단위 범위. 공수 집계뿐 아니라 일반
    # Mantis 기간 조회에서도 같은 해석을 공유한다.
    relative_day_range_match = re.search(
        r"(?:(올해|금년|이번\s*해|이번\s*년도|작년|전년|지난\s*해|지난\s*년도)\s*)?"
        r"(1[0-2]|0?[1-9])\s*월\s*(3[01]|[12]\d|0?[1-9])\s*일\s*"
        r"(?:부터|~|～|-)\s*"
        r"(?:(20\d{2})\s*년\s*)?"
        r"(1[0-2]|0?[1-9])\s*월\s*(3[01]|[12]\d|0?[1-9])\s*일\s*(?:까지)?",
        question,
    )
    if relative_day_range_match:
        base_year = date.today().year
        relative_year = str(relative_day_range_match.group(1) or "")
        if re.search(r"작년|전년|지난\s*해|지난\s*년도", relative_year):
            base_year -= 1
        end_year = int(relative_day_range_match.group(4) or base_year)
        start = _safe_date(
            base_year,
            int(relative_day_range_match.group(2)),
            int(relative_day_range_match.group(3)),
        )
        end = _safe_date(
            end_year,
            int(relative_day_range_match.group(5)),
            int(relative_day_range_match.group(6)),
        )
        if start and end and start <= end:
            return {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "precision": "day_range",
            }

    range_match = re.search(
        r"(20\d{2})\s*년?\s*(1[0-2]|0?[1-9])\s*월?\s*"
        r"(?:부터|~|～|-)\s*(?:(20\d{2})\s*년?\s*)?"
        r"(1[0-2]|0?[1-9])\s*월?(?:까지)?",
        question,
    )
    if range_match:
        start_year = int(range_match.group(1))
        start_month = int(range_match.group(2))
        end_year = int(range_match.group(3) or start_year)
        end_month = int(range_match.group(4))
        end_day = calendar.monthrange(end_year, end_month)[1]
        return {
            "from": f"{start_year:04d}-{start_month:02d}-01",
            "to": f"{end_year:04d}-{end_month:02d}-{end_day:02d}",
            "precision": "month_range",
        }

    month_match = re.search(r"(20\d{2})\s*년\s*(1[0-2]|0?[1-9])\s*월", question)
    if month_match:
        year = int(month_match.group(1))
        month = int(month_match.group(2))
        last_day = calendar.monthrange(year, month)[1]
        return {
            "from": f"{year:04d}-{month:02d}-01",
            "to": f"{year:04d}-{month:02d}-{last_day:02d}",
            "precision": "month",
        }

    # 연도를 생략한 "9월 등록된 항목"도 현재 연도의 해당 월로 해석한다.
    # 날짜와 무관한 숫자까지 잡지 않도록 월 표기와 시간 의미 동사가 함께
    # 있는 경우에만 적용한다.
    relative_month = extract_yearless_month_range(question)
    if relative_month and re.search(
        r"등록일|접수일|생성일|작성일|등록(?:된|한|했|되)|접수(?:된|한|했|되)|"
        r"생성(?:된|한|했|되)|작성(?:된|한|했|되)|만든", question,
    ):
        return relative_month

    year_match = re.search(r"(?<!\d)(20\d{2})\s*년", question)
    if year_match:
        year = int(year_match.group(1))
        return {
            "from": f"{year:04d}-01-01",
            "to": f"{year:04d}-12-31",
            "precision": "year",
        }

    relative_year_range = extract_relative_year_range(question)
    if relative_year_range:
        return relative_year_range

    relative = extract_relative_created_at_range(question)
    if relative:
        return relative
    return None


def extract_yearless_month_range(question: str, today: Optional[date] = None) -> Optional[Dict[str, str]]:
    """연도를 생략한 월 표현을 날짜 필드 의미와 분리해 해석한다."""
    match = re.search(r"(?<!\d)(1[0-2]|0?[1-9])\s*월(?!\s*\d)", question)
    if not match:
        return None
    year = (today or date.today()).year
    month = int(match.group(1))
    last_day = calendar.monthrange(year, month)[1]
    return {
        "from": f"{year:04d}-{month:02d}-01",
        "to": f"{year:04d}-{month:02d}-{last_day:02d}",
        "precision": "month",
    }


def extract_yearless_temporal_month_range(
    question: str, temporal_field: str, today: Optional[date] = None,
) -> Optional[Dict[str, str]]:
    """날짜 필드와 연도 없는 월이 직접 연결된 경우에만 기간으로 해석한다."""
    aliases = {
        "due_at": r"(?:마감일?|기한)",
        "deployed_at": r"(?:운영\s*반영일?|배포일)",
    }.get(temporal_field)
    if not aliases:
        return None
    month = r"(?<!\d)(?:1[0-2]|0?[1-9])\s*월(?!\s*\d)"
    particle = r"(?:이|가|은|는|을|를|의|에|:)?"
    boundary = rf"(?:{month}\s*(?:까지|에)?\s*{aliases}|{aliases}\s*{particle}\s*{month}(?:까지|에)?)"
    if not re.search(boundary, question):
        return None
    return extract_yearless_month_range(question, today=today)


def extract_relative_year_range(question: str, today: Optional[date] = None) -> Optional[Dict[str, str]]:
    base = today or date.today()
    if re.search(r"올해|금년|이번\s*해|이번\s*년도", question):
        year = base.year
    elif re.search(r"작년|전년|지난\s*해|지난\s*년도", question):
        year = base.year - 1
    else:
        return None
    return {
        "from": f"{year:04d}-01-01",
        "to": f"{year:04d}-12-31",
        "precision": "year",
    }


_AGING_SUFFIX = r"(?:\s*(?:이상|넘은|넘게|지난|경과|된|이나\s*된|묵은))"
_KOREAN_SMALL_NUMBERS = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10}


def _months_before(base: date, months: int) -> date:
    year, month = base.year, base.month - months
    while month <= 0:
        month += 12
        year -= 1
    day = min(base.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _extract_aging_created_at_range(question: str, base: date) -> Optional[Dict[str, str]]:
    """'한 달 넘은', '30일 지난'처럼 오래된 건만 찾는 표현을 구간으로 바꾼다."""
    if match := re.search(rf"(\d{{1,4}})\s*일{_AGING_SUFFIX}", question):
        cutoff = base - timedelta(days=int(match.group(1)))
    elif match := re.search(rf"(\d{{1,3}})\s*(?:개월|달){_AGING_SUFFIX}", question):
        cutoff = _months_before(base, max(int(match.group(1)), 1))
    elif match := re.search(rf"(\d{{1,2}})\s*년{_AGING_SUFFIX}", question):
        cutoff = _months_before(base, max(int(match.group(1)), 1) * 12)
    elif match := re.search(rf"([가-힣]{{1,2}})\s*(?:개월|달){_AGING_SUFFIX}", question):
        months = _KOREAN_SMALL_NUMBERS.get(match.group(1))
        if not months:
            return None
        cutoff = _months_before(base, months)
    else:
        return None
    return {"from": "", "to": cutoff.isoformat(), "precision": "older_than"}


def extract_relative_created_at_range(question: str, today: Optional[date] = None) -> Optional[Dict[str, str]]:
    """'오늘/어제/이번 주/지난주/최근 N일' 같은 상대 표현을 실제 날짜 범위로 바꾼다.

    today는 테스트에서 기준일을 고정할 때만 넘기고, 실제 요청 처리에서는
    항상 date.today()를 쓴다(자정을 넘기면 "오늘"의 의미도 바뀌어야 하므로)."""
    base = today or date.today()

    # "N일/개월 넘은", "한 달 지난"처럼 오래된 건만 찾는 표현.
    # 등록일이 기준일보다 이전인 것만 남기므로 상한만 정한다.
    if aging := _extract_aging_created_at_range(question, base):
        return aging

    if match := re.search(r"최근\s*(\d{1,3})\s*일", question):
        days = int(match.group(1))
        start = base - timedelta(days=max(days - 1, 0))
        return {"from": start.isoformat(), "to": base.isoformat(), "precision": "recent_days"}

    if match := re.search(r"최근\s*(\d{1,3})\s*(?:개월|달)", question):
        months = max(int(match.group(1)), 1)
        start_year, start_month = base.year, base.month - (months - 1)
        while start_month <= 0:
            start_month += 12
            start_year -= 1
        return {
            "from": date(start_year, start_month, 1).isoformat(),
            "to": base.isoformat(),
            "precision": "recent_months",
        }

    if re.search(r"지난\s*달|저번\s*달|전\s+달|전월", question):
        last_month_end = base.replace(day=1) - timedelta(days=1)
        return {
            "from": last_month_end.replace(day=1).isoformat(),
            "to": last_month_end.isoformat(),
            "precision": "last_month",
        }

    if re.search(r"이번\s*달|이달|금월|당월", question):
        last_day = calendar.monthrange(base.year, base.month)[1]
        return {
            "from": base.replace(day=1).isoformat(),
            "to": base.replace(day=last_day).isoformat(),
            "precision": "this_month",
        }

    if re.search(r"지난\s*주|저번\s*주|전\s*주(?!차)", question):
        monday, sunday = _week_bounds(base - timedelta(days=7))
        return {"from": monday.isoformat(), "to": sunday.isoformat(), "precision": "last_week"}

    if re.search(r"이번\s*주|금주(?!차)", question):
        monday, sunday = _week_bounds(base)
        return {"from": monday.isoformat(), "to": sunday.isoformat(), "precision": "this_week"}

    if re.search(r"그제|그저께|엊그제", question):
        target = base - timedelta(days=2)
        return {"from": target.isoformat(), "to": target.isoformat(), "precision": "day"}

    if re.search(r"어제|전일(?!\s*정산)", question):
        target = base - timedelta(days=1)
        return {"from": target.isoformat(), "to": target.isoformat(), "precision": "day"}

    if re.search(r"오늘|금일", question):
        return {"from": base.isoformat(), "to": base.isoformat(), "precision": "day"}

    return None


def _week_bounds(day: date) -> Tuple[date, date]:
    monday = day - timedelta(days=day.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


# "최근 이슈 보여줘"의 "최근"은 정렬 힌트일 뿐 날짜 필터가 아니다. LLM 플래너가
# 이런 단어만 보고 날짜 범위를 지어내는 사례가 실제로 있어(정확도 점검 2026-07-09,
# "담당자 OO의 최근 이슈 2개"가 최근 7일 필터로 오해석돼 빈 결과), 질문에 실제
# 시점 표현이 있을 때만 모델이 준 created_at을 받아들인다.
_TEMPORAL_EXPRESSION_PATTERN = re.compile(
    r"20\d{2}\s*년"
    r"|\d{1,2}\s*월"
    r"|오늘|금일|어제|전일|그제|그저께|엊그제"
    r"|이번\s*주|금주|지난\s*주|저번\s*주"
    r"|이번\s*달|이달|금월|당월|지난\s*달|저번\s*달|전\s+달|전월"
    r"|최근\s*\d{1,3}\s*(?:일|개월|달|주)"
    r"|올해|금년|작년|전년|지난\s*해|이번\s*해|이번\s*년도|지난\s*년도"
)


def question_has_temporal_expression(question: str) -> bool:
    return bool(_TEMPORAL_EXPRESSION_PATTERN.search(str(question or "")))


def _clause_contradicts_filters(clause: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    """규칙이 잡은 긍정 조건을 그대로 다시 제외하는 절인지 판별한다."""
    if clause.get("operator") not in NEGATIVE_OPERATORS:
        return False
    field = clause.get("field")
    positive_key = {"platform": "platform", "customer": "customer"}.get(field) if isinstance(field, str) else None
    positive = filters.get(positive_key) if positive_key else None
    if not positive:
        return False
    values = clause.get("value")
    values = values if isinstance(values, list) else [values]
    normalized_positive = str(positive).strip().casefold()
    return any(str(value).strip().casefold() == normalized_positive for value in values)


def build_query_plan(
    question: str,
    source_plan: Optional[Dict[str, Any]] = None,
    constraints: Optional[Dict[str, Any]] = None,
    model_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    source_plan = source_plan or {}
    constraints = constraints or {}
    model_plan = model_plan or {}
    raw_model_filters = model_plan.get("filters")
    model_filters: Dict[str, Any] = raw_model_filters if isinstance(raw_model_filters, dict) else {}
    model_filter_clauses = normalize_filter_clauses(
        model_plan.get("filter_clauses")
        or (model_plan.get("filters") if isinstance(model_plan.get("filters"), list) else [])
    )
    detected_created_at = constraints.get("created_at") or extract_created_at_range(question)
    model_created_at = model_filters.get("created_at") or model_plan.get("created_at")
    if not model_created_at:
        for clause in model_filter_clauses:
            if clause["field"] == "created_at" and clause["operator"] == "between":
                values = clause.get("value") or []
                if len(values) == 2:
                    model_created_at = {"from": str(values[0]), "to": str(values[1]), "precision": "range"}
                    break
    if model_created_at and not detected_created_at and not question_has_temporal_expression(question):
        # 질문에 시점 표현이 없는데 모델만 날짜 범위를 내놓은 경우 → 환각으로 간주하고 버린다.
        model_created_at = None
    if has_invalid_month_expression(question):
        # "2026년 13월"은 연도가 있어 시점 표현으로는 잡히므로 위 규칙을 통과한다.
        # 규칙이 해석을 거부한 잘못된 월을 모델이 지어낸 범위로 메우지 않도록 막는다.
        model_created_at = None
    created_at = detected_created_at or model_created_at
    if not isinstance(created_at, dict) or not created_at.get("from") or not created_at.get("to"):
        created_at = None

    operation = str(model_plan.get("operation") or model_plan.get("intent") or "").lower()
    if operation not in {"search", "count", "get", "aggregate"}:
        operation = infer_operation(question)
    deterministic_operation = infer_operation(question)
    explicit_list_limit = bool(re.search(r"\d+\s*(?:개|건)\s*만?\s*(?:보여|알려|찾아|조회)", question))
    if deterministic_operation != "search" or explicit_list_limit:
        operation = deterministic_operation

    source = str(model_plan.get("source") or source_plan.get("source") or "general").lower()
    if source not in {"mantis", "notion", "both", "general"}:
        source = str(source_plan.get("source") or "general")

    requested_limit = model_plan.get("limit")
    if not isinstance(requested_limit, int) or requested_limit <= 0:
        requested_limit = None

    deterministic_group_by = extract_group_by(question)
    model_group_by = str(model_plan.get("group_by") or "").strip().lower()
    group_by = deterministic_group_by or (model_group_by if model_group_by in GROUP_BY_PATTERNS else None)

    filters = {
        "customer": constraints.get("customer_exact") or constraints.get("customer_key"),
        "platform": constraints.get("platform_key"),
        "statuses": list(constraints.get("statuses") or []),
        "people": dict(constraints.get("people") or {}),
        "issue_ids": list(constraints.get("issue_ids") or []),
        "screen_numbers": list(constraints.get("screen_numbers") or []),
        "date_fields": dict(constraints.get("date_fields") or {}),
        "created_at": created_at,
    }
    # 규칙이 뽑은 상태 조건이 있으면 사용자가 상태를 언급한 것이다.
    question_mentions_status = bool(
        constraints.get("statuses") or constraints.get("excluded_statuses")
    )
    filter_clauses = normalize_filter_clauses(constraints.get("filter_clauses") or [])
    if group_by == "customer":
        model_filter_clauses = [
            clause for clause in model_filter_clauses if clause["field"] != "customer"
        ]
    for clause in model_filter_clauses:
        if clause["field"] == "created_at" and (
            detected_created_at
            or not question_has_temporal_expression(question)
            or has_invalid_month_expression(question)
        ):
            # 규칙으로 날짜를 확정했으면 모델이 만든 별도 범위를 겹치지 않는다.
            # 두 범위가 함께 남으면 교집합만 검색해 정상 항목을 조용히 누락한다.
            # 질문에 시점 표현이 없거나 월 표현이 잘못된 경우도 모델 날짜를 버린다.
            continue
        if clause["field"] == "status" and (not question_mentions_status or source in {"mantis", "both"}):
            # 질문에 상태 표현이 없는데 모델이 "종료 제외" 같은 필터를 붙이면
            # 사용자가 요청한 개수보다 훨씬 적은 결과가 조용히 나간다.
            # Mantis 상태는 규칙이 확정한다. 개발완료에 모델의 resolved를
            # 겹쳐 붙여 교집합이 0건이 되는 경우도 차단한다.
            continue
        if clause["field"] == "platform" and constraints.get("platform_key"):
            # 별칭을 이미 정규화했으므로 MTS AND '모바일' 같은 중복 조건을 금지한다.
            continue
        if _clause_contradicts_filters(clause, filters):
            # 규칙이 이미 조건으로 잡은 값을 모델이 다시 제외하면(예: 플랫폼=MTS인데
            # 플랫폼 제외=MTS) 결과가 항상 0건이 된다. 사용자가 직접 말한 조건이
            # 우선이므로 모순되는 모델 절은 버린다.
            continue
        if clause not in filter_clauses:
            filter_clauses.append(clause)
    from backend.query_filters import notion_temporal_field
    temporal_field = notion_temporal_field(question) if source == "notion" else "created_at"
    temporal_date_range = created_at
    if temporal_field != "created_at":
        # 마감·배포일은 규칙으로 질문과 직접 연결된 기간만 허용한다. 모델이
        # 제목 속 월 표현을 날짜 조건으로 바꾸는 경우도 여기서 함께 차단한다.
        temporal_date_range = detected_created_at or extract_yearless_temporal_month_range(
            question, temporal_field,
        )
        filters["created_at"] = None
        filter_clauses = [clause for clause in filter_clauses if clause["field"] != "created_at"]
    # 날짜 종류를 모델이 임의로 추가하지 않도록 질문의 표현과 맞춘다.
    filter_clauses = [
        clause for clause in filter_clauses
        if clause["field"] not in {"due_at", "deployed_at"}
    ]
    if temporal_date_range:
        add_filter_clause(
            filter_clauses,
            temporal_field,
            "between",
            [temporal_date_range["from"], temporal_date_range["to"]],
        )
    if source == "notion":
        from backend.notion.search import extract_notion_constraints
        native = extract_notion_constraints(question)
        if native.get("status") or native.get("excluded_statuses"):
            filters["statuses"] = [native["status"]] if native.get("status") else []
            filter_clauses = [clause for clause in filter_clauses if clause["field"] != "status"]
            for operator, values in (("in", filters["statuses"]), ("not_in", native.get("excluded_statuses") or [])):
                if values:
                    add_filter_clause(filter_clauses, "status", operator, values)
    terms = model_plan.get("terms") or source_plan.get("terms") or []
    resolution = constraints.get("status_resolution") or {}
    if resolution.get("status_intent") == "open" and resolution.get("evidence"):
        evidence = re.sub(r"\s+", "", resolution["evidence"]).casefold()
        terms = [term for term in terms if re.sub(r"\s+", "", str(term)).casefold() not in evidence]
    return {
        "version": 2,
        "grounding_text": question,
        "status_resolution": constraints.get("status_resolution"),
        "operation": operation,
        "source": source,
        "filters": filters,
        "filter_clauses": filter_clauses,
        "sort": model_plan.get("sort") or {"field": "created_at", "direction": "desc"},
        "group_by": group_by,
        "limit": requested_limit,
        "terms": [str(term) for term in terms if str(term).strip()][:8],
        "confidence": float(model_plan.get("confidence") or source_plan.get("confidence") or 0.7),
        "planner": "model" if model_plan.get("planned") else source_plan.get("planner", "rule"),
        "reason": str(model_plan.get("reason") or source_plan.get("reason") or "")[:160],
    }


def validate_query_plan(question: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    checks: List[Dict[str, str]] = []
    raw_filters = plan.get("filters")
    filters: Dict[str, Any] = raw_filters if isinstance(raw_filters, dict) else {}
    raw_created_at = filters.get("created_at")
    created_at = raw_created_at if isinstance(raw_created_at, dict) else None

    expected_date = extract_created_at_range(question)
    if plan.get("source") == "notion":
        from backend.query_filters import notion_temporal_field
        if notion_temporal_field(question) != "created_at":
            expected_date = expected_date or extract_yearless_temporal_month_range(
                question, notion_temporal_field(question),
            )
    if expected_date:
        from backend.query_filters import notion_temporal_field
        temporal_field = notion_temporal_field(question) if plan.get("source") == "notion" else "created_at"
        passed = bool(
            created_at
            and created_at.get("from") == expected_date["from"]
            and created_at.get("to") == expected_date["to"]
        )
        if temporal_field != "created_at":
            passed = any(
                clause["field"] == temporal_field and clause["operator"] == "between"
                and clause.get("value") == [expected_date["from"], expected_date["to"]]
                for clause in normalize_filter_clauses(plan.get("filter_clauses", []))
            )
        checks.append({
            "id": "date_intent",
            "label": {"created_at": "등록일 기간 해석", "due_at": "기한 기간 해석", "deployed_at": "운영반영일 기간 해석"}[temporal_field],
            "status": "pass" if passed else "fail",
            "detail": (
                f"{expected_date['from']}~{expected_date['to']}"
                if passed
                else "질문의 기간 조건이 실행 계획에 정확히 반영되지 않음"
            ),
        })

    expected_operation = infer_operation(question)
    operation_passed = plan.get("operation") == expected_operation
    checks.append({
        "id": "operation_intent",
        "label": "요청 작업 해석",
        "status": "pass" if operation_passed else "fail",
        "detail": f"요청={expected_operation} · 계획={plan.get('operation', '미상')}",
    })

    source = plan.get("source")
    source_passed = source in {"mantis", "notion", "both", "general"}
    checks.append({
        "id": "source_intent",
        "label": "검색 소스 해석",
        "status": "pass" if source_passed else "fail",
        "detail": f"소스={source or '미상'}",
    })
    clauses = normalize_filter_clauses(plan.get("filter_clauses") or [])
    if source in {"mantis", "both"}:
        from backend.intent.entities import extract_status_constraints
        expected_statuses = set(extract_status_constraints(question))
        from backend.intent.status_semantics import ALL_STATUSES, resolution_is_grounded
        if ALL_STATUSES.search(question) and not expected_statuses - {"open", "closed"}:
            expected_statuses = set()
        resolution = plan.get("status_resolution") or {}
        if not expected_statuses and resolution_is_grounded(resolution):
            expected_statuses = {"open"}
        if expected_statuses:
            planned_statuses = set(filters.get("statuses") or [])
            clause_statuses: set[str] = set()
            for clause in clauses:
                if clause["field"] == "status" and clause["operator"] in {"equals", "in"}:
                    values = clause["value"] if isinstance(clause["value"], list) else [clause["value"]]
                    clause_statuses.update(str(value) for value in values)
            status_passed = expected_statuses == planned_statuses == clause_statuses
            checks.append({
                "id": "status_intent", "label": "상태 조건 해석",
                "status": "pass" if status_passed else "fail",
                "detail": f"요청={','.join(sorted(expected_statuses))} · 계획={','.join(sorted(planned_statuses)) or '없음'}",
            })
    raw_clauses = plan.get("filter_clauses") or []
    schema_passed = len(clauses) == len(raw_clauses) if isinstance(raw_clauses, list) else False
    checks.append({
        "id": "filter_schema",
        "label": "필터 스키마 검증",
        "status": "pass" if schema_passed else "fail",
        "detail": f"검증된 조건={len(clauses)}개",
    })
    has_negative_phrase = bool(re.search(r"빼고|제외|아닌|말고|없이|미포함|없는", question))
    if has_negative_phrase:
        negative_passed = any(
            clause["operator"] in {"not_equals", "not_in", "not_contains", "not_exists"}
            for clause in clauses
        )
        checks.append({
            "id": "negative_intent",
            "label": "제외 조건 해석",
            "status": "pass" if negative_passed else "fail",
            "detail": "제외 연산자 반영" if negative_passed else "제외 표현이 포함 조건으로 뒤집힘",
        })
    return {
        "valid": all(check["status"] == "pass" for check in checks),
        "checks": checks,
    }


def plan_created_at_matches(value: str, plan_range: Optional[Dict[str, str]]) -> bool:
    if not plan_range:
        return True
    candidate = str(value or "")[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", candidate):
        return False
    # "한 달 넘은 건"처럼 한쪽만 정해진 구간도 있으므로 빈 경계는 무시한다.
    start = str(plan_range.get("from") or "")
    end = str(plan_range.get("to") or "")
    if start and candidate < start:
        return False
    if end and candidate > end:
        return False
    return True
