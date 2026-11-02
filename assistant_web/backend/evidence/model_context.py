"""이미 조회한 근거로 모델 입력과 검증 출처를 구성한다. 추가 조회 없음."""
import re
from collections import Counter
from typing import Any, Dict, List, Optional
from backend.display import customer_name
from backend.utils.text import compact_text, extract_quoted_value, normalize_for_match, remove_quoted_value
from backend.mantis_status import display_mantis_status_label
from backend.notion.search import build_notion_terms
from backend.notion.matching import collect_retrieved_notion_pages
from backend.notion.formatting import build_sources_from_notion_pages
from backend.sources.mantis_fields import build_sources_from_issue_rows
from backend.evidence.retrieval_rows import collect_retrieved_issues, collect_retrieved_customer_groups, count_retrieved_issues

def _notion_model_content(page: Dict[str, Any], retrieval_result: Dict[str, Any]) -> Dict[str, Any]:
    """이미 읽은 원문에서 질문 근처를 발췌한다. 조회 범위와 발췌 범위는 구분한다."""
    from backend.notion.read_api import NOTION_CONTENT_INCOMPLETE
    from backend.query.notion_text import split_notion_body_query

    content = str(page.get("content") or "")
    max_chars = 520
    result = {
        "content": content,
        "content_excerpted": len(content) > max_chars,
        "content_incomplete": bool(page.get("content_incomplete") or NOTION_CONTENT_INCOMPLETE in content),
    }
    query = str(retrieval_result.get("query") or "")
    _subject, current_body_clauses = split_notion_body_query(query)
    title = str(page.get("title") or "")
    if normalize_for_match(extract_quoted_value(query)) == normalize_for_match(title):
        query = remove_quoted_value(query)
    title_terms = {normalize_for_match(term) for term in build_notion_terms(title)}
    generic_terms = {
        "본문", "내용", "코드", "표", "문서", "항목", "페이지", "자료", "설명", "요약",
        "동작", "부분", "조건", "관련", "그", "이", "body", "table", "code",
    }
    keyword_terms: List[str] = []
    body_clauses: List[Dict[str, Any]] = []
    matched_terms = list(page.get("matched_terms") or [])
    for stage in retrieval_result.get("stages", []):
        if stage.get("source") != "notion" or not any(
            item.get("id") == page.get("id") for item in stage.get("notion_pages", [])
        ):
            continue
        meta = stage.get("meta") or {}
        matched_terms.extend(meta.get("terms") or [])
        body_clauses.extend((meta.get("constraints") or {}).get("body_filter_clauses") or [])
        for clause in (meta.get("constraints") or {}).get("common_filter_clauses", []):
            if clause.get("field") == "keyword" and clause.get("operator") in {"contains", "equals", "in"}:
                value = clause.get("value")
                keyword_terms.extend(str(item) for item in (value if isinstance(value, list) else [value]) if item)

    def specific_terms(values: List[str]) -> List[str]:
        return list(dict.fromkeys(
            str(term) for term in values
            if len(str(term)) >= 2
            and normalize_for_match(str(term)) not in generic_terms | title_terms
        ))

    def matching_terms(values: List[str]) -> List[str]:
        return [term for term in specific_terms(values) if re.search(re.escape(term), content, re.IGNORECASE)]

    query_values = [str(clause["value"]) for clause in current_body_clauses
                    if clause.get("operator") == "contains"] + build_notion_terms(query)
    has_current_subject = bool(current_body_clauses or specific_terms(query_values))
    active_body_clauses = [
        clause for clause in body_clauses if clause.get("operator") == "contains"
        and (
            any(clause == current for current in current_body_clauses)
            if current_body_clauses else
            not has_current_subject or normalize_for_match(str(clause.get("value") or "")) in normalize_for_match(query)
        )
    ]
    # 같은 낱말이 속성/다른 종류의 블록에도 있어도 검색에서 검증한 원문을 우선한다.
    # 후속 질문이 바뀌면 이전 블록·조건을 그대로 재사용하지 않는다.
    matched_body_texts = list(dict.fromkeys(
        str(block["text"]) for block in page.get("matched_body_blocks") or []
        if str(block.get("owner_page_id") or block.get("page_id") or "").replace("-", "")
        == str(page.get("id") or "").replace("-", "")
        and block.get("text")
        and normalize_for_match(str(block["text"])) in normalize_for_match(content)
        and any(
            (not clause.get("block_type") or clause["block_type"] == block.get("type"))
            and normalize_for_match(str(clause["value"])) in normalize_for_match(str(block["text"]))
            for clause in active_body_clauses
        )
    ))
    if matched_body_texts:
        selected_content = "\n[…]\n".join(matched_body_texts)
        result["content_excerpted"] = result["content_excerpted"] or selected_content != content
        content = selected_content
        result["content"] = content
    if len(content) <= max_chars:
        return result

    if has_current_subject:
        keyword_terms = [term for term in keyword_terms if normalize_for_match(term) in normalize_for_match(query)]
    # 실제 질문에 맞는 구절이 있으면 이전 검색의 다른 코드·표를 끼워 넣지 않는다.
    literal_terms = [str(clause["value"]) for clause in [*current_body_clauses, *active_body_clauses]
                     if clause.get("operator") == "contains" and clause.get("value")
                     and re.search(re.escape(str(clause["value"])), content, re.IGNORECASE)]
    # 명시한 본문 리터럴은 제목과 같거나 '코드' 같은 일반 명사여도 그대로 보존한다.
    terms = list(dict.fromkeys(literal_terms + matching_terms(keyword_terms + query_values)))
    if not terms and not has_current_subject:
        terms = matching_terms(matched_terms)
    hits = [match.span() for term in terms[:8]
            if (match := re.search(re.escape(term), content, re.IGNORECASE))]
    groups: List[List[int]] = []
    for start, end in sorted(hits):
        if groups and end - groups[-1][0] <= 160:
            groups[-1][1] = max(groups[-1][1], end)
        else:
            groups.append([start, end])
    groups = groups[:3]
    if not groups:
        # 구체적인 질문 근거가 없을 때도 줄바꿈을 유지하고 발췌 여부를 알린다.
        result["content"] = content[:max_chars]
        return result

    separator = "\n[…]\n"
    window_size = (max_chars - len(separator) * (len(groups) - 1)) // len(groups)
    excerpts = []
    for hit_start, hit_end in groups:
        start = max(0, (hit_start + hit_end - window_size) // 2)
        end = min(len(content), start + window_size)
        start = max(0, end - window_size)
        # 가능한 한 완전한 줄을 보낸다. 한 줄 자체가 길면 일치 지점을 포함해 자른다.
        next_line = content.find("\n", start, hit_start)
        if next_line >= 0:
            start = next_line + 1
        previous_line = content.rfind("\n", hit_end, end)
        if previous_line >= 0:
            end = previous_line
        excerpts.append(content[start:end].strip("\r\n"))
    result["content"] = separator.join(excerpts)
    return result


def build_model_retrieval_context(retrieval_result: Dict[str, Any], limit: int) -> Dict[str, Any]:
    issue_limit = max(5, min(limit, 10))
    notion_limit = max(5, min(limit, 10))
    issues = collect_retrieved_issues(retrieval_result, issue_limit)
    notion_pages = collect_retrieved_notion_pages(retrieval_result, notion_limit)
    customer_groups = collect_retrieved_customer_groups(retrieval_result)

    return {
        "query": retrieval_result.get("query", ""),
        "keyword": retrieval_result.get("keyword", ""),
        "constraints": retrieval_result.get("constraints", {}),
        "limit": max(len(issues), len(notion_pages)),
        "requested_limit": limit,
        "provided_issue_count": len(issues),
        "issues_truncated": len(collect_retrieved_issues(retrieval_result, None)) > len(issues),
        "provided_notion_count": len(notion_pages),
        "notion_pages_truncated": len(collect_retrieved_notion_pages(retrieval_result, notion_limit + 1)) > len(notion_pages),
        "customer_counts": dict(Counter(customer_name(value) for value in customer_groups.values())),
        "customer_counts_complete": len(customer_groups) == count_retrieved_issues(retrieval_result),
        "stages": [
            {
                "source": stage.get("source"),
                "status": stage.get("status"),
                # 조회 ID 목록/스캔 건수는 답변 근거가 아니다. 특히 빈 결과에서
                # scanned_count를 일치 건수로 오독하거나 내부 로그를 출력하지 않게 한다.
                "meta": {key: value for key, value in stage.get("meta", {}).items()
                         if key in {"operation", "matched_count", "search_complete", "fetchedAt"}
                         or (stage.get("source") == "notion" and key == "content_complete")},
            }
            for stage in retrieval_result.get("stages", [])
        ],
        "issues": [
            {
                "id": issue.get("id"),
                "related_issue_ids": (issue.get("detail") or {}).get("related_issue_ids", []),
                "source_reference_ids": (issue.get("detail") or {}).get("source_reference_ids", []),
                "project": issue.get("project"),
                "customer_name": customer_name(issue.get("project")),
                "title": issue.get("summary"),
                "status": (issue.get("detail") or {}).get("status_label") or display_mantis_status_label(issue.get("status")),
                "priority": (issue.get("detail") or {}).get("priority_label") or issue.get("priority", ""),
                "severity": (issue.get("detail") or {}).get("severity_label") or issue.get("severity", ""),
                "handler": issue.get("handler"),
                "created_at": issue.get("created_at"),
                "updated_at": (issue.get("detail") or {}).get("updated_at", ""),
                "screen_number": (issue.get("detail") or {}).get("screen_number", ""),
                "media_type": (issue.get("detail") or {}).get("media_type", ""),
                "date_fields": (issue.get("detail") or {}).get("date_fields", {}),
                "resolution_fields": (issue.get("detail") or {}).get("resolution_fields", {}),
                "matching_evidence": (issue.get("detail") or {}).get("matching_evidence", []),
                "answer_evidence": (issue.get("detail") or {}).get("answer_evidence", {"complete": False, "excerpts": []}),
                "additional_information": compact_text((issue.get("detail") or {}).get("additional_information", ""), 620),
                "description": compact_text(
                    (issue.get("detail") or {}).get("description") or issue.get("preview", ""),
                    620,
                ),
            }
            for issue in issues
        ],
        "notion_pages": [
            {
                "id": page.get("id"),
                "title": page.get("title", "제목 없음"),
                "database": page.get("database_label", "Notion DB"),
                "status": page.get("status", "상태 미상"),
                "owner": page.get("owner", "담당자 미지정"),
                "due_date": page.get("due_date", ""),
                "updated_at": page.get("last_edited_time", ""),
                **_notion_model_content(page, retrieval_result),
            }
            for page in notion_pages
        ],
    }


def build_model_evidence_sources(retrieval_result: Optional[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """모델에 전달한 행과 같은 정렬·한도로 검증 근거를 만든다."""
    if not retrieval_result:
        return []
    evidence_limit = max(5, min(limit, 10))
    return [
        *build_sources_from_issue_rows(collect_retrieved_issues(retrieval_result, evidence_limit)),
        *build_sources_from_notion_pages(collect_retrieved_notion_pages(retrieval_result, evidence_limit)),
    ]
