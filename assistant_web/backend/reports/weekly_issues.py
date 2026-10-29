"""토~금 업무 날짜 기준 차트. 기존 등록일·미종료 지표와 독립적이다."""
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any

from backend.effort_fields import delivery_date
from backend.mantis_status import canonical_mantis_status

WEEK_ANCHOR = date(2022, 1, 1)
REMAINING_EXCLUDED = frozenset({"closed", "feedback", "developed", "resolved"})


def _media(issue: dict[str, Any]) -> str:
    value = str(issue.get("media_type") or "").strip()
    return value.upper() if value.casefold() in {"hts", "mts"} else value or "기타/미상"


def _media_order(name: str) -> tuple[int, str]:
    return ({"HTS": 0, "MTS": 1}.get(name, 3 if name == "기타/미상" else 2), name)


def _grouped(issues: list[dict[str, Any]], missing: int) -> dict[str, Any]:
    counts: dict[str, Counter[str]] = defaultdict(Counter)
    for issue in issues:
        label = str(issue.get("severity_label") or issue.get("severity") or "미분류").strip() or "미분류"
        counts[_media(issue)][label] += 1
    return {"total": len(issues), "missingDate": missing,
            "rows": [{"name": name, "counts": dict(counts[name])} for name in sorted(counts, key=_media_order)]}


def weekly_issue_summary(issues: list[dict[str, Any]], today: date, *, complete: bool) -> dict[str, Any]:
    start = WEEK_ANCHOR + timedelta(weeks=(today - WEEK_ANCHOR).days // 7)
    end = start + timedelta(days=6)
    received, completed = [], []
    missing_request = missing_delivery = 0
    remaining: Counter[str] = Counter()
    for issue in issues:
        dates = issue.get("date_fields") or {}
        requested = delivery_date(dates.get("고객사요청일"))
        delivered = delivery_date(dates.get("고객사전달일"))
        if not requested:
            missing_request += 1
        elif start.isoformat() <= requested <= end.isoformat():
            received.append(issue)
        if "정기점검" not in str(issue.get("summary") or ""):
            if not delivered:
                missing_delivery += 1
            elif start.isoformat() <= delivered <= end.isoformat():
                completed.append(issue)
        status = canonical_mantis_status(str(issue.get("status") or ""))
        status = status or canonical_mantis_status(str(issue.get("status_label") or ""))
        if status not in REMAINING_EXCLUDED:
            remaining[_media(issue)] += 1
    return {
        "from": start.isoformat(), "to": end.isoformat(), "complete": complete,
        "received": _grouped(received, missing_request),
        "completed": _grouped(completed, missing_delivery),
        "remaining": {"total": sum(remaining.values()),
                      "rows": [{"name": name, "count": remaining[name]} for name in sorted(remaining, key=_media_order)]},
    }
