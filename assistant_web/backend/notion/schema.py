"""Notion 데이터베이스와 속성 스키마를 해석한다."""

import re
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from backend.common import normalize_for_match
from backend.config import get_notion_settings as _get_notion_settings
from backend.notion.client import (
    fetch_notion_database_schema as _fetch_notion_database_schema,
    fetch_notion_database_title as _fetch_notion_database_title,
)
from backend.notion.compat import compat_dependency


def get_notion_settings() -> Dict[str, Any]:
    return compat_dependency("get_notion_settings", _get_notion_settings)()

def fetch_notion_database_schema(database_id: str) -> Dict[str, Any]:
    return compat_dependency("fetch_notion_database_schema", _fetch_notion_database_schema)(database_id)

def fetch_notion_database_title(database_id: str) -> str:
    return compat_dependency("fetch_notion_database_title", _fetch_notion_database_title)(database_id)


def select_notion_database_id(label_or_id: Optional[str] = None) -> str:
    settings = get_notion_settings()
    database_ids = settings.get("database_ids", [])
    if not settings["token"] or not database_ids:
        raise HTTPException(status_code=503, detail="NOTION_API_TOKEN/NOTION_DATABASE_IDS가 설정되지 않았습니다.")

    if label_or_id:
        normalized_target = normalize_for_match(label_or_id.replace("-", ""))
        for database_id in database_ids:
            if normalized_target in normalize_for_match(database_id):
                return database_id
            title = fetch_notion_database_title(database_id)
            if normalized_target in normalize_for_match(title):
                return database_id

    if len(database_ids) == 1:
        return database_ids[0]

    for database_id in database_ids:
        title = fetch_notion_database_title(database_id)
        if "주요이슈" in normalize_for_match(title):
            return database_id
    return database_ids[0]


def normalize_notion_id(value: str) -> str:
    return re.sub(r"[^0-9a-f]", "", str(value or "").lower())


def validate_notion_page_id(page_id: str) -> str:
    normalized = normalize_notion_id(page_id)
    if len(normalized) != 32:
        raise HTTPException(status_code=422, detail="올바르지 않은 Notion 페이지 ID입니다.")
    return normalized


def ensure_configured_notion_database(database_id: str) -> str:
    normalized = normalize_notion_id(database_id)
    allowed = {
        normalize_notion_id(item)
        for item in get_notion_settings().get("database_ids", [])
        if normalize_notion_id(item)
    }
    if not normalized or normalized not in allowed:
        raise HTTPException(status_code=403, detail="허용되지 않은 Notion 데이터베이스입니다.")
    return normalized


def notion_property_schema(database_id: str) -> Dict[str, Dict[str, Any]]:
    return fetch_notion_database_schema(database_id).get("properties", {})


def find_notion_property(
    properties: Dict[str, Dict[str, Any]],
    keywords: List[str],
    allowed_types: Optional[set[str]] = None,
) -> Optional[tuple[str, Dict[str, Any]]]:
    normalized_keywords = [normalize_for_match(keyword) for keyword in keywords]
    candidates = [
        (prop_name, prop)
        for prop_name, prop in properties.items()
        if not allowed_types or prop.get("type") in allowed_types
    ]
    for keyword in normalized_keywords:
        for prop_name, prop in candidates:
            if normalize_for_match(prop_name) == keyword:
                return prop_name, prop
    for prop_name, prop in candidates:
        normalized_name = normalize_for_match(prop_name)
        if any(keyword in normalized_name for keyword in normalized_keywords):
            return prop_name, prop
    return None


def find_notion_option_property(
    properties: Dict[str, Dict[str, Any]],
    keywords: List[str],
    value: str,
) -> Optional[tuple[str, Dict[str, Any]]]:
    candidates: List[tuple[int, int, str, Dict[str, Any]]] = []
    normalized_keywords = [normalize_for_match(keyword) for keyword in keywords]
    for prop_name, prop in properties.items():
        if prop.get("type") not in {"select", "status"}:
            continue
        normalized_name = normalize_for_match(prop_name)
        name_score = next(
            (0 if normalized_name == keyword else 1 for keyword in normalized_keywords if keyword in normalized_name),
            None,
        )
        if name_score is None:
            continue
        mapped = notion_option_name(prop, value)
        prop_type = prop.get("type")
        options = (prop.get(prop_type, {}) or {}).get("options", []) if isinstance(prop_type, str) else []
        option_names = {normalize_for_match(option.get("name", "")) for option in options}
        option_score = 0 if normalize_for_match(mapped) in option_names else 1
        candidates.append((option_score, name_score, prop_name, prop))
    if not candidates:
        return None
    _, _, prop_name, prop = sorted(candidates, key=lambda item: (item[0], item[1], len(item[2])))[0]
    return prop_name, prop


def notion_title_property(properties: Dict[str, Dict[str, Any]]) -> str:
    for prop_name, prop in properties.items():
        if prop.get("type") == "title":
            return prop_name
    raise HTTPException(status_code=502, detail="Notion DB에 title 속성이 없습니다.")


def notion_option_name(prop: Dict[str, Any], value: str) -> str:
    requested = str(value or "").strip()
    if not requested:
        return requested
    prop_type = prop.get("type")
    options = (prop.get(prop_type, {}) or {}).get("options", []) if isinstance(prop_type, str) else []
    for option in options:
        if normalize_for_match(option.get("name", "")) == normalize_for_match(requested):
            return option.get("name", requested)
    for option in options:
        if normalize_for_match(requested) in normalize_for_match(option.get("name", "")):
            return option.get("name", requested)
    status_aliases = {
        "완료": ["완료", "종료된이슈", "결재완료", "결재완료(팀장)"],
        "진행": ["진행 중", "진행중", "할당된이슈"],
        "등록": ["등록됨", "신규이슈"],
        "보관": ["보관됨"],
        "결재요청": ["결재요청"],
    }
    normalized_requested = normalize_for_match(requested)
    for alias_key, candidates in status_aliases.items():
        if normalize_for_match(alias_key) in normalized_requested:
            for candidate in candidates:
                for option in options:
                    if normalize_for_match(option.get("name", "")) == normalize_for_match(candidate):
                        return option.get("name", candidate)
    return requested


def notion_property_payload(prop: Dict[str, Any], value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    prop_type = prop.get("type")
    if prop_type == "title":
        return {"title": [{"type": "text", "text": {"content": str(value)}}]}
    if prop_type == "rich_text":
        return {"rich_text": [{"type": "text", "text": {"content": str(value)}}]}
    if prop_type == "select":
        return {"select": {"name": notion_option_name(prop, str(value))}}
    if prop_type == "status":
        return {"status": {"name": notion_option_name(prop, str(value))}}
    if prop_type == "multi_select":
        values = value if isinstance(value, list) else re.split(r"[,/]", str(value))
        return {
            "multi_select": [
                {"name": notion_option_name(prop, item)}
                for item in (item.strip() for item in values)
                if item
            ]
        }
    if prop_type == "date":
        text = str(value).strip()
        match = re.search(r"\d{4}-\d{2}-\d{2}", text)
        if not match:
            return None
        return {"date": {"start": match.group(0)}}
    if prop_type == "checkbox":
        return {"checkbox": bool(value)}
    if prop_type == "number":
        try:
            return {"number": float(value)}
        except (TypeError, ValueError):
            return None
    if prop_type == "url":
        return {"url": str(value)}
    if prop_type == "email":
        return {"email": str(value)}
    if prop_type == "phone_number":
        return {"phone_number": str(value)}
    return None


__all__ = [
    "select_notion_database_id",
    "normalize_notion_id",
    "validate_notion_page_id",
    "ensure_configured_notion_database",
    "notion_property_schema",
    "find_notion_property",
    "find_notion_option_property",
    "notion_title_property",
    "notion_option_name",
    "notion_property_payload",
]
