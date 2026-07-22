"""Notion 소스 어댑터 — 페이지 조회/파싱/검색/쓰기(Layer 3).

Notion REST API 호출, 속성/블록 파싱, 페이지 정규화·검색·랭킹, 쓰기(생성/수정)
명령 해석과 결정론적 답변 포맷을 담당한다. @app 라우트 핸들러는 app.py에 남는다.

의존: config/common/schemas/query_planning + understanding(should_run_notion_search 등).
app.py는 `from backend.sources.notion import *`로 이 이름들을 그대로 재노출한다.
"""

import os
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import HTTPException

from backend.common import (
    extract_quoted_value,
    is_valid_person_name,
    normalize_for_match,
    normalize_search_token,
    remove_quoted_value,
    strip_korean_particle,
)
from backend.config import (
    NOTION_API_BASE,
    STOPWORDS,
    get_notion_headers,
    get_notion_settings,
    notion_post_json,
)
from backend.query_planning import extract_group_by
from backend.query_filters import filter_clause_label, normalize_filter_clauses, values_for_clause
from backend.schemas import ChatMessage, NotionPageUpdateRequest, NotionPageWriteRequest
from backend.understanding import (
    ISSUE_LIST_INTENT_KEYWORDS,
    NOTION_QUERY_STOPWORDS,
    load_known_people,
    should_run_notion_search,
)


NOTION_STATUS_QUERY_TERMS = {
    "등록됨",
    "보관됨",
    "결재요청",
    "결재완료",
    "결재완료(팀장)",
    "완료",
    "진행중",
    "종료된이슈",
    "할당된이슈",
}


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


@lru_cache(maxsize=512)
def fetch_notion_relation_title(page_id: str) -> str:
    """Resolve a related Notion page ID to its title for owner grouping."""
    if not page_id:
        return ""
    try:
        response = requests.get(
            f"{NOTION_API_BASE}/pages/{page_id}",
            headers=get_notion_headers(),
            timeout=20,
        )
    except requests.RequestException:
        return ""
    if response.status_code != 200:
        return ""
    return notion_title(response.json().get("properties", {}))


@lru_cache(maxsize=512)
def fetch_notion_user_name(user_id: str) -> str:
    if not user_id:
        return ""
    try:
        response = requests.get(
            f"{NOTION_API_BASE}/users/{user_id}",
            headers=get_notion_headers(),
            timeout=20,
        )
    except requests.RequestException:
        return ""
    if response.status_code != 200:
        return ""
    return notion_user_name(response.json())


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


def notion_properties_to_text(properties: Dict[str, Dict[str, Any]]) -> str:
    parts = []
    for name, prop in properties.items():
        text = notion_property_to_text(prop)
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
    noisy_name_keywords = [
        "휴가표시",
        "vacation",
        "주차",
        "금주",
        "차주",
        "경과",
        "오늘",
        "생성",
        "최종",
        "편집",
        "on/off",
        "id",
        "uuid",
    ]
    parts = []
    for name, prop in properties.items():
        normalized_name = normalize_for_match(name)
        if any(normalize_for_match(keyword) in normalized_name for keyword in noisy_name_keywords):
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


def fetch_notion_page_blocks(page_id: str, max_blocks: int = 30) -> str:
    if os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() in {"0", "false", "no"}:
        return ""

    blocks = []
    cursor = None
    while len(blocks) < max_blocks:
        params = {"page_size": min(100, max_blocks - len(blocks))}
        if cursor:
            params["start_cursor"] = cursor
        try:
            response = requests.get(
                f"{NOTION_API_BASE}/blocks/{page_id}/children",
                headers=get_notion_headers(),
                params=params,
                timeout=20,
            )
        except requests.RequestException:
            break

        if response.status_code != 200:
            break
        data = response.json()
        blocks.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")

    return "\n".join(text for text in (notion_block_text(block) for block in blocks) if text).strip()


@lru_cache(maxsize=16)
def fetch_notion_database_title(database_id: str) -> str:
    try:
        response = requests.get(
            f"{NOTION_API_BASE}/databases/{database_id}",
            headers=get_notion_headers(),
            timeout=15,
        )
    except requests.RequestException:
        return database_id[-8:] if database_id else "Notion DB"

    if response.status_code != 200:
        return database_id[-8:] if database_id else "Notion DB"

    data = response.json()
    title = notion_rich_text_to_plain(data.get("title", []))
    return title or (database_id[-8:] if database_id else "Notion DB")


@lru_cache(maxsize=16)
def fetch_notion_database_schema(database_id: str) -> Dict[str, Any]:
    response = requests.get(
        f"{NOTION_API_BASE}/databases/{database_id}",
        headers=get_notion_headers(),
        timeout=20,
    )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Notion DB 정보를 불러오지 못했습니다: {response.status_code} {response.text[:300]}",
        )
    return response.json()


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
        options = (prop.get(prop.get("type"), {}) or {}).get("options", [])
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
    options = (prop.get(prop_type, {}) or {}).get("options", [])
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


@lru_cache(maxsize=1)
def fetch_notion_users() -> tuple[tuple[str, str], ...]:
    users: List[tuple[str, str]] = []
    cursor = None
    while True:
        params = {"page_size": 100}
        if cursor:
            params["start_cursor"] = cursor
        response = requests.get(
            f"{NOTION_API_BASE}/users",
            headers=get_notion_headers(),
            params=params,
            timeout=20,
        )
        if response.status_code >= 400:
            return tuple(users)
        data = response.json()
        for user in data.get("results", []):
            name = notion_user_name(user)
            user_id = user.get("id")
            if name and user_id:
                users.append((name, user_id))
        if not data.get("has_more"):
            break
        cursor = data.get("next_cursor")
    return tuple(users)


def notion_people_payload(prop: Dict[str, Any], value: str) -> Optional[Dict[str, Any]]:
    names = [name.strip() for name in re.split(r"[,/]", str(value or "")) if name.strip()]
    people = []
    for name in names:
        for user_name, user_id in fetch_notion_users():
            if normalize_for_match(name) in normalize_for_match(user_name):
                people.append({"id": user_id})
                break
    if not people:
        return None
    return {"people": people}


def build_notion_write_properties(
    database_id: str,
    request_data: Dict[str, Any],
    require_title: bool = False,
) -> Dict[str, Any]:
    properties = notion_property_schema(database_id)
    payload: Dict[str, Any] = {}

    title = request_data.get("title")
    if title:
        title_prop = notion_title_property(properties)
        payload[title_prop] = notion_property_payload(properties[title_prop], title)
    elif require_title:
        raise HTTPException(status_code=400, detail="Notion 항목 제목이 필요합니다.")

    field_specs = [
        ("status", ["상태", "status", "진행", "구상태", "이슈진행"], {"select", "status"}),
        ("owner", ["담당", "owner", "주담당", "이슈할당", "참여"], {"people", "rich_text"}),
        ("dueDate", ["목표일", "일정", "기한", "날짜", "신청일", "고객사요청일"], {"date"}),
        ("pageType", ["분류", "유형", "타입", "이슈유형", "세부유형"], {"select", "multi_select", "rich_text"}),
        ("customer", ["고객사"], {"select", "multi_select", "rich_text"}),
        ("media", ["매체", "플랫폼"], {"select", "multi_select", "rich_text"}),
        ("memo", ["메모", "요약", "설명"], {"rich_text"}),
    ]
    for key, keywords, allowed_types in field_specs:
        value = request_data.get(key)
        if value in (None, ""):
            continue
        matched = (
            find_notion_option_property(properties, keywords, str(value))
            if key == "status"
            else find_notion_property(properties, keywords, allowed_types)
        )
        if not matched:
            continue
        prop_name, prop = matched
        if prop.get("type") == "people":
            property_payload = notion_people_payload(prop, str(value))
        else:
            property_payload = notion_property_payload(prop, value)
        if property_payload:
            payload[prop_name] = property_payload

    for prop_name, value in (request_data.get("properties") or {}).items():
        prop = properties.get(prop_name)
        if not prop:
            continue
        property_payload = notion_property_payload(prop, value)
        if property_payload:
            payload[prop_name] = property_payload

    return payload


def notion_children_from_content(content: Optional[str]) -> List[Dict[str, Any]]:
    text = str(content or "").strip()
    if not text:
        return []
    return [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [
                    {"type": "text", "text": {"content": chunk}}
                    for chunk in [text[:1900]]
                ]
            },
        }
    ]


def create_notion_page(request: NotionPageWriteRequest) -> Dict[str, Any]:
    database_id = select_notion_database_id(request.databaseId or request.databaseLabel)
    body: Dict[str, Any] = {
        "parent": {"database_id": database_id},
        "properties": build_notion_write_properties(database_id, request.model_dump(), require_title=True),
    }
    children = notion_children_from_content(request.content)
    if children:
        body["children"] = children

    response = requests.post(
        f"{NOTION_API_BASE}/pages",
        headers=get_notion_headers(),
        json=body,
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Notion 항목 생성 실패: {response.status_code} {response.text[:500]}",
        )
    return normalize_notion_page(
        response.json(),
        include_blocks=True,
        database_id=database_id,
        database_label=fetch_notion_database_title(database_id),
    )


def update_notion_page(page_id: str, request: NotionPageUpdateRequest) -> Dict[str, Any]:
    page_response = requests.get(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=get_notion_headers(),
        timeout=20,
    )
    if page_response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Notion 항목 조회 실패: {page_response.status_code} {page_response.text[:300]}",
        )
    page = page_response.json()
    database_id = page.get("parent", {}).get("database_id", "")
    if not database_id:
        raise HTTPException(status_code=400, detail="DB 항목이 아닌 Notion 페이지는 수정할 수 없습니다.")

    properties = build_notion_write_properties(database_id, request.model_dump(), require_title=False)
    if not properties:
        raise HTTPException(status_code=400, detail="수정할 수 있는 Notion 속성이 없습니다.")

    response = requests.patch(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=get_notion_headers(),
        json={"properties": properties},
        timeout=30,
    )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Notion 항목 수정 실패: {response.status_code} {response.text[:500]}",
        )
    return normalize_notion_page(
        response.json(),
        include_blocks=True,
        database_id=database_id,
        database_label=fetch_notion_database_title(database_id),
    )


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


def build_notion_terms(text: str, constraints: Optional[Dict[str, Any]] = None) -> List[str]:
    cleaned = re.sub(r"[#,:;!?()\[\]{}\"']", " ", text)
    cleaned = re.sub(r"\d+\s*(개|건)\s*만?", " ", cleaned)
    notion_stopwords = {
        normalize_for_match(keyword)
        for keyword in {*STOPWORDS, *NOTION_QUERY_STOPWORDS, "관한", "관해서", "관하여"}
    }
    terms: List[str] = []
    for token in cleaned.split():
        normalized = normalize_search_token(token)
        if not normalized or len(normalized) < 2 or normalize_for_match(normalized) in notion_stopwords:
            continue
        if normalized.isdigit():
            continue
        terms.append(normalized)
    normalized_text = normalize_for_match(text)
    if "결재요청" in normalized_text and "결재요청" not in terms:
        terms.append("결재요청")
    if "등록" in normalized_text and "등록됨" not in terms:
        terms.append("등록됨")
    return list(dict.fromkeys(terms))[:8]


def trim_owner_adnominal(candidate: str) -> str:
    """"권한별인"처럼 관형형 어미가 붙어 잡힌 이름을 실제 담당자명으로 다듬는다.

    담당자 필터는 부분 일치로 비교하므로 어미가 남으면 아무 항목과도 맞지
    않는다. 실제 담당자 명단에 있는 어간일 때만 잘라내 오탐을 막는다."""
    name = str(candidate or "").strip()
    handlers, reporters = load_known_people()
    known = set(handlers) | set(reporters)
    if name in known:
        return name
    for suffix in ("인", "이"):
        if name.endswith(suffix) and len(name) > 2 and name[: -len(suffix)] in known:
            return name[: -len(suffix)]
    return name


def inherit_notion_context(recent_texts: List[str]) -> str:
    """후속 질문이 잃어버린 Notion 조건(DB·상태)을 이전 턴에서 물려받는다.

    "노션에서 결재요청 상태인 항목 보여줘" 다음의 "담당자별로 묶어줘"는
    Mantis 쪽 조건 상속만으로는 상태와 소스를 모두 잃는다. 반환한 접두
    텍스트를 질문 앞에 붙이면 기존 조건 추출이 그대로 다시 인식한다.
    """
    if len(recent_texts) < 2:
        return ""
    latest = extract_notion_constraints(recent_texts[0])
    if latest.get("status") or latest.get("exact_title") or latest.get("excluded_statuses"):
        return ""
    for prior in recent_texts[1:]:
        prior_constraints = extract_notion_constraints(prior)
        parts = [
            value
            for value in (
                prior_constraints.get("database_label"),
                prior_constraints.get("status"),
            )
            if value
        ]
        if parts:
            # 소스가 Notion이라는 사실도 함께 물려줘야 Mantis로 새지 않는다.
            return " ".join([*parts, "노션"])
    return ""


def extract_notion_constraints(text: str) -> Dict[str, Any]:
    exact_title = extract_quoted_value(text) or ""
    constraint_text = remove_quoted_value(text) if exact_title else text
    normalized = normalize_for_match(constraint_text)
    status = ""
    excluded_statuses: List[str] = []
    for candidate in sorted(NOTION_STATUS_QUERY_TERMS, key=len, reverse=True):
        if normalize_for_match(candidate) in normalized:
            candidate_text = normalize_for_match(candidate)
            if re.search(
                re.escape(candidate_text) + r"(?:은|는|이|가)?(?:아닌|말고|제외|빼고)",
                normalized,
            ):
                excluded_statuses.append(candidate)
            elif not status:
                status = candidate

    group_by = extract_group_by(constraint_text)
    owner_text = re.sub(
        r"(?:담당자|담당)\s*(?:별(?:로)?|기준(?:으)?로|마다)",
        " ",
        constraint_text,
    )
    owner = ""
    # "담당자 미지정 항목만"은 미지정이라는 이름을 찾는 게 아니라 담당자가
    # 비어 있는 항목을 찾는 요청이다.
    unassigned_owner = bool(
        re.search(
            r"담당자?\s*(?:가|는|를|도)?\s*"
            r"(?:없는|없이|없음|미지정|미배정|안\s*정해진|지정\s*안\s*된|할당\s*안\s*된|배정\s*안\s*된)",
            constraint_text,
        )
    )
    owner_match = re.search(
        # 조사 "가"를 함께 소비하지 않으면 "담당자가"가 담당+자가로 쪼개져
        # "자가"가 이름으로 잡힌다.
        r"(?:담당자|담당|owner)\s*(?:가|는|은|를|을|=|:)?\s*([가-힣]{2,4}|[A-Za-z]{2,20})",
        owner_text,
        re.IGNORECASE,
    ) or re.search(
        # "담당자가 권한별인"에서 "자가"처럼 담당자라는 말 자체가 이름으로
        # 잘려 나오지 않도록 앞에 담당이 붙은 경우를 제외한다.
        r"(?<!담당)([가-힣]{2,4}|[A-Za-z]{2,20})\s*(?:님)?(?:이|가|은|는)?\s*(?:담당|소유|작성|등록|할당)",
        owner_text,
    )
    if owner_match and not unassigned_owner:
        candidate = owner_match.group(1)
        # 조사가 붙은 일반 명사가 이름으로 잡히지 않도록 어간까지 검증한다.
        if is_valid_person_name(candidate) and is_valid_person_name(
            strip_korean_particle(candidate)
        ):
            owner = trim_owner_adnominal(candidate)

    return {
        "database_label": extract_notion_database_label_from_text(constraint_text) or "",
        "exact_title": exact_title,
        "status": status,
        "excluded_statuses": excluded_statuses,
        "owner": owner,
        "unassigned_owner": unassigned_owner,
        "group_by": group_by,
    }


def merge_common_notion_constraints(
    notion_constraints: Dict[str, Any],
    constraints: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """공통 필터 계획을 Notion 검색/후처리 조건으로 연결한다."""
    merged = {
        **notion_constraints,
        "common_filter_clauses": normalize_filter_clauses((constraints or {}).get("filter_clauses") or []),
        "excluded_statuses": list(
            dict.fromkeys(
                [
                    *(notion_constraints.get("excluded_statuses") or []),
                    *((constraints or {}).get("excluded_statuses") or []),
                ]
            )
        ),
    }
    for clause in merged["common_filter_clauses"]:
        values = values_for_clause(clause.get("value"))
        if clause["field"] == "status":
            if clause["operator"] in {"not_equals", "not_in"}:
                merged["excluded_statuses"].extend(values)
            elif clause["operator"] in {"equals", "in"} and values and not merged.get("status"):
                merged["status"] = values[0]
        elif clause["field"] == "owner" and clause["operator"] in {"equals", "in"} and values and not merged.get("owner"):
            merged["owner"] = values[0]
        elif clause["field"] == "database" and clause["operator"] in {"equals", "contains"} and values and not merged.get("database_label"):
            merged["database_label"] = values[0]
        elif clause["field"] == "title" and clause["operator"] == "equals" and values and not merged.get("exact_title"):
            merged["exact_title"] = values[0]
    merged["excluded_statuses"] = list(dict.fromkeys(merged["excluded_statuses"]))
    return merged


def notion_page_matches_common_filters(page: Dict[str, Any], clauses: List[Dict[str, Any]]) -> bool:
    field_values = {
        "customer": page.get("customer") or page.get("search_text", ""),
        "platform": page.get("platform") or page.get("search_text", ""),
        "status": page.get("status", ""),
        "owner": page.get("owner", ""),
        "created_at": page.get("created_time", ""),
        "title": page.get("title", ""),
        "keyword": "\n".join([page.get("search_text", ""), page.get("content", "")]),
        "database": page.get("database_label", ""),
    }
    for clause in normalize_filter_clauses(clauses):
        if clause["field"] not in field_values:
            continue
        candidate_raw = str(field_values[clause["field"]] or "")
        candidate = normalize_for_match(candidate_raw)
        values = [normalize_for_match(value) for value in values_for_clause(clause.get("value"))]
        operator = clause["operator"]
        # customer/platform/keyword are represented inside a property search
        # document, so equality is intentionally compiled as membership.
        document_field = clause["field"] in {"customer", "platform", "keyword"}
        if operator in {"equals", "in"}:
            matched = any(value in candidate if document_field else value == candidate for value in values)
            if not matched:
                return False
        elif operator in {"not_equals", "not_in"}:
            matched = any(value in candidate if document_field else value == candidate for value in values)
            if matched:
                return False
        elif operator == "contains" and not any(value in candidate for value in values):
            return False
        elif operator == "not_contains" and any(value in candidate for value in values):
            return False
        elif operator == "exists" and not candidate:
            return False
        elif operator == "not_exists" and candidate:
            return False
        elif operator == "gte" and values and candidate_raw[:10] < str(clause.get("value"))[:10]:
            return False
        elif operator == "lte" and values and candidate_raw[:10] > str(clause.get("value"))[:10]:
            return False
        elif operator == "between" and len(values) == 2:
            date_value = candidate_raw[:10]
            raw_values = values_for_clause(clause.get("value"))
            if not date_value or not (raw_values[0] <= date_value <= raw_values[1]):
                return False
    return True


def notion_page_matches_constraints(page: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    if constraints.get("database_label") and normalize_for_match(constraints["database_label"]) not in normalize_for_match(
        page.get("database_label", "")
    ):
        return False
    if constraints.get("exact_title") and normalize_for_match(constraints["exact_title"]) not in normalize_for_match(
        page.get("title", "")
    ):
        return False
    if constraints.get("status") and normalize_for_match(constraints["status"]) not in normalize_for_match(
        page.get("status", "")
    ):
        return False
    if constraints.get("owner") and normalize_for_match(constraints["owner"]) not in normalize_for_match(
        page.get("owner", "")
    ):
        return False
    if constraints.get("unassigned_owner") and str(page.get("owner", "")).strip() not in {
        "",
        "담당자 미지정",
        "미지정",
    }:
        return False
    if any(
        normalize_for_match(status) == normalize_for_match(page.get("status", ""))
        for status in constraints.get("excluded_statuses", [])
    ):
        return False
    if not notion_page_matches_common_filters(page, constraints.get("common_filter_clauses", [])):
        return False
    return True


def notion_page_matches_and_enrich(page: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    """Apply cheap constraints first, then lazily resolve relation-backed owners."""
    without_owner = {**constraints, "owner": ""}
    if not notion_page_matches_constraints(page, without_owner):
        return False
    if constraints.get("owner") or constraints.get("group_by") == "owner":
        resolve_notion_page_owner(page)
    return notion_page_matches_constraints(page, constraints)


def notion_exact_match_score(page: Dict[str, Any], constraints: Dict[str, Any]) -> int:
    score = 0
    if constraints.get("database_label"):
        score += 30
    if constraints.get("exact_title"):
        score += 60
    if constraints.get("status"):
        score += 25
    if constraints.get("owner"):
        score += 20
    return score


def notion_page_match_score(page: Dict[str, Any], terms: List[str]) -> int:
    if not terms:
        return 1
    weighted_fields = [
        (page.get("title", ""), 6),
        (page.get("type", ""), 4),
        (page.get("status", ""), 3),
        (page.get("owner", ""), 2),
        (page.get("search_text", ""), 1),
    ]
    score = 0
    for term in terms:
        normalized_term = normalize_for_match(term)
        for value, weight in weighted_fields:
            if normalized_term in normalize_for_match(value):
                score += weight
                break
    return score


def notion_minimum_match_score(terms: List[str]) -> int:
    if not terms:
        return 1
    if len(terms) <= 2:
        return 1
    return 2


def notion_page_matches_terms(page: Dict[str, Any], terms: List[str]) -> bool:
    if not terms:
        return True
    normalized_search_text = normalize_for_match(page.get("search_text", ""))
    return all(normalize_for_match(term) in normalized_search_text for term in terms)


def notion_search_max_pages(constraints: Dict[str, Any]) -> int:
    # Structured filters and grouped summaries must scan the complete configured
    # databases; otherwise an arbitrary recent-300 window produces incomplete
    # counts and groups. Keep ordinary fuzzy searches bounded for latency.
    if (
        constraints.get("status")
        or constraints.get("owner")
        or constraints.get("group_by")
        or constraints.get("common_filter_clauses")
    ):
        return 30
    return 3


def notion_database_status_filter(
    database_id: str,
    status: str,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Return whether a DB has status-like select fields and an exact API filter.

    When the requested status is absent from every status-like option list, the
    caller can safely skip that database. If schema inspection fails, return
    ``False`` so the existing local-scan fallback remains available.
    """
    if not status:
        return False, None
    try:
        properties = fetch_notion_database_schema(database_id).get("properties", {})
    except (HTTPException, requests.RequestException):
        return False, None
    found_status_property = False
    target = normalize_for_match(status)
    for prop_name, prop in properties.items():
        normalized_name = normalize_for_match(prop_name)
        if "상태" not in normalized_name and "status" not in normalized_name:
            continue
        prop_type = prop.get("type")
        if prop_type not in {"select", "status"}:
            continue
        found_status_property = True
        options = (prop.get(prop_type) or {}).get("options", [])
        for option in options:
            option_name = str(option.get("name") or "")
            if normalize_for_match(option_name) == target:
                return True, {
                    "property": prop_name,
                    prop_type: {"equals": option_name},
                }
    return found_status_property, None


NOTION_BODY_SCAN_MAX_PER_DB = 30


def execute_notion_search(
    query_text: str,
    limit: int = 5,
    constraints: Optional[Dict[str, Any]] = None,
    planned_terms: Optional[List[str]] = None,
) -> Dict[str, Any]:
    settings = get_notion_settings()
    database_ids = settings.get("database_ids", [])
    if not settings["token"] or not database_ids:
        return {
            "source": "notion",
            "status": "skipped",
            "reason": "NOTION_API_TOKEN/NOTION_DATABASE_IDS가 설정되지 않았습니다.",
            "notion_pages": [],
            "meta": {"displayed_count": 0},
        }

    notion_constraints = merge_common_notion_constraints(
        extract_notion_constraints(query_text),
        constraints,
    )
    exact_title = notion_constraints.get("exact_title", "")
    terms = list(dict.fromkeys(planned_terms or build_notion_terms(query_text, constraints)))[:8]
    # Status/owner are structured filters. Leaving them in free-text terms makes
    # every field participate in an accidental AND query and hides valid rows.
    structured_values = {
        normalize_for_match(value)
        for value in (notion_constraints.get("status"), notion_constraints.get("owner"))
        if value
    }
    structured_values.update(
        normalize_for_match(value)
        for clause in notion_constraints.get("common_filter_clauses", [])
        for value in values_for_clause(clause.get("value"))
        if clause.get("field") not in {"title", "keyword"}
    )
    terms = [
        term
        for term in terms
        if not any(
            normalize_for_match(term) in value or value in normalize_for_match(term)
            for value in structured_values
        )
    ]
    title_terms = [
        term
        for term in terms
        if normalize_for_match(term)
        not in {normalize_for_match(status) for status in NOTION_STATUS_QUERY_TERMS}
    ]
    target_database_label = notion_constraints.get("database_label", "")
    pages: List[Dict[str, Any]] = []
    scanned_count = 0
    database_meta = []
    errors = []
    seen_page_ids: set[str] = set()
    max_pages = notion_search_max_pages(notion_constraints)
    page_size = 100

    try:
        for database_id in database_ids:
            database_label = fetch_notion_database_title(database_id)
            if target_database_label and normalize_for_match(target_database_label) not in normalize_for_match(database_label):
                continue
            database_found_count = 0
            body_scan_candidates: List[Dict[str, Any]] = []
            status_filter_supported, status_api_filter = notion_database_status_filter(
                database_id,
                notion_constraints.get("status", ""),
            )
            if notion_constraints.get("status") and status_filter_supported and not status_api_filter:
                database_meta.append(
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "found_count": 0,
                    }
                )
                continue
            try:
                title_property = notion_title_property(notion_property_schema(database_id))
            except HTTPException:
                title_property = ""
            if exact_title and not title_property:
                errors.append(
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "reason": "Notion title property could not be resolved",
                    }
                )
                database_meta.append(
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "found_count": 0,
                    }
                )
                continue
            if exact_title and title_property:
                exact_payload = {
                    "page_size": min(page_size, max(limit, 5)),
                    "filter": {"property": title_property, "title": {"contains": exact_title}},
                    "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                }
                try:
                    exact_response = notion_post_json(
                        f"{NOTION_API_BASE}/databases/{database_id}/query",
                        exact_payload,
                        timeout=45,
                        retries=2,
                    )
                except requests.RequestException as exc:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "reason": f"Notion exact-title query failed: {exc}",
                        }
                    )
                    database_meta.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "found_count": 0,
                        }
                    )
                    continue
                if exact_response.status_code < 400:
                    exact_results = exact_response.json().get("results", [])
                    scanned_count += len(exact_results)
                    for raw_page in exact_results:
                        page = normalize_notion_page(
                            raw_page,
                            include_blocks=False,
                            database_id=database_id,
                            database_label=database_label,
                        )
                        page_id = page.get("id", "")
                        if page_id in seen_page_ids:
                            continue
                        if not notion_page_matches_and_enrich(page, notion_constraints):
                            continue
                        seen_page_ids.add(page_id)
                        page["match_score"] = (
                            notion_page_match_score(page, terms)
                            + notion_exact_match_score(page, notion_constraints)
                            + 60
                        )
                        page["matched_terms"] = terms
                        page["match_method"] = "notion_exact_title_filter"
                        pages.append(page)
                        database_found_count += 1
                else:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "reason": f"{exact_response.status_code} {exact_response.text[:300]}",
                        }
                    )
                # Exact-title requests are backed by Notion's server-side title
                # filter. Do not fall through to a multi-thousand-row database
                # scan when a literal title is absent or one database is slow.
                database_meta.append(
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "found_count": database_found_count,
                    }
                )
                continue
            if title_terms and title_property:
                try:
                    title_filter = {
                        "and": [
                            {"property": title_property, "title": {"contains": term}}
                            for term in title_terms
                        ]
                    }
                    targeted_response = notion_post_json(
                        f"{NOTION_API_BASE}/databases/{database_id}/query",
                        {
                            "page_size": page_size,
                            "filter": title_filter,
                            "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                        },
                        timeout=45,
                        retries=2,
                    )
                    if targeted_response.status_code < 400:
                        targeted_results = targeted_response.json().get("results", [])
                        scanned_count += len(targeted_results)
                        for raw_page in targeted_results:
                            page = normalize_notion_page(
                                raw_page,
                                include_blocks=False,
                                database_id=database_id,
                                database_label=database_label,
                            )
                            page_id = page.get("id", "")
                            if page_id in seen_page_ids:
                                continue
                            if not notion_page_matches_and_enrich(page, notion_constraints):
                                continue
                            seen_page_ids.add(page_id)
                            page["match_score"] = (
                                notion_page_match_score(page, terms)
                                + notion_exact_match_score(page, notion_constraints)
                                + 20
                            )
                            page["matched_terms"] = terms
                            page["match_method"] = "notion_title_filter"
                            pages.append(page)
                            database_found_count += 1
                except (HTTPException, requests.RequestException) as exc:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "reason": f"Notion targeted-title query failed: {exc}",
                        }
                    )
            cursor = None
            for _ in range(max_pages):
                payload: Dict[str, Any] = {
                    "page_size": page_size,
                    "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
                }
                if status_api_filter:
                    payload["filter"] = status_api_filter
                if cursor:
                    payload["start_cursor"] = cursor

                try:
                    response = notion_post_json(
                        f"{NOTION_API_BASE}/databases/{database_id}/query",
                        payload,
                        timeout=45,
                        retries=2,
                    )
                except requests.RequestException as exc:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "reason": f"Notion database scan failed: {exc}",
                        }
                    )
                    break
                if response.status_code >= 400:
                    errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "reason": f"{response.status_code} {response.text[:300]}",
                        }
                    )
                    break

                data = response.json()
                results = data.get("results", [])
                scanned_count += len(results)
                for raw_page in results:
                    page = normalize_notion_page(
                        raw_page,
                        include_blocks=False,
                        database_id=database_id,
                        database_label=database_label,
                    )
                    page_id = page.get("id", "")
                    if page_id in seen_page_ids:
                        continue
                    if not notion_page_matches_and_enrich(page, notion_constraints):
                        continue
                    match_score = notion_page_match_score(page, terms)
                    if notion_page_matches_terms(page, terms):
                        seen_page_ids.add(page_id)
                        page["match_score"] = match_score + notion_exact_match_score(page, notion_constraints)
                        page["matched_terms"] = [
                            term
                            for term in terms
                            if normalize_for_match(term)
                            in normalize_for_match(page.get("search_text", ""))
                        ]
                        pages.append(page)
                        database_found_count += 1
                    elif terms and len(body_scan_candidates) < NOTION_BODY_SCAN_MAX_PER_DB:
                        body_scan_candidates.append(page)
                if not data.get("has_more"):
                    break
                cursor = data.get("next_cursor")

            if terms and database_found_count < limit and body_scan_candidates:
                for page in body_scan_candidates:
                    page_id = page.get("id", "")
                    if page_id in seen_page_ids:
                        continue
                    if not notion_page_matches_and_enrich(page, notion_constraints):
                        continue
                    block_text = fetch_notion_page_blocks(page_id, max_blocks=20)
                    if not block_text:
                        continue
                    body_search_text = f"{page.get('search_text', '')}\n{block_text}"
                    if not all(
                        normalize_for_match(term) in normalize_for_match(body_search_text)
                        for term in terms
                    ):
                        continue
                    seen_page_ids.add(page_id)
                    page["content"] = "\n\n".join(
                        part for part in (page.get("properties_text", ""), block_text) if part
                    ).strip()
                    page["match_score"] = notion_page_match_score(page, terms) - 5
                    page["matched_terms"] = [
                        term
                        for term in terms
                        if normalize_for_match(term) in normalize_for_match(body_search_text)
                    ]
                    page["match_method"] = "notion_body_scan"
                    pages.append(page)
                    database_found_count += 1
                    if database_found_count >= limit:
                        break
            database_meta.append(
                {
                    "database_id": database_id,
                    "database_label": database_label,
                    "found_count": database_found_count,
                }
            )
    except requests.RequestException as exc:
        return {
            "source": "notion",
            "status": "error",
            "reason": f"Notion connection failed: {exc}",
            "notion_pages": [],
            "meta": {"displayed_count": 0},
        }

    pages.sort(
        key=lambda page: (page.get("match_score", 0), page.get("last_edited_time", "")),
        reverse=True,
    )
    deduplicated_pages = deduplicate_notion_pages_by_id(pages)
    limited_pages = deduplicated_pages[:limit]
    if os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() not in {"0", "false", "no"}:
        for page in limited_pages:
            # notion_body_scan 단계에서 이미 본문을 조회한 페이지는 재조회하지 않는다.
            if page.get("match_method") == "notion_body_scan" and page.get("content"):
                continue
            block_text = fetch_notion_page_blocks(page.get("id", ""), max_blocks=20)
            if block_text:
                page["content"] = "\n\n".join(
                    part for part in (page.get("properties_text", ""), block_text) if part
                ).strip()
    status = "partial_error" if errors and limited_pages else "error" if errors else "ok"

    return {
        "source": "notion",
        "status": status,
        "reason": "; ".join(
            f"{error['database_label']}: {error['reason']}" for error in errors
        )[:500]
        if errors
        else None,
        "meta": {
            "displayed_count": len(limited_pages),
            "candidate_count": len(deduplicated_pages),
            "scanned_count": scanned_count,
            "terms": terms[:6],
            "constraints": notion_constraints,
            "database_ids": database_ids,
            "databases": database_meta,
            "errors": errors,
        },
        "notion_pages": limited_pages,
    }


def deduplicate_notion_pages_by_id(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduplicated_pages: List[Dict[str, Any]] = []
    seen_result_page_ids = set()
    for page in pages:
        page_id = str(page.get("id") or "")
        if not page_id or page_id in seen_result_page_ids:
            continue
        seen_result_page_ids.add(page_id)
        deduplicated_pages.append(page)
    return deduplicated_pages


def collect_retrieved_notion_pages(retrieval_result: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    pages = []
    seen_page_ids = set()
    for stage in retrieval_result.get("stages", []):
        for page in stage.get("notion_pages", []):
            page_id = page.get("id")
            if not page_id or page_id in seen_page_ids:
                continue
            seen_page_ids.add(page_id)
            pages.append(page)
            if len(pages) >= limit:
                return pages
    return pages


def build_sources_from_notion_pages(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "notion",
            "source": "notion",
            "page_id": page.get("id"),
            "title": page.get("title", "제목 없음"),
            "database_label": page.get("database_label", "Notion DB"),
            "status": page.get("status", "상태 미상"),
            "owner": page.get("owner", "담당자 미지정"),
            "due_date": page.get("due_date", ""),
            "updated_at": page.get("last_edited_time", ""),
            "preview": page.get("content", "")[:320],
            "url": page.get("url", ""),
            "detail": page,
        }
        for page in pages
    ]


def build_sources_from_single_notion_page(page: Dict[str, Any]) -> List[Dict[str, Any]]:
    return build_sources_from_notion_pages([page])


def extract_notion_write_fields(text: str) -> Dict[str, Any]:
    fields: Dict[str, Any] = {}
    direct_status = re.search(r"(완료|보관|진행중|진행 중|등록됨|결재요청|결재완료|종료된이슈|할당된이슈)\s*(?:로|으로)", text)
    if direct_status:
        fields["status"] = direct_status.group(1).strip()
    else:
        status_match = re.search(r"(?:상태|진행|구상태)\s*(?:를|을|은|는)?\s*([가-힣A-Za-z0-9()_-]+)", text)
        if status_match:
            fields["status"] = status_match.group(1).strip()

    owner_match = re.search(r"(?:담당자|담당|주담당)\s*(?:를|을|은|는)?\s*([가-힣A-Za-z]{2,20})", text)
    if owner_match:
        fields["owner"] = owner_match.group(1).strip()

    date_match = re.search(r"(?:기한|목표일|일정|신청일)\s*(?:을|를|은|는)?\s*(\d{4}-\d{2}-\d{2})", text)
    if date_match:
        fields["dueDate"] = date_match.group(1)

    memo_match = re.search(r"(?:메모|요약|설명)\s*(?:을|를|은|는)?\s*[:：]?\s*(.+)", text)
    if memo_match:
        fields["memo"] = memo_match.group(1).strip()
    return fields


def extract_notion_database_label_from_text(text: str) -> Optional[str]:
    normalized_text = normalize_for_match(text)
    if "이슈리스트" in normalized_text:
        return "이슈리스트"
    if "주요이슈" in normalized_text or "주요이슈현황" in normalized_text:
        return "주요이슈"
    return None


def clean_notion_create_title(text: str) -> str:
    quoted = extract_quoted_value(text)
    if quoted:
        return quoted
    cleaned = re.sub(r"노션|notion|노선", " ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:에|에서)?\s*(?:항목|페이지|이슈)?\s*(?:추가|등록|생성|만들어|만들기|작성)(?:해줘|해|해볼래|해줘볼래)?", " ", cleaned)
    cleaned = re.sub(r"(?:이슈리스트|주요이슈현황|주요이슈)(?:_?테스트)?\s*(?:에|에서)?", " ", cleaned)
    cleaned = re.sub(r"(?:상태|진행|구상태|담당자|담당|주담당|기한|목표일|일정|신청일|메모|요약|설명)\s*[:：]?.*$", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" -:：")


def clean_notion_update_target(text: str) -> str:
    quoted = extract_quoted_value(text)
    if quoted:
        return quoted
    cleaned = re.sub(r"노션|notion|노선", " ", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"(?:이슈리스트|주요이슈현황|주요이슈)(?:_?테스트)?\s*(?:에|에서)?", " ", cleaned)
    cleaned = re.sub(r"(?:상태|진행|구상태|담당자|담당|주담당|기한|목표일|일정|신청일|메모|요약|설명)\s*[:：]?.*$", " ", cleaned)
    cleaned = re.sub(r"(?:수정|변경|바꿔|업데이트|완료|보관|진행중|진행 중|등록됨|결재요청|결재완료)(?:해줘|해|해볼래|해줘볼래|로|으로)?", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip(" -:：")


def notion_write_intent(text: str) -> Optional[str]:
    normalized_text = normalize_for_match(text)
    if not any(keyword in normalized_text for keyword in ("노션", "notion", "노선")):
        return None
    command_suffix = r"(?:해줘|해주세요|해볼래|해줘볼래|해라|하자|할래|해)"
    if re.search(rf"(?:추가|등록|생성|만들|작성){command_suffix}", normalized_text):
        return "create"
    if re.search(rf"(?:수정|변경|업데이트){command_suffix}|바꿔(?:줘|주세요|볼래)?", normalized_text):
        return "update"
    return None


def should_run_notion_write(text: str) -> bool:
    return notion_write_intent(text) is not None


NOTION_WRITE_CONFIRM_HEADER_CREATE = "📝 다음 내용으로 Notion에 새 항목을 만들까요?"


NOTION_WRITE_CONFIRM_HEADER_UPDATE = "📝 다음 내용으로 기존 Notion 항목을 수정할까요?"


NOTION_WRITE_CONFIRM_PHRASES = (
    "네", "예", "응", "ㅇㅇ", "진행해줘", "진행해", "실행해줘", "실행해",
    "그렇게 해줘", "확인", "맞아", "승인", "좋아", "진행",
)


NOTION_WRITE_CANCEL_PHRASES = (
    "아니", "아니오", "아니요", "취소", "하지마", "하지 마", "그만", "취소해줘", "취소할래",
)


NOTION_WRITE_CONFIRM_MAX_LEN = 12


def is_notion_write_confirm_phrase(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > NOTION_WRITE_CONFIRM_MAX_LEN:
        return False
    normalized = normalize_for_match(stripped)
    return any(normalize_for_match(phrase) in normalized for phrase in NOTION_WRITE_CONFIRM_PHRASES)


def is_notion_write_cancel_phrase(text: str) -> bool:
    stripped = text.strip()
    if not stripped or len(stripped) > NOTION_WRITE_CONFIRM_MAX_LEN:
        return False
    normalized = normalize_for_match(stripped)
    return any(normalize_for_match(phrase) in normalized for phrase in NOTION_WRITE_CANCEL_PHRASES)


def find_pending_notion_write_command(messages: List[ChatMessage]) -> Optional[str]:
    """직전 assistant 턴이 Notion 쓰기 확인 미리보기였다면, 그 확인을 유발한
    원본 사용자 명령(2턴 전 사용자 메시지)을 반환한다. 없으면 None."""
    if len(messages) < 3:
        return None
    original_cmd, preview_assistant, _latest_user = messages[-3:]
    if preview_assistant.role != "assistant" or original_cmd.role != "user":
        return None
    preview_text = (preview_assistant.content or "").strip()
    if not (
        preview_text.startswith(NOTION_WRITE_CONFIRM_HEADER_CREATE)
        or preview_text.startswith(NOTION_WRITE_CONFIRM_HEADER_UPDATE)
    ):
        return None
    return original_cmd.content


def resolve_notion_write_action(text: str, write_intent: str) -> Dict[str, Any]:
    """실행 가능 여부까지 검증한 쓰기 작업 정보를 반환한다.

    바로 실행 가능하면 {"ready": True, ...세부 정보}를, 실행할 수 없으면
    {"ready": False, "response": {...}} 형태로 사유를 담아 반환한다. 미리보기
    생성과 실제 실행이 같은 검증 로직을 공유하도록 분리했다."""
    if write_intent == "create":
        database_label = extract_notion_database_label_from_text(text)
        title = clean_notion_create_title(text)
        if not title:
            return {
                "ready": False,
                "response": {
                    "content": "Notion에 생성할 항목 제목을 찾지 못했습니다. 예: 노션에 \"장애보고 양식 점검\" 추가해줘",
                    "sources": [],
                    "raw": {"mode": "notion_create_missing_title"},
                },
            }
        fields = extract_notion_write_fields(text)
        return {
            "ready": True,
            "action": "create",
            "database_label": database_label,
            "title": title,
            "fields": fields,
        }

    fields = extract_notion_write_fields(text)
    if not fields:
        return {
            "ready": False,
            "response": {
                "content": "수정할 Notion 필드를 찾지 못했습니다. 예: 노션에서 \"휴가\" 상태를 완료로 바꿔줘",
                "sources": [],
                "raw": {"mode": "notion_update_missing_fields"},
            },
        }

    target = clean_notion_update_target(text)
    if not target:
        return {
            "ready": False,
            "response": {
                "content": "수정할 Notion 항목을 찾지 못했습니다. 항목 제목이나 키워드를 따옴표로 지정해 주세요.",
                "sources": [],
                "raw": {"mode": "notion_update_missing_target"},
            },
        }

    search_result = execute_notion_search(target, 5, {})
    # execute_notion_search는 단일 결과 dict({"notion_pages": [...]})를 반환하고
    # "stages" 키가 없다. collect_retrieved_notion_pages는 여러 stage를 순회하는
    # 함수라 여기 그대로 넘기면 항상 빈 리스트가 나와 수정 대상을 못 찾는
    # 기존 버그가 있었다. notion_pages를 직접 읽어야 한다.
    pages = (search_result.get("notion_pages") or [])[:5]
    if not pages:
        return {
            "ready": False,
            "response": {
                "content": f"Notion에서 '{target}' 조건에 맞는 항목을 찾지 못했습니다.",
                "sources": [],
                "retrieval": search_result,
                "raw": {"mode": "notion_update_not_found"},
            },
        }
    if len(pages) > 1:
        return {
            "ready": False,
            "response": {
                "content": (
                    f"'{target}'로 수정 후보가 {len(pages)}건 검색되었습니다. "
                    "잘못 수정하지 않도록 항목 제목을 더 구체적으로 입력해 주세요."
                ),
                "sources": build_sources_from_notion_pages(pages),
                "retrieval": search_result,
                "raw": {"mode": "notion_update_ambiguous"},
            },
        }

    return {
        "ready": True,
        "action": "update",
        "fields": fields,
        "target_page": pages[0],
        "search_result": search_result,
    }


NOTION_WRITE_FIELD_LABELS = {"status": "상태", "owner": "담당자", "dueDate": "기한", "memo": "메모"}


def build_notion_write_preview(text: str, write_intent: str) -> Dict[str, Any]:
    resolved = resolve_notion_write_action(text, write_intent)
    if not resolved["ready"]:
        return resolved["response"]

    if resolved["action"] == "create":
        fields = resolved["fields"]
        lines = [
            NOTION_WRITE_CONFIRM_HEADER_CREATE,
            "",
            f"- DB: {resolved['database_label'] or 'Notion DB'}",
            f"- 제목: {resolved['title']}",
            f"- 상태: {fields.get('status') or '등록됨'}",
        ]
        for key in ("owner", "dueDate", "memo"):
            if fields.get(key):
                lines.append(f"- {NOTION_WRITE_FIELD_LABELS[key]}: {fields[key]}")
        lines.extend(["", "이대로 진행하려면 '네', 취소하려면 '취소'라고 답해주세요."])
        return {
            "content": "\n".join(lines),
            "sources": [],
            "raw": {"mode": "notion_write_pending", "action": "create"},
        }

    fields = resolved["fields"]
    page = resolved["target_page"]
    changed_labels = [NOTION_WRITE_FIELD_LABELS[key] for key in NOTION_WRITE_FIELD_LABELS if fields.get(key)]
    lines = [
        NOTION_WRITE_CONFIRM_HEADER_UPDATE,
        "",
        f"- DB: {page.get('database_label', 'Notion DB')}",
        f"- 대상: {page.get('title', '제목 없음')}",
        f"- 변경 항목: {', '.join(changed_labels) or '속성'}",
    ]
    for key, label in NOTION_WRITE_FIELD_LABELS.items():
        if fields.get(key):
            lines.append(f"  · {label}: {fields[key]}")
    lines.extend(["", "이대로 진행하려면 '네', 취소하려면 '취소'라고 답해주세요."])
    return {
        "content": "\n".join(lines),
        "sources": build_sources_from_notion_pages([page]),
        "retrieval": resolved["search_result"],
        "raw": {"mode": "notion_write_pending", "action": "update"},
    }


def execute_notion_write_command(text: str) -> Dict[str, Any]:
    write_intent = notion_write_intent(text)
    if not write_intent:
        return {
            "content": "확인하려던 원래 요청을 다시 해석하지 못했습니다. 요청을 다시 입력해 주세요.",
            "sources": [],
            "raw": {"mode": "notion_write_reparse_failed"},
        }
    resolved = resolve_notion_write_action(text, write_intent)
    if not resolved["ready"]:
        return resolved["response"]

    if resolved["action"] == "create":
        fields = resolved["fields"]
        page = create_notion_page(
            NotionPageWriteRequest(
                databaseLabel=resolved["database_label"],
                title=resolved["title"],
                status=fields.get("status") or "등록됨",
                owner=fields.get("owner"),
                dueDate=fields.get("dueDate"),
                memo=fields.get("memo"),
            )
        )
        return {
            "content": (
                "✅ Notion 항목을 생성했습니다.\n\n"
                f"- DB: {page.get('database_label', 'Notion DB')}\n"
                f"- 제목: {page.get('title', '제목 없음')}\n"
                f"- 상태: {page.get('status', '상태 미상')}\n"
                f"- 담당자: {page.get('owner', '담당자 미지정')}"
            ),
            "sources": build_sources_from_single_notion_page(page),
            "raw": {"mode": "notion_create"},
        }

    fields = resolved["fields"]
    page = update_notion_page(resolved["target_page"]["id"], NotionPageUpdateRequest(**fields))
    changed_labels = [NOTION_WRITE_FIELD_LABELS[key] for key in NOTION_WRITE_FIELD_LABELS if fields.get(key)]
    return {
        "content": (
            "✅ Notion 항목을 수정했습니다.\n\n"
            f"- DB: {page.get('database_label', 'Notion DB')}\n"
            f"- 제목: {page.get('title', '제목 없음')}\n"
            f"- 변경: {', '.join(changed_labels) or '속성'}\n"
            f"- 현재 상태: {page.get('status', '상태 미상')}\n"
            f"- 담당자: {page.get('owner', '담당자 미지정')}"
        ),
        "sources": build_sources_from_single_notion_page(page),
        "retrieval": resolved["search_result"],
        "raw": {"mode": "notion_update"},
    }


def run_notion_write_command(text: str, messages: List[ChatMessage]) -> Optional[Dict[str, Any]]:
    pending_command = find_pending_notion_write_command(messages)
    if pending_command:
        if is_notion_write_cancel_phrase(text):
            return {
                "content": "🚫 Notion 작업을 취소했습니다.",
                "sources": [],
                "raw": {"mode": "notion_write_cancelled"},
            }
        if is_notion_write_confirm_phrase(text):
            return execute_notion_write_command(pending_command)
        # 확인/취소 문구가 아니면 대기 중이던 작업은 그대로 소멸시킨다(서버에 상태를
        # 남기지 않으므로 다시 언급하지 않는 한 실행되지 않는다). 현재 입력은 아래에서
        # 새 명령으로 다시 판별한다.

    write_intent = notion_write_intent(text)
    if not write_intent:
        return None
    return build_notion_write_preview(text, write_intent)


def should_return_deterministic_notion_list(text: str, retrieval_result: Dict[str, Any]) -> bool:
    plan_source = str((retrieval_result.get("intent_plan") or {}).get("source", ""))
    if plan_source == "both":
        return False
    if not should_run_notion_search(text) and plan_source not in {"notion", "both"}:
        return False
    normalized_text = normalize_for_match(text)
    has_list_intent = any(
        normalize_for_match(keyword) in normalized_text
        for keyword in ISSUE_LIST_INTENT_KEYWORDS
    )
    return has_list_intent and any(stage.get("source") == "notion" for stage in retrieval_result.get("stages", []))


def notion_page_owner_names(page: Dict[str, Any]) -> List[str]:
    names = [part.strip() for part in str(page.get("owner") or "").split(",") if part.strip()]
    return names or ["담당자 미지정"]


def format_grouped_notion_answer(
    pages: List[Dict[str, Any]],
    notion_stage: Dict[str, Any],
    group_by: str,
) -> str:
    if group_by != "owner":
        return ""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for page in pages:
        for owner in notion_page_owner_names(page):
            groups.setdefault(owner, []).append(page)

    meta = notion_stage.get("meta", {})
    total_matches = int(meta.get("candidate_count", len(pages)))
    lines = [f"📊 조건에 맞는 Notion 항목은 총 {total_matches}건입니다. 담당자별로 정리했습니다."]
    if total_matches > len(pages):
        lines.append(f"현재 조회 범위에서는 최근 수정순 {len(pages)}건까지 담당자별로 표시합니다.")
    lines.append("")
    for owner in sorted(groups, key=lambda value: (value == "담당자 미지정", value)):
        owner_pages = groups[owner]
        lines.append(f"**{owner} — {len(owner_pages)}건**")
        for page in owner_pages:
            meta_items = [
                page.get("database_label", "Notion DB"),
                f"상태 {page.get('status', '상태 미상')}",
            ]
            if page.get("due_date"):
                meta_items.append(f"기한 {page['due_date']}")
            if page.get("last_edited_time"):
                meta_items.append(f"수정 {page['last_edited_time']}")
            lines.append(f"- **{page.get('title', '제목 없음')}** · {' · '.join(meta_items)}")
        lines.append("")
    return "\n".join(lines).rstrip()


def format_deterministic_notion_answer(
    query_text: str,
    retrieval_result: Dict[str, Any],
    limit: int,
) -> str:
    pages = collect_retrieved_notion_pages(retrieval_result, limit)
    notion_stage = next(
        (stage for stage in retrieval_result.get("stages", []) if stage.get("source") == "notion"),
        {},
    )
    if notion_stage.get("status") == "skipped":
        return (
            "Notion 연동 설정이 아직 비어 있습니다.\n\n"
            "- `.env`에 `NOTION_API_TOKEN`을 입력하세요.\n"
            "- `.env`에 `NOTION_DATABASE_IDS`를 입력하세요. 두 DB ID는 쉼표로 구분합니다.\n"
            "- Notion DB 우측 상단 공유 메뉴에서 Integration을 추가하세요."
        )
    if notion_stage.get("status") == "error":
        return f"Notion 조회 중 오류가 발생했습니다.\n\n{notion_stage.get('reason', '원인 미상')}"
    if not pages:
        return (
            "🔍 Notion DB에서는 조건에 맞는 항목을 찾지 못했어요. "
            "키워드를 바꾸거나 조회할 DB를 좁혀서 다시 찾아볼까요?"
        )

    notion_constraints = notion_stage.get("meta", {}).get("constraints", {})
    group_by = notion_constraints.get("group_by") or extract_group_by(query_text)
    grouped_answer = format_grouped_notion_answer(pages, notion_stage, str(group_by or ""))
    if grouped_answer:
        return grouped_answer

    terms = notion_stage.get("meta", {}).get("terms", [])
    database_labels = [
        item.get("database_label", "")
        for item in notion_stage.get("meta", {}).get("databases", [])
        if item.get("database_label")
    ]
    criteria = f"검색 기준: Notion DB"
    if database_labels:
        criteria += f"({', '.join(database_labels)})"
    if terms:
        criteria += f", 키워드={', '.join(terms)}"
    structured_labels = [
        filter_clause_label(clause)
        for clause in notion_constraints.get("common_filter_clauses", [])
        if clause.get("field") not in {"keyword"}
    ]
    if structured_labels:
        criteria += f", 조건={'; '.join(structured_labels)}"
    if notion_constraints.get("unassigned_owner"):
        # 필터가 적용됐는데 기준에 안 보이면 결과가 왜 줄었는지 알 수 없다.
        criteria += ", 담당자=미지정"
    criteria += ", 정렬: 최근 수정순"

    count_intent = any(
        marker in normalize_for_match(query_text)
        for marker in ("몇개", "몇건", "건수", "총몇")
    )
    total_matches = int(notion_stage.get("meta", {}).get("candidate_count", len(pages)))
    if count_intent:
        intro = f"📊 조건에 맞는 Notion 항목은 총 {total_matches}건입니다. 대표 항목을 함께 표시합니다."
    elif total_matches > len(pages):
        # 화면엔 limit만큼만 표시되지만 실제로는 더 많은 후보가 있는 경우,
        # 사용자가 잘려 나간 나머지를 인지하도록 총 후보 수를 함께 알려준다.
        intro = (
            f"🔍 Notion에서 조건에 맞는 항목이 총 {total_matches}건 있어요. "
            f"최근 수정순 상위 {len(pages)}건을 표시합니다. 더 보려면 '더 보여줘'라고 말해주세요."
        )
    else:
        intro = f"🔍 Notion에서 관련 항목 {len(pages)}건을 찾았어요. 최근 수정순으로 정리했습니다."
    lines = [intro, "", criteria, ""]
    for index, page in enumerate(pages, start=1):
        meta_items = [
            f"DB {page.get('database_label', 'Notion DB')}",
            f"상태 {page.get('status', '상태 미상')}",
            f"담당 {page.get('owner', '담당자 미지정')}",
        ]
        if page.get("due_date"):
            meta_items.append(f"기한 {page['due_date']}")
        if page.get("last_edited_time"):
            meta_items.append(f"수정 {page['last_edited_time']}")
        lines.extend(
            [
                f"{index}. **{page.get('title', '제목 없음')}**",
                f"   - {' · '.join(meta_items)}",
            ]
        )

    return "\n".join(lines)
