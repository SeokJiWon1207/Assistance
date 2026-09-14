"""Notion 속성과 내부 페이지 모델 사이를 변환한다."""

import re
from typing import Any, Dict, List

from backend.common import normalize_for_match
from backend.notion.client import (
    fetch_notion_page_blocks as _fetch_notion_page_blocks,
    fetch_notion_relation_title as _fetch_notion_relation_title,
    fetch_notion_user_name as _fetch_notion_user_name,
)
from backend.notion.compat import compat_dependency
from backend.notion.constants import NOISY_PROPERTY_NAME_KEYWORDS, NOISY_PROPERTY_TYPES


def fetch_notion_page_blocks(*args: Any, **kwargs: Any) -> str:
    return compat_dependency("fetch_notion_page_blocks", _fetch_notion_page_blocks)(*args, **kwargs)

def fetch_notion_relation_title(*args: Any, **kwargs: Any) -> str:
    return compat_dependency("fetch_notion_relation_title", _fetch_notion_relation_title)(*args, **kwargs)

def fetch_notion_user_name(*args: Any, **kwargs: Any) -> str:
    return compat_dependency("fetch_notion_user_name", _fetch_notion_user_name)(*args, **kwargs)


def notion_rich_text_to_plain(items: List[Dict[str, Any]]) -> str:
    return "".join(item.get("plain_text", "") for item in items or []).strip()


def notion_user_name(user: Dict[str, Any]) -> str:
    return user.get("name") or user.get("person", {}).get("email") or user.get("id", "")


def notion_property_to_text(prop: Dict[str, Any]) -> str:
    prop_type = prop.get("type")
    value = prop.get(prop_type or "", None)

    if prop_type in {"title", "rich_text"}:
        return notion_rich_text_to_plain(value or [])
    if prop_type in {"select", "status"}:
        return (value or {}).get("name", "")
    if prop_type == "multi_select":
        return ", ".join(item.get("name", "") for item in value or [])
    if prop_type == "people":
        return ", ".join(notion_user_name(user) for user in value or [])
    if prop_type == "date":
        if not value:
            return ""
        start = value.get("start", "")
        end = value.get("end", "")
        return f"{start} ~ {end}" if end else start
    if prop_type == "checkbox":
        return "true" if value else "false"
    if prop_type == "number":
        return "" if value is None else str(value)
    if prop_type in {"url", "email", "phone_number", "created_time", "last_edited_time"}:
        return value or ""
    if prop_type in {"created_by", "last_edited_by"}:
        return notion_user_name(value or {})
    if prop_type == "formula":
        formula_type = (value or {}).get("type")
        if not isinstance(formula_type, str):
            return ""
        return notion_property_to_text({"type": formula_type, formula_type: (value or {}).get(formula_type)})
    if prop_type == "rollup":
        rollup_type = (value or {}).get("type")
        rollup_value = (value or {}).get(rollup_type)
        if isinstance(rollup_value, list):
            return ", ".join(notion_property_to_text(item) for item in rollup_value)
        return "" if rollup_value is None else str(rollup_value)
    if prop_type == "relation":
        return ", ".join(item.get("id", "") for item in value or [])
    if isinstance(value, str):
        return value
    return ""


def notion_prop_by_keywords(
    properties: Dict[str, Dict[str, Any]],
    keywords: List[str],
) -> str:
    for prop_name, prop in properties.items():
        normalized_name = normalize_for_match(prop_name)
        if any(normalize_for_match(keyword) in normalized_name for keyword in keywords):
            text = notion_property_to_text(prop)
            if text:
                return text
    return ""


def notion_title(properties: Dict[str, Dict[str, Any]]) -> str:
    for prop in properties.values():
        if prop.get("type") == "title":
            title = notion_property_to_text(prop)
            if title:
                return title
    return "제목 없음"


def notion_owner_relation_ids(properties: Dict[str, Dict[str, Any]]) -> List[str]:
    relation_ids: List[str] = []
    owner_keywords = ("담당", "owner", "ownership", "담당자", "assignee")
    for prop_name, prop in properties.items():
        normalized_name = normalize_for_match(prop_name)
        if not any(normalize_for_match(keyword) in normalized_name for keyword in owner_keywords):
            continue
        if prop.get("type") != "relation":
            continue
        for item in prop.get("relation") or []:
            relation_id = str(item.get("id") or "").strip()
            if relation_id and relation_id not in relation_ids:
                relation_ids.append(relation_id)
    return relation_ids


def notion_owner_user_ids(properties: Dict[str, Dict[str, Any]]) -> List[str]:
    user_ids: List[str] = []
    owner_keywords = ("담당", "owner", "ownership", "담당자", "assignee")
    for prop_name, prop in properties.items():
        normalized_name = normalize_for_match(prop_name)
        if not any(normalize_for_match(keyword) in normalized_name for keyword in owner_keywords):
            continue
        if prop.get("type") != "people":
            continue
        for user in prop.get("people") or []:
            user_id = str(user.get("id") or "").strip()
            if user_id and user_id not in user_ids:
                user_ids.append(user_id)
    return user_ids


def notion_user_name_hints(properties: Dict[str, Dict[str, Any]]) -> Dict[str, str]:
    hints: Dict[str, str] = {}
    for prop in properties.values():
        if prop.get("type") != "people":
            continue
        for user in prop.get("people") or []:
            user_id = str(user.get("id") or "").strip()
            name = str(user.get("name") or "").strip()
            if user_id and name:
                hints[user_id] = name
    return hints


def notion_status(properties: Dict[str, Dict[str, Any]]) -> str:
    explicit = notion_prop_by_keywords(properties, ["상태", "status", "진행"])
    if explicit:
        return explicit
    for prop in properties.values():
        if prop.get("type") in {"status", "select"}:
            text = notion_property_to_text(prop)
            if text:
                return text
    return "상태 미상"


def notion_owner(properties: Dict[str, Dict[str, Any]]) -> str:
    owner = (
        notion_prop_by_keywords(properties, ["담당", "owner", "ownership", "담당자", "assignee"])
        or "담당자 미지정"
    )
    owner_parts = [
        part.strip()
        for part in owner.split(",")
        if part.strip()
        and not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            part.strip(),
            re.IGNORECASE,
        )
    ]
    return ", ".join(owner_parts) or "담당자 미지정"


def resolve_notion_page_owner(page: Dict[str, Any]) -> Dict[str, Any]:
    if page.get("owner") and page.get("owner") != "담당자 미지정":
        return page
    names = [
        fetch_notion_relation_title(str(page_id))
        for page_id in page.get("owner_relation_ids", [])
    ]
    hints = page.get("owner_user_name_hints") or {}
    for user_id in page.get("owner_user_ids", []):
        resolved_name = str(hints.get(user_id) or fetch_notion_user_name(str(user_id)) or "").strip()
        # An assigned but unreadable Notion user is different from an
        # unassigned item. Keep separate stable buckets instead of collapsing
        # every inaccessible user into "담당자 미지정".
        anonymous_key = str(user_id).translate(str.maketrans("0123456789", "abcdefghij"))
        anonymous_key = re.sub(r"[^a-z]", "", anonymous_key.lower())[:8] or "unknown"
        names.append(resolved_name or f"미확인 담당자({anonymous_key})")
    names = list(dict.fromkeys(name for name in names if name and name != "제목 없음"))
    if names:
        page["owner"] = ", ".join(names)
        page["search_text"] = "\n".join(
            part for part in (page.get("search_text", ""), page["owner"]) if part
        )
    return page


def notion_due_date(properties: Dict[str, Dict[str, Any]]) -> str:
    return notion_prop_by_keywords(properties, ["일정", "기한", "날짜", "date", "due"]) or ""


def notion_type(properties: Dict[str, Dict[str, Any]]) -> str:
    return notion_prop_by_keywords(properties, ["타입", "유형", "분류", "type", "category"]) or ""


def is_noisy_property_name(name: str) -> bool:
    normalized_name = normalize_for_match(name)
    return any(
        normalize_for_match(keyword) in normalized_name
        for keyword in NOISY_PROPERTY_NAME_KEYWORDS
    )


def notion_display_property_text(prop: Dict[str, Any]) -> str:
    """본문 표시용 값. 사람이 읽을 수 없는 UUID는 건수로 바꾼다.

    people/relation은 원본 API가 이름 없이 사용자·페이지 ID만 주는 경우가 있다.
    그대로 찍으면 화면과 모델 근거가 UUID로 뒤덮이므로, 이름을 아는 만큼만 적고
    나머지는 "미확인 사용자 N명"처럼 건수로 남겨 배정 사실 자체는 잃지 않는다.
    """
    prop_type = prop.get("type")
    if prop_type == "people":
        names: List[str] = []
        unknown = 0
        for user in prop.get("people") or []:
            name = str(user.get("name") or (user.get("person") or {}).get("email") or "").strip()
            if name:
                names.append(name)
            else:
                unknown += 1
        if unknown:
            names.append(f"미확인 사용자 {unknown}명")
        return ", ".join(names)
    if prop_type == "relation":
        # 관계 대상 제목은 페이지마다 추가 조회가 필요해 목록 응답에서는 건수만 남긴다.
        count = len(prop.get("relation") or [])
        return f"연결 항목 {count}건" if count else ""
    if prop_type == "checkbox":
        return "예" if prop.get("checkbox") else ""
    return notion_property_to_text(prop)


def notion_properties_to_text(properties: Dict[str, Dict[str, Any]]) -> str:
    parts = []
    for name, prop in properties.items():
        if prop.get("type") in NOISY_PROPERTY_TYPES or is_noisy_property_name(name):
            continue
        text = notion_display_property_text(prop)
        if text:
            parts.append(f"{name}: {text}")
    return "\n".join(parts)


def notion_properties_to_search_text(properties: Dict[str, Dict[str, Any]]) -> str:
    useful_name_keywords = [
        "제목",
        "내용",
        "메모",
        "고객사",
        "매체",
        "분류",
        "타입",
        "유형",
        "상태",
        "담당",
        "owner",
        "type",
        "category",
        "status",
    ]
    parts = []
    for name, prop in properties.items():
        normalized_name = normalize_for_match(name)
        if is_noisy_property_name(name):
            continue
        if not any(normalize_for_match(keyword) in normalized_name for keyword in useful_name_keywords):
            continue
        text = notion_property_to_text(prop)
        if text:
            parts.append(f"{name}: {text}")
    return "\n".join(parts)


def notion_block_text(block: Dict[str, Any]) -> str:
    block_type = block.get("type")
    value = block.get(block_type or "", {})
    if block_type in {
        "paragraph",
        "heading_1",
        "heading_2",
        "heading_3",
        "bulleted_list_item",
        "numbered_list_item",
        "to_do",
        "toggle",
        "quote",
        "callout",
    }:
        return notion_rich_text_to_plain(value.get("rich_text", []))
    if block_type == "child_page":
        return value.get("title", "")
    return ""


def normalize_notion_page(
    page: Dict[str, Any],
    include_blocks: bool = True,
    database_id: str = "",
    database_label: str = "",
) -> Dict[str, Any]:
    properties = page.get("properties", {})
    property_text = notion_properties_to_text(properties)
    search_property_text = notion_properties_to_search_text(properties)
    block_text = fetch_notion_page_blocks(page.get("id", ""), max_blocks=20) if include_blocks else ""
    content = "\n\n".join(part for part in (property_text, block_text) if part).strip()
    title = notion_title(properties)
    status = notion_status(properties)
    owner = notion_owner(properties)
    due_date = notion_due_date(properties)
    page_type = notion_type(properties)
    customer = notion_prop_by_keywords(properties, ["고객사", "customer"]) or ""
    platform = notion_prop_by_keywords(properties, ["매체", "플랫폼", "media", "platform"]) or ""
    return {
        "id": page.get("id", ""),
        "title": title,
        "status": status,
        "status_fields": {
            name: notion_property_to_text(prop) for name, prop in properties.items()
            if prop.get("type") in {"status", "select"}
            and any(key in normalize_for_match(name) for key in ("상태", "status", "진행"))
        },
        "owner": owner,
        "owner_relation_ids": notion_owner_relation_ids(properties),
        "owner_user_ids": notion_owner_user_ids(properties),
        "owner_user_name_hints": notion_user_name_hints(properties),
        "due_date": due_date,
        "type": page_type,
        "customer": customer,
        "platform": platform,
        "database_id": database_id,
        "database_label": database_label or (database_id[-8:] if database_id else "Notion DB"),
        "url": page.get("url", ""),
        "created_time": str(page.get("created_time", ""))[:10],
        "last_edited_time": str(page.get("last_edited_time", ""))[:10],
        "properties_text": property_text,
        "search_text": "\n".join(
            part
            for part in (title, status, owner, due_date, page_type, search_property_text)
            if part
        ),
        "content": content or title,
    }


__all__ = [
    "notion_rich_text_to_plain",
    "notion_user_name",
    "notion_property_to_text",
    "notion_prop_by_keywords",
    "notion_title",
    "notion_owner_relation_ids",
    "notion_owner_user_ids",
    "notion_user_name_hints",
    "notion_status",
    "notion_owner",
    "resolve_notion_page_owner",
    "notion_due_date",
    "notion_type",
    "is_noisy_property_name",
    "notion_display_property_text",
    "notion_properties_to_text",
    "notion_properties_to_search_text",
    "notion_block_text",
    "normalize_notion_page",
]
