"""답변 본문에 쓰는 사용자용 표기. 리포트마다 따로 바꾸면 화면마다 이름이 어긋난다.

- 고객사: Mantis 프로젝트 이름(112KBStar(KB증권), 117Hantu_Project) → 화면 이름(KB증권, 한국투자증권)
- 상태: 내부 값(confirmed, developed) → 화면 이름(확인된 이슈, 개발완료)
- 날짜: 2026-10-07 → 10월 7일(올해가 아니면 연도 포함)
- 시각: 2026-10-07T05:35:39+00:00 → 10월 7일 오후 2:35(한국 시간)
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from backend.common import display_mantis_status_label
from backend.team_templates import customer_display_for_project

KST = timezone(timedelta(hours=9))


def customer_name(project: Any) -> str:
    raw = str(project or "").strip()
    if not raw:
        return "고객사 미상"
    return customer_display_for_project(raw) or raw


def status_name(status: Any) -> str:
    return display_mantis_status_label(status)


def _parse_day(value: Any) -> Optional[date]:
    try:
        return date.fromisoformat(str(value or "")[:10])
    except ValueError:
        return None


def korean_day(value: Any, today: Optional[date] = None) -> str:
    """'2026-10-07' → '10월 7일'. 날짜가 아니면 받은 값을 그대로 돌려준다."""
    parsed = _parse_day(value)
    if parsed is None:
        return str(value or "")
    today = today or date.today()
    return f"{parsed.month}월 {parsed.day}일" if parsed.year == today.year else f"{parsed.year}년 {parsed.month}월 {parsed.day}일"


def korean_day_range(start: Any, end: Any, today: Optional[date] = None) -> str:
    """같은 달이면 '10월 5일~11일', 아니면 '9월 28일~10월 4일'."""
    first, last = _parse_day(start), _parse_day(end)
    if first is None or last is None:
        return f"{korean_day(start, today)}~{korean_day(end, today)}"
    today = today or date.today()
    if first.year == last.year == today.year and first.month == last.month:
        return f"{first.month}월 {first.day}일~{last.day}일"
    return f"{korean_day(first.isoformat(), today)}~{korean_day(last.isoformat(), today)}"


def korean_time(value: Any, today: Optional[date] = None) -> str:
    """ISO 시각을 한국 시간 '10월 7일 오후 2:35'로. 해석할 수 없으면 받은 값을 그대로 돌려준다."""
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    local = parsed.astimezone(KST)
    hour = local.hour % 12 or 12
    period = "오전" if local.hour < 12 else "오후"
    return f"{korean_day(local.date().isoformat(), today)} {period} {hour}:{local.minute:02d}"
