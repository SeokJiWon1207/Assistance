"""조회 기간의 달력 계산과 명시 날짜 표현."""
from __future__ import annotations
import calendar
import re
from datetime import date, timedelta
from typing import Dict, Optional, Tuple


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
    # 질문 앞의 "10월 MTS 이슈"도 등록 월을 뜻한다. 배포일 등 다른 날짜
    # 필드나 제목 안의 월 표현을 등록일 조건으로 바꾸지는 않는다.
    relative_month = extract_yearless_month_range(question)
    month_issue_request = re.search(
        r"^\s*(?:(?:MTS|HTS)\s*)?(?:1[0-2]|0?[1-9])\s*월\s*"
        r"(?:(?:MTS|HTS)\s*)?이슈", question, re.IGNORECASE,
    )
    if relative_month and (month_issue_request or re.search(
        r"등록일|접수일|생성일|작성일|등록(?:된|한|했|되)|접수(?:된|한|했|되)|"
        r"생성(?:된|한|했|되)|작성(?:된|한|했|되)|만든", question,
    )):
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

    if match := re.search(r"최근\s*(\d{1,3})\s*주", question):
        days = max(int(match.group(1)), 1) * 7
        start = base - timedelta(days=days - 1)
        return {"from": start.isoformat(), "to": base.isoformat(), "precision": "recent_weeks"}

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
