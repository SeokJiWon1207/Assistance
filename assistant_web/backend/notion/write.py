"""Notion 생성·수정·확인 흐름과 허용 DB 검증을 담당한다."""

import re
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException

from backend.common import extract_quoted_value, normalize_for_match, remove_quoted_value
from backend.config import (
    NOTION_API_BASE,
    get_notion_headers,
    get_notion_settings as _get_notion_settings,
)
from backend.notion.client import fetch_notion_database_title, fetch_notion_users
from backend.notion.actions import consume_action, save_action
from backend.notion.compat import compat_dependency
from backend.notion.constants import (
    NOTION_WRITE_CANCEL_PHRASES,
    NOTION_WRITE_CONFIRM_HEADER_CREATE,
    NOTION_WRITE_CONFIRM_HEADER_UPDATE,
    NOTION_WRITE_CONFIRM_MAX_LEN,
    NOTION_WRITE_CONFIRM_PHRASES,
    NOTION_WRITE_FIELD_LABELS,
)
from backend.notion.formatting import build_sources_from_notion_pages, build_sources_from_single_notion_page
from backend.notion.mapping import normalize_notion_page
from backend.notion.query_cache import clear_notion_query_cache
from backend.notion.schema import (
    ensure_configured_notion_database,
    find_notion_option_property,
    find_notion_property,
    notion_property_payload,
    notion_property_schema,
    notion_title_property,
    select_notion_database_id,
    validate_notion_page_id,
)
from backend.notion.search import execute_notion_search, extract_notion_database_label_from_text
from backend.schemas import ChatMessage, NotionPageUpdateRequest, NotionPageWriteRequest


def get_notion_settings() -> Dict[str, Any]:
    return compat_dependency("get_notion_settings", _get_notion_settings)()


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
        custom_prop = properties.get(prop_name)
        if not custom_prop:
            continue
        property_payload = notion_property_payload(custom_prop, value)
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


def create_notion_page(request: NotionPageWriteRequest, *, prepared_properties: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    database_id = select_notion_database_id(request.databaseId or request.databaseLabel)
    body: Dict[str, Any] = {
        "parent": {"database_id": database_id},
        "properties": prepared_properties if prepared_properties is not None else build_notion_write_properties(database_id, request.model_dump(), require_title=True),
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
    clear_notion_query_cache()
    return normalize_notion_page(
        response.json(),
        include_blocks=True,
        database_id=database_id,
        database_label=fetch_notion_database_title(database_id),
    )


def update_notion_page(page_id: str, request: NotionPageUpdateRequest, *, expected_database_id: Optional[str] = None, prepared_properties: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    page_id = validate_notion_page_id(page_id)
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
    ensure_configured_notion_database(database_id)
    if expected_database_id and database_id.replace("-", "") != expected_database_id.replace("-", ""):
        raise HTTPException(status_code=409, detail="확인 이후 문서의 DB가 변경되었습니다. 다시 요청해 주세요.")
    if page.get("archived") or page.get("in_trash"):
        raise HTTPException(status_code=409, detail="보관되거나 삭제된 문서는 수정하지 않습니다.")

    properties = prepared_properties if prepared_properties is not None else build_notion_write_properties(database_id, request.model_dump(), require_title=False)
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
    clear_notion_query_cache()
    return normalize_notion_page(
        response.json(),
        include_blocks=True,
        database_id=database_id,
        database_label=fetch_notion_database_title(database_id),
    )


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
    # 제목 안의 '수정해주세요'는 명령이 아니라 검색 대상의 일부다.
    normalized_text = normalize_for_match(remove_quoted_value(text))
    if not any(keyword in normalized_text for keyword in ("노션", "notion", "노선")):
        return None
    if re.search(r"바꿔서(?:다시)?(?:찾아|조회|검색)", normalized_text):
        return None
    command_suffix = r"(?:해줘|해주세요|해볼래|해줘볼래|해라|하자|할래|해)"
    if re.search(rf"(?:추가|등록|생성|만들|작성){command_suffix}", normalized_text):
        return "create"
    if re.search(rf"(?:수정|변경|업데이트){command_suffix}|바꿔(?:줘|주세요|볼래)?", normalized_text):
        return "update"
    return None


def should_run_notion_write(text: str) -> bool:
    return notion_write_intent(text) is not None


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


def build_notion_write_preview(text: str, write_intent: str, *, resolved: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    resolved = resolved or resolve_notion_write_action(text, write_intent)
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
        lines.extend(["", "10분 안에 '네'로 확인하거나 '취소'라고 답해주세요."])
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
    lines.extend(["", "10분 안에 '네'로 확인하거나 '취소'라고 답해주세요."])
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

    return execute_resolved_notion_action(resolved)


def execute_resolved_notion_action(resolved: Dict[str, Any]) -> Dict[str, Any]:
    if resolved["action"] == "create":
        fields = resolved["fields"]
        page = create_notion_page(
            NotionPageWriteRequest(
                databaseId=resolved.get("database_id"),
                databaseLabel=resolved["database_label"],
                title=resolved["title"],
                status=fields.get("status") or "등록됨",
                owner=fields.get("owner"),
                dueDate=fields.get("dueDate"),
                memo=fields.get("memo"),
            ),
            prepared_properties=resolved.get("prepared_properties"),
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
    page = update_notion_page(resolved["target_page"]["id"], NotionPageUpdateRequest(**fields),
                             expected_database_id=resolved["target_page"].get("database_id"),
                             prepared_properties=resolved.get("prepared_properties"))
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


def run_notion_write_command(
    text: str,
    messages: List[ChatMessage],
    *,
    allow_write: bool = True,
    user_uid: str = "",
) -> Optional[Dict[str, Any]]:
    pending = compat_dependency("find_pending_notion_write_command", find_pending_notion_write_command)(messages)
    action_id = messages[-2].notionActionId if len(messages) >= 2 and messages[-2].role == "assistant" else ""
    if (pending or action_id) and (is_notion_write_confirm_phrase(text) or is_notion_write_cancel_phrase(text)):
        if not allow_write or not user_uid:
            return {"content": "Notion 수정은 로그인한 매니저 이상만 가능합니다.", "sources": [],
                    "raw": {"mode": "notion_write_forbidden"}}
        return consume_action(action_id, user_uid, execute_resolved_notion_action,
                              cancel=is_notion_write_cancel_phrase(text))
    write_intent = notion_write_intent(text)
    if not write_intent:
        return None
    if not allow_write or not user_uid:
        return {"content": "Notion 수정은 로그인한 매니저 이상만 가능합니다.", "sources": [],
                "raw": {"mode": "notion_write_forbidden"}}
    resolved = resolve_notion_write_action(text, write_intent)
    if not resolved["ready"]:
        return resolved["response"]
    if resolved["action"] == "create":
        resolved["database_id"] = select_notion_database_id(resolved["database_label"])
    database_id = resolved.get("database_id") or resolved["target_page"].get("database_id")
    database_id = ensure_configured_notion_database(str(database_id or ""))
    request_data = dict(resolved["fields"])
    if resolved["action"] == "create":
        request_data.update(title=resolved["title"], status=request_data.get("status") or "등록됨")
    properties = build_notion_write_properties(database_id, request_data, require_title=resolved["action"] == "create")
    if not properties:
        raise HTTPException(status_code=400, detail="수정할 수 있는 Notion 속성이 없습니다.")
    schema = notion_property_schema(database_id)
    # 속성 이름이 바뀌더라도 다른 속성으로 재해석하지 않도록 안정적인 ID를 저장한다.
    resolved["prepared_properties"] = {str(schema[name].get("id") or name): value for name, value in properties.items()}
    preview = build_notion_write_preview(text, write_intent, resolved=resolved)
    preview["notionActionId"] = save_action(user_uid, resolved)
    return preview


__all__ = [
    "extract_notion_database_label_from_text",
    "notion_people_payload",
    "build_notion_write_properties",
    "notion_children_from_content",
    "create_notion_page",
    "update_notion_page",
    "extract_notion_write_fields",
    "clean_notion_create_title",
    "clean_notion_update_target",
    "notion_write_intent",
    "should_run_notion_write",
    "is_notion_write_confirm_phrase",
    "is_notion_write_cancel_phrase",
    "find_pending_notion_write_command",
    "resolve_notion_write_action",
    "build_notion_write_preview",
    "execute_notion_write_command",
    "run_notion_write_command",
]
