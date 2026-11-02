"""Mantis 상태의 단일 업무 정의. 알 수 없는 상태를 미종료로 추정하지 않는다."""

import re
from typing import Any, Dict, Optional

from backend.settings.values import MANTIS_STATUS_NAMES as STATUS_NAMES, MANTIS_OPEN_STATUSES as OPEN_STATUSES


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


MANTIS_STATUS_DISPLAY_LABELS = {
    "new": "신규 이슈",
    "feedback": "정보 부족",
    "acknowledged": "이슈 검토",
    "confirmed": "확인된 이슈",
    "assigned": "할당된 이슈",
    "developed": "개발완료",
    "resolved": "해결된 이슈",
    "closed": "종료된 이슈",
    # 검색 결과에서만 사용하는 묶음 상태다.
    "open": "미종료",
}


def display_mantis_status_label(status: Any) -> str:
    """Mantis 내부 상태 코드를 사용자용 한국어 라벨로 변환한다."""
    normalized = str(status or "").strip()
    return MANTIS_STATUS_DISPLAY_LABELS.get(
        normalized.casefold(), normalized or "상태 미상"
    )
