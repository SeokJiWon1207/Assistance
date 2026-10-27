"""공수 합계에 반영한 행을 같은 전달일 기준의 일별 시계열로 만든다."""
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from backend.effort_fields import issue_delivery_date


def daily_effort(
    rows: list[dict[str, Any]], start: str, end: str, *, today: date | None = None,
) -> list[dict[str, Any]] | None:
    """기입자 필터를 통과한 effort_items만 합산한다. 장기 조회는 생략한다."""
    first, last = date.fromisoformat(start), date.fromisoformat(end)
    if (last - first).days >= 366:
        return None
    if not rows:
        return []
    amounts: dict[str, Decimal] = defaultdict(Decimal)
    for row in rows:
        day = issue_delivery_date(row)
        if day and start <= day <= end:
            amounts[day] += sum((Decimal(str(item['amount'])) for item in row['effort_items']), Decimal(0))
    if not amounts:
        return []
    # 미래의 빈 날은 실적 0으로 만들지 않는다. 이미 합계에 포함된 미래 전달일은 보존한다.
    today = today or datetime.now(timezone(timedelta(hours=9))).date()
    last = min(last, max(today, date.fromisoformat(max(amounts))))
    return [
        {'date': (calendar_day := first + timedelta(days=offset)).isoformat(),
         'amount': float(amounts.get(calendar_day.isoformat(), Decimal(0)))}
        for offset in range((last - first).days + 1)
    ]
