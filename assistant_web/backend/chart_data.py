"""조회한 원본과 같은 날짜 기준으로 만드는 일별 등록 건수."""
from collections import Counter
from datetime import date, timedelta
from typing import Any, Iterable


def daily_registrations(issues: Iterable[dict[str, Any]], start: date, end: date) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for issue in issues:
        try:
            created = date.fromisoformat(str(issue.get("created_at") or "")[:10])
        except ValueError:
            continue
        if start <= created <= end:
            counts[created.isoformat()] += 1
    return [
        {"date": (start + timedelta(days=offset)).isoformat(),
         "count": counts[(start + timedelta(days=offset)).isoformat()]}
        for offset in range((end - start).days + 1)
    ]
