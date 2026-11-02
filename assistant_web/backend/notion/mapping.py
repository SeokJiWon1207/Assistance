"""Notion 속성과 내부 페이지 모델 사이를 변환한다."""

from backend.notion.values import (
    KST as KST,
    notion_kst_date as notion_kst_date,
    notion_rich_text_to_plain as notion_rich_text_to_plain,
    notion_user_name as notion_user_name,
    notion_property_to_text as notion_property_to_text,
    notion_prop_by_keywords as notion_prop_by_keywords,
    notion_title as notion_title,
    notion_owner_relation_ids as notion_owner_relation_ids,
    notion_owner_user_ids as notion_owner_user_ids,
    notion_user_name_hints as notion_user_name_hints,
    notion_status as notion_status,
    notion_owner as notion_owner,
    notion_due_date as notion_due_date,
    notion_type as notion_type,
    is_noisy_property_name as is_noisy_property_name,
    notion_display_property_text as notion_display_property_text,
    notion_properties_to_text as notion_properties_to_text,
    notion_properties_to_search_text as notion_properties_to_search_text,
    notion_block_text as notion_block_text,
)
from backend.notion import read_api
import re
from datetime import datetime as datetime, timedelta as timedelta, timezone as timezone
from typing import Any as Any, Dict as Dict, List as List

from backend.utils.text import normalize_for_match
from backend.notion.read_api import fetch_notion_page_blocks as fetch_notion_page_blocks, fetch_notion_relation_title as fetch_notion_relation_title, fetch_notion_user_name as fetch_notion_user_name
from backend.notion.constants import NOISY_PROPERTY_NAME_KEYWORDS as NOISY_PROPERTY_NAME_KEYWORDS, NOISY_PROPERTY_TYPES as NOISY_PROPERTY_TYPES
from backend.notion.property_roles import (
    CUSTOMER_NAMES, DEPLOYMENT_NAMES, DUE_NAMES, REGISTRATION_NAMES, date_property,
    owner_properties, responsible_properties, role_properties,
)


def resolve_notion_page_owner(page: Dict[str, Any]) -> Dict[str, Any]:
    names = [
        name.strip() for name in str(page.get("owner") or "").split(",")
        if name.strip() and name.strip() not in {"담당자 미지정", "미지정"}
    ]
    names.extend(
        read_api.fetch_notion_relation_title(str(page_id))
        for page_id in page.get("owner_relation_ids", [])
    )
    hints = page.get("owner_user_name_hints") or {}
    for user_id in page.get("owner_user_ids", []):
        resolved_name = str(hints.get(user_id) or read_api.fetch_notion_user_name(str(user_id)) or "").strip()
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


def normalize_notion_page(
    page: Dict[str, Any],
    include_blocks: bool = True,
    database_id: str = "",
    database_label: str = "",
) -> Dict[str, Any]:
    properties = page.get("properties", {})
    property_text = notion_properties_to_text(properties)
    search_property_text = notion_properties_to_search_text(properties)
    block_text = read_api.fetch_notion_page_blocks(page.get("id", ""), max_blocks=200) if include_blocks else ""
    content = "\n\n".join(part for part in (property_text, block_text) if part).strip()
    title = notion_title(properties)
    status = notion_status(properties)
    owner = notion_owner(properties)
    due_date = notion_due_date(properties)
    deployment_property = date_property(properties, DEPLOYMENT_NAMES)
    registration_properties = role_properties(properties, REGISTRATION_NAMES, {"date"})
    registration_property = registration_properties[0] if registration_properties else ""
    registration_dates = [
        notion_kst_date((properties.get(name, {}).get("date") or {}).get("start"))
        for name in registration_properties
        if (properties.get(name, {}).get("date") or {}).get("start")
    ]
    created_time_raw = str(page.get("created_time", ""))
    last_edited_time_raw = str(page.get("last_edited_time", ""))
    page_type = notion_type(properties)
    customer_properties = role_properties(
        properties,
        CUSTOMER_NAMES,
        {"select", "multi_select", "rich_text"},
    )
    customer = ", ".join(
        value
        for name in customer_properties
        if (value := notion_property_to_text(properties[name]))
    )
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
        "owner_role": "기여자" if not owner_properties(properties) and responsible_properties(properties) else "담당자",
        "owner_properties": responsible_properties(properties),
        "owner_relation_ids": notion_owner_relation_ids(properties),
        "owner_user_ids": notion_owner_user_ids(properties),
        "owner_user_name_hints": notion_user_name_hints(properties),
        "due_date": due_date,
        "due_date_property": date_property(properties, DUE_NAMES),
        "deployment_date": str((properties.get(deployment_property, {}).get("date") or {}).get("start") or ""),
        "registration_date": registration_dates[0] if registration_dates else "",
        "registration_time_raw": next((str((properties[name].get("date") or {}).get("start"))
                                       for name in registration_properties if (properties[name].get("date") or {}).get("start")), ""),
        "registration_dates": registration_dates,
        "registration_date_property": registration_property,
        "registration_date_properties": registration_properties,
        "date_fields": {name: notion_property_to_text(prop) for name, prop in properties.items() if prop.get("type") == "date"},
        "type": page_type,
        "customer": customer,
        "platform": platform,
        "database_id": database_id,
        "database_label": database_label or (database_id[-8:] if database_id else "Notion DB"),
        "url": page.get("url", ""),
        "created_time": notion_kst_date(created_time_raw),
        "created_time_raw": created_time_raw,
        "last_edited_time": notion_kst_date(last_edited_time_raw),
        "last_edited_time_raw": last_edited_time_raw,
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
    "notion_kst_date",
    "normalize_notion_page",
]
