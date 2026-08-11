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
    if re.search(r"(?:이슈\s*)?#?0*\d{5,7}\b", question, re.IGNORECASE) and any(
        marker in normalized for marker in SIMILAR_MARKERS
    ):
        return "search"
    if re.search(r"(?:이슈\s*)?#?0*\d{5,7}\b", question, re.IGNORECASE):
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


def extract_relative_created_at_range(question: str, today: Optional[date] = None) -> Optional[Dict[str, str]]:
    """'오늘/어제/이번 주/지난주/최근 N일' 같은 상대 표현을 실제 날짜 범위로 바꾼다.

    today는 테스트에서 기준일을 고정할 때만 넘기고, 실제 요청 처리에서는
    항상 date.today()를 쓴다(자정을 넘기면 "오늘"의 의미도 바뀌어야 하므로)."""
    base = today or date.today()

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

    if re.search(r"지난\s*달|저번\s*달|전\s*달(?!력)|전월", question):
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
    r"|이번\s*달|이달|금월|당월|지난\s*달|저번\s*달|전\s*달|전월"
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
    detected_created_at = extract_created_at_range(question)
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
            not question_has_temporal_expression(question)
            or has_invalid_month_expression(question)
        ):
            # filters.created_at을 버려도 이 절이 남으면 검색 제약을 만들 때
            # 다시 날짜 범위로 살아나므로 같은 조건으로 함께 막는다.
            continue
        if clause["field"] == "status" and not question_mentions_status:
            # 질문에 상태 표현이 없는데 모델이 "종료 제외" 같은 필터를 붙이면
            # 사용자가 요청한 개수보다 훨씬 적은 결과가 조용히 나간다.
            continue
        if _clause_contradicts_filters(clause, filters):
            # 규칙이 이미 조건으로 잡은 값을 모델이 다시 제외하면(예: 플랫폼=MTS인데
            # 플랫폼 제외=MTS) 결과가 항상 0건이 된다. 사용자가 직접 말한 조건이
            # 우선이므로 모순되는 모델 절은 버린다.
            continue
        if clause not in filter_clauses:
            filter_clauses.append(clause)
    if created_at:
        add_filter_clause(
            filter_clauses,
            "created_at",
            "between",
            [created_at["from"], created_at["to"]],
        )
    terms = model_plan.get("terms") or source_plan.get("terms") or []
    return {
        "version": 2,
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
    if expected_date:
        passed = bool(
            created_at
            and created_at.get("from") == expected_date["from"]
            and created_at.get("to") == expected_date["to"]
        )
        checks.append({
            "id": "date_intent",
            "label": "등록일 기간 해석",
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
    return str(plan_range.get("from", "")) <= candidate <= str(plan_range.get("to", ""))
