"""Mantis 상태의 단일 업무 정의. 알 수 없는 상태를 미종료로 추정하지 않는다."""

import re
from typing import Any, Dict, Optional

from backend.config import MANTIS_STATUS_NAMES as STATUS_NAMES, MANTIS_OPEN_STATUSES as OPEN_STATUSES


def canonical_mantis_status(value: str) -> Optional[str]:
    normalized = re.sub(r"\s+", "", str(value or "")).casefold()
    return next((name for name, aliases in STATUS_NAMES.items()
                 if normalized in {re.sub(r"\s+", "", alias).casefold() for alias in aliases}), None)


def mantis_status_bucket(value: str) -> str:
    status = canonical_mantis_status(value)
    if status in OPEN_STATUSES:
        return "open"
    return "done" if status in {"resolved", "closed"} else "unknown"


def matches_mantis_status(value: str, requested: str) -> bool:
    if requested == "open":
        return mantis_status_bucket(value) == "open"
    canonical = canonical_mantis_status(requested)
    return bool(canonical and canonical_mantis_status(value) == canonical)


def source_status(source: Dict[str, Any]) -> str:
    value = source.get("status") or (source.get("detail") or {}).get("status") or ""
    if isinstance(value, dict):
        return str(value.get("name") or value.get("label") or "")
    return str(value)
