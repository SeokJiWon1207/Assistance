"""Mantis 날짜 필드 표현을 추출하고 정규화한다."""

import re
from typing import Dict

from backend.config import MANTIS_DATE_FIELD_ALIASES


def extract_date_field_constraints(text: str) -> Dict[str, str]:
    constraints: Dict[str, str] = {}
    date_pattern = (
        r"(20\d{2})(?:[./-]|년)\s*"
        r"(1[0-2]|0?[1-9])(?:[./-]|월)\s*"
        r"(3[01]|[12]\d|0?[1-9])일?(?!\d)"
    )
    for field_name, aliases in MANTIS_DATE_FIELD_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            match = re.search(
                rf"{re.escape(alias)}\s*(?:이|가|은|는|을|를|:|=)?\s*{date_pattern}",
                text,
                re.IGNORECASE,
            )
            if not match:
                continue
            year, month, day = match.group(1), int(match.group(2)), int(match.group(3))
            constraints[field_name] = f"{year}-{month:02d}-{day:02d}"
            break
    return constraints


def remove_date_field_phrases(text: str) -> str:
    cleaned = text
    for aliases in MANTIS_DATE_FIELD_ALIASES.values():
        for alias in sorted(aliases, key=len, reverse=True):
            cleaned = re.sub(re.escape(alias), " ", cleaned, flags=re.IGNORECASE)
    return cleaned


__all__ = ["extract_date_field_constraints", "remove_date_field_phrases"]
