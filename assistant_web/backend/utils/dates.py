"""날짜 문자열과 Mantis 날짜 값을 기존 규칙으로 변환한다."""
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional


def parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def format_mantis_custom_date(value: Any) -> str:
    raw_value = str(value or "").strip()
    if not raw_value or raw_value in {"0", "-"}:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_value):
        return raw_value
    if re.fullmatch(r"\d{8}", raw_value):
        return f"{raw_value[:4]}-{raw_value[4:6]}-{raw_value[6:8]}"
    if re.fullmatch(r"\d{10,13}", raw_value):
        timestamp = int(raw_value)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        try:
            # Mantis 변경 이력의 날짜는 한국 시간 기준이다. 자정 값은 UTC로
            # 전날 15시에 저장되므로 UTC 날짜로 자르면 월초 공수가 전월로 빠진다.
            return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=9))).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return raw_value
    return raw_value


def local_timestamp() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
