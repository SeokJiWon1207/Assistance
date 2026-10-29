"""Notion 검색 근거와 결정형 답변을 포맷한다."""

import re
from datetime import datetime

from typing import Any, Dict, List, Optional

from backend.common import extract_quoted_value, has_lookup_request, normalize_for_match, remove_quoted_value
from backend.intent.constants import ISSUE_ANALYSIS_INTENT_KEYWORDS, ISSUE_LIST_INTENT_KEYWORDS
from backend.notion.client import NOTION_CONTENT_INCOMPLETE
from backend.notion.search import collect_retrieved_notion_pages, notion_requested_information_fields
from backend.notion.query_text import notion_structural_query
from backend.notion.mapping import KST
from backend.query_filters import filter_clause_label
from backend.query_planning import extract_group_by, has_count_intent


def append_notion_completeness_notice(
    content: str, retrieval_result: Optional[Dict[str, Any]],
) -> str:
    """모델·비교·대체 답변에 확인된 Notion 검색·본문 범위의 한계를 표시한다."""
    stages = [stage for stage in (retrieval_result or {}).get("stages", [])
              if stage.get("source") == "notion"]
    search_incomplete = any(
        (stage.get("meta") or {}).get("search_complete") is False
        or stage.get("status") in {"partial_error", "error"}
        or bool((stage.get("meta") or {}).get("errors"))
        for stage in stages
    )
    content_incomplete = any(
        (stage.get("meta") or {}).get("content_complete") is False
        or bool((stage.get("meta") or {}).get("content_incomplete_page_ids"))
        or any(page.get("content_incomplete") or NOTION_CONTENT_INCOMPLETE in str(page.get("content") or "")
               for page in stage.get("notion_pages", []))
        for stage in stages
    )
    notices = []
    # 결정형 Notion 목록을 재사용한 대체 답변에는 이미 검색 범위 안내가 있다.
    scope_already_explained = any(notice in content for notice in (
        "일부 항목을 아직 확인하지 못해 전체 건수와 다를 수 있습니다.",
        "전체 항목을 확인한 결과는 아닙니다.",
        "전체 조회가 끝나지 않아 결과가 없다고 확정할 수 없습니다.",
    ))
    if search_incomplete and not scope_already_explained:
        notices.append("Notion 전체 검색이 완료되지 않아, 확인한 범위의 근거로 답변했습니다.")
    if content_incomplete:
        notices.append("일부 문서 본문을 끝까지 읽지 못해 누락된 내용이 있을 수 있습니다.")
    missing_notices = [notice for notice in notices if notice not in content]
    return content + "\n\n" + " ".join(missing_notices) if missing_notices else content


NOTION_CONTENT_ANSWER_MARKERS = (
    "상세",
    "자세히",
    "본문",
    "내용",
    "요약",
    "설명",
    "정리",
    "방법",
    "절차",
    "원인",
    "이유",
    "왜",
    "어떻게",
)
NOTION_STRONG_CONTENT_MARKERS = tuple(
    marker for marker in NOTION_CONTENT_ANSWER_MARKERS
    if marker not in {"원인", "이유", "왜", "어떻게"}
)


def is_simple_notion_title_lookup(text: str) -> bool:
    """명시한 제목을 찾는 요청만 모델 플래너를 생략한다."""
    if not extract_quoted_value(text):
        return False
    intent = normalize_for_match(remove_quoted_value(text))
    return bool(re.fullmatch(
        r"(?:노션|notion)(?:에서|에)?(?:제목(?:이|은|으로)?|이름(?:이|은|으로)?)?"
        r"(?:인|이라는|라는)?(?:문서|항목|페이지|자료)?(?:을|를)?"
        r"(?:찾아(?:서)?|검색(?:해서|해(?:서)?)?|조회(?:해(?:서)?)?|보여)(?:줘|주세요)?"
        r"(?:알려(?:줘|주세요))?[.!?]*", intent,
    ))


def has_deterministic_notion_list_intent(text: str) -> bool:
    """Return whether Notion can answer without reading page bodies."""
    # Quoted text is a title filter, not the requested answer form. For
    # example, ``'배포 요청 문서' 상세 내용`` needs the page body even
    # though the title itself contains a list-intent keyword.
    intent_text = notion_structural_query(text)
    normalized_text = normalize_for_match(intent_text)
    if extract_group_by(intent_text):
        return True
    if notion_requested_information_fields(intent_text):
        return False
    if re.search(
        r"(?:문서|항목|페이지|자료|업무)(?:가|은|는)?있(?:어(?:요)?|나요|는지)",
        normalized_text,
    ):
        return True
    # 문서 주제의 '방법'과 목록을 정리하는 명령은 본문 요약 요청이 아니다.
    explicit_content = any(word in normalized_text for word in ("본문", "내용", "요약", "설명", "상세", "자세히"))
    if not explicit_content and any(word in normalized_text for word in ("목록", "리스트")):
        return True
    if not explicit_content and "정리" in normalized_text and any(
        word in normalized_text for word in ("항목", "이슈", "마감")
    ):
        return True
    if any(
        normalize_for_match(marker) in normalized_text
        for marker in NOTION_STRONG_CONTENT_MARKERS
    ):
        return False
    # Count questions such as ``원인별 건수`` are still deterministic. A
    # strong content verb above (``요약``, ``절차``, ``설명``) takes priority
    # for mixed requests like ``몇 개를 요약해줘``.
    if has_count_intent(intent_text):
        return True
    if any(
        normalize_for_match(marker) in normalized_text
        for marker in (*NOTION_CONTENT_ANSWER_MARKERS, *ISSUE_ANALYSIS_INTENT_KEYWORDS)
    ):
        return False
    if any(marker in normalized_text for marker in ("목록", "리스트")):
        return True
    if re.search(
        r"(?:등록|접수|생성|작성)(?:된|한|된거|된게|한거|한게).*(?:뭐야|뭐가있|뭐있)",
        normalized_text,
    ):
        return True
    return has_lookup_request(intent_text) or any(
        normalize_for_match(keyword) in normalized_text
        for keyword in ISSUE_LIST_INTENT_KEYWORDS
    )


def build_sources_from_notion_pages(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "type": "notion",
            "source": "notion",
            "page_id": page.get("id"),
            "title": page.get("title", "제목 없음"),
            "database_label": page.get("database_label", "Notion DB"),
            "status": page.get("status", "상태 미상"),
            "status_property": page.get("status_property", ""),
            "owner": page.get("owner", "담당자 미지정"),
            "owner_role": page.get("owner_role", "담당자"),
            "due_date": page.get("due_date", ""),
            "deployment_date": page.get("deployment_date", ""),
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
    return has_deterministic_notion_list_intent(text) and has_notion_stage


def format_notion_missing_information_answer(text: str, pages: List[Dict[str, Any]]) -> str:
    """단일 공지에 명시된 빈 항목은 확정 여부나 목표일을 추론하지 않고 답한다."""
    fields = notion_requested_information_fields(text)
    if not fields or len(pages) != 1:
        return ""
    content = str(pages[0].get("content") or "")
    evidence = content + "\n" + str(pages[0].get("properties_text") or "")
    labels = {"location": "장소|위치", "schedule": "일시|일정|날짜|시간"}
    for field in fields:
        values = re.findall(rf"^[^\S\r\n]*(?:{labels[field]})[^\S\r\n]*[:：][^\S\r\n]*(.*)$", evidence, re.MULTILINE)
        if not values or any(value.strip() for value in values):
            return ""
    names = "·".join("장소" if field == "location" else "일시" for field in fields)
    title = pages[0].get('title', '해당 공지')
    if pages[0].get("content_incomplete") or NOTION_CONTENT_INCOMPLETE in content:
        answer = f"**{title}**에서 확인한 ‘{names}’ 항목은 비어 있습니다. 본문 일부가 미확인되어 확정 {names}는 확인이 필요합니다."
    else:
        answer = f"**{title}**의 확정 {names}는 원문에 미기재되어 있습니다."
    if "location" in fields:
        candidates = list(dict.fromkeys(
            re.sub(r"^\s*\d+[.)]\s*", "", item).strip()
            for match in re.finditer(r"^\s*회식장소\s*[:：]\s*(.+)$", content, re.MULTILINE)
            for item in match.group(1).split(",") if item.strip()
        ))
        if candidates:
            label = "원문에 기록된 투표 후보" if "투표" in content else "본문에 기재된 회식장소"
            answer += f"\n\n{label}: " + ", ".join(candidates) + "."
    return answer


def notion_page_owner_names(page: Dict[str, Any]) -> List[str]:
    names = [part.strip() for part in str(page.get("owner") or "").split(",") if part.strip()]
    return names or ["담당자 미지정"]


def notion_result_sort_label(constraints: Dict[str, Any]) -> str:
    created_at_filtered = any(
        clause.get("field") == "created_at"
        for clause in constraints.get("common_filter_clauses", [])
    )
    created_order = (constraints.get("sort") or {}).get("field") == "created_at"
    return "최근 등록순" if created_at_filtered or created_order else "최근 수정순"


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
    sort_label = notion_result_sort_label(meta.get("constraints", {}))
    total_matches = int(meta.get("candidate_count", len(pages)))
    scope = "조회한 범위에서 확인된" if meta.get("search_complete") is False else "총"
    lines = [f"📊 조건에 맞는 Notion 항목은 {scope} {total_matches}건입니다. 담당자별로 정리했습니다."]
    if meta.get("search_complete") is False:
        lines.append("일부 항목을 아직 확인하지 못해 전체 건수와 다를 수 있습니다.")
    if total_matches > len(pages):
        lines.append(f"현재 조회 범위에서는 {sort_label} {len(pages)}건까지 담당자별로 표시합니다.")
    lines.append("")
    for owner in sorted(groups, key=lambda value: (value == "담당자 미지정", value)):
        owner_pages = groups[owner]
        lines.append(f"**{owner} — {len(owner_pages)}건**")
        for page in owner_pages:
            meta_items = [
                page.get("database_label", "Notion DB"),
                f"상태 {page.get('status', '상태 미상')}",
            ]
            if page.get("owner_role") == "기여자":
                meta_items.append("기여자 기준")
            if page.get("due_date"):
                meta_items.append(f"기한 {page['due_date']}")
            if page.get("deployment_date"):
                meta_items.append(f"운영반영 {page['deployment_date']}")
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
            "Notion에서 조회할 수 있는 DB가 아직 없습니다.\n\n"
            "- `.env`에 `NOTION_API_TOKEN`을 입력하세요.\n"
            "- 조회할 Notion DB 우측 상단 `연결(Connections)`에 해당 Integration을 추가하세요. "
            "연결된 DB는 자동으로 검색합니다.\n"
            "- 특정 DB만 검색하려면 `.env`의 `NOTION_DATABASE_IDS`에 DB ID를 쉼표로 구분해 입력하세요."
        )
    if notion_stage.get("status") == "error":
        return f"Notion 조회 중 오류가 발생했습니다.\n\n{notion_stage.get('reason', '원인 미상')}"
    scope_meta = notion_stage.get("meta", {})
    catalog_time_notice = ""
    if scope_meta.get("tool") == "notion_recent_catalog":
        try:
            stamp = datetime.fromisoformat(scope_meta["snapshot_as_of"].replace("Z", "+00:00"))
            catalog_time_notice = f"목록 기준 시각: {stamp.astimezone(KST):%Y-%m-%d %H:%M} KST"
        except (KeyError, TypeError, ValueError):
            pass
    scope_notice = ""
    if scope_meta.get("incomplete_reasons") == ["page_limit"] and scope_meta.get("scan_limit_per_database"):
        scope_notice = (
            f"검색 범위는 제목 검색 결과와 DB별 최근 수정 항목 최대 {scope_meta['scan_limit_per_database']}건입니다. "
            "전체 항목을 확인한 결과는 아닙니다."
        )
    if not pages:
        if notion_stage.get("meta", {}).get("search_complete") is False:
            return "Notion에서 조회한 범위에는 일치하는 항목이 없습니다. " + (scope_notice or "전체 조회가 끝나지 않아 결과가 없다고 확정할 수 없습니다.")
        if catalog_time_notice:
            return f"문서 목록 저장본에서 조건에 맞는 항목을 찾지 못했습니다.\n\n{catalog_time_notice}"
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
    # 개별 결과 카드가 출처 DB를 이미 표시한다. 전체 검색 범위의 DB명을
    # 기준 줄에 모두 나열하면 자손 DB가 늘어날수록 답변의 대부분을 차지한다.
    criteria = "검색 기준: Notion"
    exact_title = str(notion_constraints.get("exact_title") or "").strip()
    if exact_title:
        criteria += f", 정확 제목={exact_title}"
    elif terms:
        criteria += f", 키워드={', '.join(terms)}"
    structured_labels = [
        filter_clause_label(clause)
        for clause in notion_constraints.get("common_filter_clauses", [])
        # 담당자는 바로 아래의 사용자 친화적인 라벨로 한 번만 표시한다.
        if clause.get("field") not in {"keyword", "owner"}
    ]
    if structured_labels:
        criteria += f", 조건={'; '.join(structured_labels)}"
    if notion_constraints.get("unassigned_owner"):
        # 필터가 적용됐는데 기준에 안 보이면 결과가 왜 줄었는지 알 수 없다.
        criteria += ", 담당자=미지정"
    if notion_constraints.get("status"):
        criteria += f", 상태={notion_constraints['status']}"
    if notion_constraints.get("excluded_statuses"):
        criteria += f", 제외 상태={', '.join(notion_constraints['excluded_statuses'])}"
    if notion_constraints.get("owner"):
        criteria += f", 담당자={notion_constraints['owner']}"
    status_typo = notion_constraints.get("status_typo")
    if status_typo:
        # 조용히 바꾸지 않고 무엇을 무엇으로 봤는지 밝힌다.
        criteria += f", 표기 보정={status_typo[0]}→{status_typo[1]}"
    sort_label = notion_result_sort_label(notion_constraints)
    criteria += f", 정렬: {sort_label}"

    count_intent = any(
        marker in normalize_for_match(query_text)
        for marker in ("몇개", "몇건", "건수", "총몇")
    )
    total_matches = int(notion_stage.get("meta", {}).get("candidate_count", len(pages)))
    if notion_stage.get("meta", {}).get("search_complete") is False or notion_stage.get("status") == "partial_error":
        intro = (
            f"🔍 조회한 범위에서 조건에 맞는 Notion 항목 {total_matches}건을 확인했습니다. "
            f"{sort_label}으로 {len(pages)}건을 표시합니다. "
            + (scope_notice or "일부 항목을 아직 확인하지 못해 전체 건수와 다를 수 있습니다.")
        )
    elif count_intent:
        intro = f"📊 조건에 맞는 Notion 항목은 총 {total_matches}건입니다. 대표 항목을 함께 표시합니다."
    elif scope_meta.get("title_search_only"):
        intro = f"🔍 Notion에서 제목에 검색어가 포함된 항목을 {sort_label}으로 {len(pages)}건 표시합니다."
    elif scope_meta.get("candidate_count_complete") is False and scope_meta.get("result_window_complete"):
        intro = f"🔍 Notion에서 조건에 맞는 항목을 {sort_label}으로 {len(pages)}건 표시합니다."
    elif total_matches > len(pages):
        # 화면엔 limit만큼만 표시되지만 실제로는 더 많은 후보가 있는 경우,
        # 사용자가 잘려 나간 나머지를 인지하도록 총 후보 수를 함께 알려준다.
        intro = (
            f"🔍 Notion에서 조건에 맞는 항목이 총 {total_matches}건 있어요. "
            f"{sort_label} 상위 {len(pages)}건을 표시합니다. 더 보려면 '더 보여줘'라고 말해주세요."
        )
    else:
        intro = f"🔍 Notion에서 관련 항목 {len(pages)}건을 찾았어요. {sort_label}으로 정리했습니다."
    if scope_meta.get("coverage_basis") == "api_properties":
        if scope_meta.get("tool") == "notion_recent_catalog":
            criteria += ", 검색 범위: 문서 목록 저장본 (표시 문서의 현재 속성 재확인)"
            if catalog_time_notice:
                criteria += f", {catalog_time_notice}"
        else:
            criteria += ", 검색 범위: 저장본의 전체 제목·속성 (제목 일치 후보의 현재 속성 재확인)"
        if scope_meta.get("excluded_document_count"):
            criteria += " (삭제·범위 밖 이동·접근 불가 문서 제외)"
    if scope_meta.get("coverage_basis") == "api_readable":
        criteria += ", 검색 범위: 저장본의 API 지원 제목·속성·본문 전체"
        excluded = scope_meta.get("excluded_element_count", 0)
        if excluded:
            criteria += " (API 미지원 요소·접근 불가 연결 원본 제외)"
        if scope_meta.get("excluded_document_count"):
            criteria += " (삭제·범위 밖 이동·접근 불가 문서 제외)"
    lines = [intro, "", criteria, ""]
    for index, page in enumerate(pages, start=1):
        meta_items = [
            f"DB {page.get('database_label', 'Notion DB')}",
            f"상태 {page.get('status', '상태 미상')}",
            f"{page.get('owner_role', '담당')} {page.get('owner', '담당자 미지정')}",
        ]
        if page.get("status_property") and page["status_property"] != "상태":
            meta_items.append(f"상태 필드 {page['status_property']}")
        if page.get("due_date"):
            meta_items.append(f"기한 {page['due_date']}")
        if page.get("deployment_date"):
            meta_items.append(f"운영반영 {page['deployment_date']}")
        if sort_label == "최근 등록순" and (page.get("registration_date") or page.get("created_time")):
            meta_items.append(f"등록 {page.get('registration_date') or page['created_time']}")
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
    "has_deterministic_notion_list_intent",
    "should_return_deterministic_notion_list",
    "notion_page_owner_names",
    "format_grouped_notion_answer",
    "format_deterministic_notion_answer",
]
