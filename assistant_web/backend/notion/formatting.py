"""Notion 검색 근거와 결정형 답변을 포맷한다."""

from typing import Any, Dict, List

from backend.common import extract_quoted_value, normalize_for_match, remove_quoted_value
from backend.intent.constants import ISSUE_LIST_INTENT_KEYWORDS
from backend.notion.search import collect_retrieved_notion_pages
from backend.query_filters import filter_clause_label
from backend.query_planning import extract_group_by


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


def should_return_deterministic_notion_list(text: str, retrieval_result: Dict[str, Any]) -> bool:
    plan_source = str((retrieval_result.get("intent_plan") or {}).get("source", ""))
    if plan_source == "both":
        return False
    has_notion_stage = any(stage.get("source") == "notion" for stage in retrieval_result.get("stages", []))
    if plan_source != "notion" and not has_notion_stage:
        return False
    # 인용된 제목은 검색 대상이지 사용자의 명령이 아니다. 제목에 "요청" 같은
    # 목록 의도 단어가 들어 있으면 ``상세 내용 정리해줘``도 목록 조회로 오인한다.
    # 정확 제목 뒤의 실제 명령만 보고 목록 여부를 판정한다.
    intent_text = remove_quoted_value(text) if extract_quoted_value(text) else text
    normalized_text = normalize_for_match(intent_text)
    if extract_quoted_value(text) and any(
        normalize_for_match(marker) in normalized_text
        for marker in ("상세", "자세히", "요약", "내용 정리", "정리해줘")
    ):
        return False
    has_list_intent = any(
        normalize_for_match(keyword) in normalized_text
        for keyword in ISSUE_LIST_INTENT_KEYWORDS
    )
    return has_list_intent and has_notion_stage


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
    notion_stage: Dict[str, Any] = next(
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
    criteria = "검색 기준: Notion DB"
    if database_labels:
        criteria += f"({', '.join(database_labels)})"
    exact_title = str(notion_constraints.get("exact_title") or "").strip()
    if exact_title:
        criteria += f", 정확 제목={exact_title}"
    elif terms:
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
    status_typo = notion_constraints.get("status_typo")
    if status_typo:
        # 조용히 바꾸지 않고 무엇을 무엇으로 봤는지 밝힌다.
        criteria += f", 표기 보정={status_typo[0]}→{status_typo[1]}"
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


__all__ = [
    "build_sources_from_notion_pages",
    "build_sources_from_single_notion_page",
    "should_return_deterministic_notion_list",
    "notion_page_owner_names",
    "format_grouped_notion_answer",
    "format_deterministic_notion_answer",
]
