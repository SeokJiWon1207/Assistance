"""페이지의 매칭·점수·정렬과 본문 근거를 값만으로 판정한다."""
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from backend.utils.text import normalize_for_match
from backend.notion.page_ownership import segment_belongs_to_page
from backend.query_filters import clause_supported, matches_source_filters, normalize_filter_clauses, values_for_clause


def notion_page_matches_common_filters(page: Dict[str, Any], clauses: List[Dict[str, Any]]) -> bool:
    from backend.customer_identity import customer_value_matches
    field_values = {
        "customer": page.get("customer") or page.get("search_text", ""),
        "platform": page.get("platform") or page.get("search_text", ""),
        "status": page.get("status", ""),
        "owner": page.get("owner", ""),
        "created_at": (
            page.get("registration_dates", [page.get("registration_date", "")])
            if page.get("registration_date_properties") or page.get("registration_date_property")
            else page.get("created_time", "")
        ),
        "due_at": page.get("due_date", ""),
        "deployed_at": page.get("deployment_date", ""),
        "title": page.get("title", ""),
        "keyword": "\n".join([page.get("search_text", ""), page.get("content", "")]),
        "database": page.get("database_label", ""),
    }
    for clause in normalize_filter_clauses(clauses):
        if not clause_supported("notion", clause) or clause["field"] not in field_values:
            continue
        if clause["field"] in {"created_at", "due_at", "deployed_at"}:
            if not matches_source_filters("notion", field_values, [clause]):
                return False
            continue
        if clause['field'] == 'customer' and clause['operator'] in {'equals', 'in', 'not_equals', 'not_in'}:
            matched = customer_value_matches(field_values['customer'], values_for_clause(clause.get('value')),
                                             text_fallback=not bool(page.get('customer')))
            if matched != (clause['operator'] in {'equals', 'in'}):
                return False
            continue
        candidate_raw = str(field_values[clause["field"]] or "")
        if clause["field"] == "owner" and candidate_raw.strip() in {"담당자 미지정", "미지정"}:
            candidate_raw = ""
        candidate = normalize_for_match(candidate_raw)
        values = [normalize_for_match(value) for value in values_for_clause(clause.get("value"))]
        operator = clause["operator"]
        # customer/platform/keyword are represented inside a property search
        # document, so equality is intentionally compiled as membership.
        document_field = clause["field"] in {"customer", "platform", "keyword"}
        owner_names = {normalize_for_match(name) for name in candidate_raw.split(",")}
        if operator in {"equals", "in"}:
            matched = any(
                value in owner_names if clause["field"] == "owner"
                else value in candidate if document_field else value == candidate for value in values
            )
            if not matched:
                return False
        elif operator in {"not_equals", "not_in"}:
            matched = any(
                value in owner_names if clause["field"] == "owner"
                else value in candidate if document_field else value == candidate for value in values
            )
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
    if constraints.get("date_filter") and not matches_source_filters(
        "notion", {"due_at": page.get("due_date", ""), "deployed_at": page.get("deployment_date", "")},
        [constraints["date_filter"]],
    ):
        return False
    if constraints.get("database_label") and normalize_for_match(constraints["database_label"]) not in normalize_for_match(
        page.get("database_label", "")
    ):
        return False
    if constraints.get("exact_title") and normalize_for_match(constraints["exact_title"]) != normalize_for_match(
        page.get("title", "")
    ):
        return False
    requested_status = normalize_for_match(constraints.get("status", ""))
    if requested_status == "진행":
        requested_status = "진행중"
    if requested_status and requested_status != normalize_for_match(page.get("status", "")):
        return False
    if constraints.get("owner") and normalize_for_match(constraints["owner"]) not in {
        normalize_for_match(name) for name in str(page.get("owner", "")).split(",")
    }:
        return False
    if constraints.get("unassigned_owner") and str(page.get("owner", "")).strip() not in {
        "",
        "담당자 미지정",
        "미지정",
    }:
        return False
    if any(
        normalize_for_match(status) in {
            normalize_for_match(value)
            for value in [page.get("status", ""), *(page.get("status_fields") or {}).values()]
        }
        for status in constraints.get("excluded_statuses", [])
    ):
        return False
    if not notion_page_matches_common_filters(page, constraints.get("common_filter_clauses", [])):
        return False
    return True


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


def notion_page_sort_key(page: Dict[str, Any], *, created_at_filtered: bool) -> tuple[str, float, int]:
    """날짜와 전체 ISO 시각을 분리해 모든 DB에서 같은 타입으로 정렬한다."""
    if created_at_filtered:
        calendar_date = str(page.get("registration_date") or page.get("created_time") or "")[:10]
        raw_time = str(page.get("registration_time_raw") or page.get("created_time_raw") or page.get("created_time") or "")
    else:
        calendar_date = str(page.get("last_edited_time") or "")[:10]
        raw_time = str(page.get("last_edited_time_raw") or page.get("last_edited_time") or "")
    try:
        parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            local_registration = created_at_filtered and page.get("registration_time_raw")
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)) if local_registration else timezone.utc)
        timestamp = parsed.timestamp()
    except (ValueError, OverflowError):
        timestamp = 0.0
    return calendar_date, timestamp, int(page.get("match_score") or 0)


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


def match_indexed_body_candidate(
    page: Dict[str, Any], candidate: Dict[str, Any], remaining_constraints: Dict[str, Any],
    body_clauses: List[Dict[str, Any]], keyword_clauses: List[Dict[str, Any]], terms: List[str],
) -> Optional[Dict[str, Any]]:
    """이미 조회한 페이지·소유 블록의 유형, 부정 조건과 점수를 판정한다."""
    page_id = str(page.get("id") or "").replace("-", "")
    segments = [block for block in candidate["segments"]
                if segment_belongs_to_page(block, page_id)]
    # 명시적 본문 조건에는 속성/자식 문서의 텍스트를 섞지 않는다.
    body_text = "\n".join(str(block["text"]) for block in segments)
    page["content"] = "\n\n".join(part for part in (page.get("properties_text", ""), body_text) if part)
    if not candidate.get("complete"):
        page["content_incomplete"] = True
        if any(clause["operator"] in {"not_contains", "not_equals", "not_in", "not_exists"}
               for clause in keyword_clauses):
            return None
    search_text = f"{page.get('search_text', '')}\n{body_text}"
    if not all(normalize_for_match(term) in normalize_for_match(search_text) for term in terms):
        return None
    evidence = []
    for clause in body_clauses:
        matching = [block for block in segments
                    if (not clause["block_type"] or block["type"] == clause["block_type"])
                    and normalize_for_match(str(clause["value"])) in normalize_for_match(str(block["text"]))]
        if bool(matching) != (clause["operator"] == "contains"):
            break
        evidence.extend(matching)
    else:
        if not notion_page_matches_constraints(page, remaining_constraints):
            return None
        if not body_clauses:
            evidence = [block for block in segments if any(
                normalize_for_match(term) in normalize_for_match(str(block["text"])) for term in terms
            )]
            page["index_title_match"] = bool(terms) and all(
                normalize_for_match(term) in normalize_for_match(page.get("title", "")) for term in terms
            )
        page["matched_body_blocks"] = list({block["block_id"]: block for block in evidence}.values())[:10]
        page["match_method"] = "notion_body_index"
        page["matched_terms"] = terms
        page["match_score"] = notion_page_match_score(page, terms) - 5
        return page
    return None
