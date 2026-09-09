"""검색 파이프라인 오케스트레이션(Layer 4).

질문·제약조건에 따라 Mantis/Notion/Chroma 소스 검색을 순차 실행하고
(run_retrieval_pipeline), 결과를 모델 컨텍스트·근거 목록으로 정리한다.

의존: sources/*, understanding.
"""

import re
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from backend.common import (
    compact_text,
    display_mantis_status_label,
    extract_chroma_date,
    extract_chroma_field,
    extract_project_from_chroma_text,
    extract_quoted_value,
    extract_title_from_chroma_text,
    normalize_for_match,
    remove_quoted_value,
)
from backend.query_filters import filter_audit_for_source
from backend.sources.chroma import execute_chroma_search
from backend.sources.mantis import (
    execute_mantis_filter_search,
    execute_mantis_id_lookup,
    execute_mantis_page_scan,
    execute_mantis_snapshot_search,
    get_mantis_snapshot_freshness,
    revalidate_mantis_snapshot_stage,
)
from backend.sources.notion import (
    build_notion_terms,
    collect_retrieved_notion_pages,
    execute_notion_search,
)
from backend.understanding import (
    COMBINED_SOURCE_MARKERS,
    EXPLICIT_MANTIS_MARKERS,
    EXPLICIT_NOTION_MARKERS,
    build_keyword_terms,
    build_similar_issue_search_text,
    extract_query_constraints,
    has_constraint,
    merge_query_plan_constraints,
    rule_based_search_intent,
    wants_similar_issue_search,
)


def source_specific_terms(query_text: str, source: str, constraints: Dict[str, Any]) -> List[str]:
    if source == "notion":
        terms = build_notion_terms(query_text, constraints)
        blocked = {
            *EXPLICIT_MANTIS_MARKERS,
            *COMBINED_SOURCE_MARKERS,
            "장애",
            "오류",
            "버그",
            "화면번호",
            "공수",
            "이력",
        }
    else:
        terms = build_keyword_terms(query_text, constraints)
        blocked = {
            *EXPLICIT_NOTION_MARKERS,
            *COMBINED_SOURCE_MARKERS,
            "공지",
            "회식",
            "휴가",
            "결재요청",
            "결재완료",
            "주요이슈현황",
            "이력",
        }
    normalized_blocked = {normalize_for_match(value) for value in blocked}
    filtered = [
        term
        for term in terms
        if not any(blocked in normalize_for_match(term) for blocked in normalized_blocked)
    ]
    if source == "notion" and "공지" in normalize_for_match(query_text) and "공지" not in filtered:
        filtered.insert(0, "공지")
    return list(dict.fromkeys(filtered))[:6]


def grounded_mantis_terms(
    query_text: str,
    constraints: Dict[str, Any],
    planned_terms: Optional[List[str]] = None,
) -> List[str]:
    """질문에 실제로 남는 Mantis 키워드만 플래너 결과에서 허용한다."""
    deterministic_terms = source_specific_terms(query_text, "mantis", constraints)
    if not planned_terms or not deterministic_terms:
        return deterministic_terms

    grounded = []
    normalized_deterministic = [normalize_for_match(term) for term in deterministic_terms]
    for term in planned_terms:
        normalized = normalize_for_match(str(term))
        if normalized and any(
            normalized == candidate
            or normalized in candidate
            or candidate in normalized
            for candidate in normalized_deterministic
        ):
            grounded.append(str(term))
    return list(dict.fromkeys(grounded or deterministic_terms))[:6]


def run_retrieval_pipeline(
    query_text: str,
    limit: int,
    tools: Dict[str, bool],
    intent_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    constraints = merge_query_plan_constraints(extract_query_constraints(query_text), intent_plan)
    stages = []
    plan_source = str((intent_plan or {}).get("source", ""))
    if plan_source not in {"notion", "mantis", "both", "general"}:
        plan_source = str(rule_based_search_intent(query_text, constraints).get("source") or "general")
    # 고객사별 최신 목록은 특정 검색어가 아니라 전체 이슈 집합이 대상이다.
    # 플래너가 "최근"·"하나씩" 같은 표현을 검색어로 되돌려도 적용하지 않는다.
    planned_terms = (
        []
        if (intent_plan or {}).get("group_by") == "customer"
        else (intent_plan or {}).get("terms") or []
    )
    notion_intent = plan_source in {"notion", "both"}
    mantis_intent = plan_source in {"mantis", "both"}
    normalized_query = normalize_for_match(query_text)
    explicit_notion = any(
        normalize_for_match(keyword) in normalized_query
        for keyword in ("notion", "노션", "노선")
    )
    planner_notion = plan_source == "notion"
    effective_constraints = {} if explicit_notion or planner_notion else constraints
    literal_notion_title = extract_quoted_value(query_text) if notion_intent else ""
    # A quoted Notion value is an exact document title, not a bag of customer,
    # platform, status, owner, or screen-number filters. Re-parse only the text
    # outside the quotes so an explicitly stated outer condition is preserved
    # without turning words inside the title into accidental AND filters.
    notion_constraints = (
        extract_query_constraints(remove_quoted_value(query_text))
        if literal_notion_title
        else constraints
    )
    notion_terms = planned_terms if plan_source == "notion" and planned_terms else None
    mantis_terms = (
        grounded_mantis_terms(query_text, effective_constraints, planned_terms)
        if plan_source == "mantis"
        else None
    )
    if plan_source == "both":
        notion_terms = source_specific_terms(query_text, "notion", effective_constraints)
        mantis_terms = source_specific_terms(query_text, "mantis", effective_constraints)
    keyword = " ".join(mantis_terms or build_keyword_terms(query_text, effective_constraints))
    notion_only = planner_notion
    similar_issue_search = wants_similar_issue_search(query_text, constraints)
    exact_issue_lookup = bool(constraints.get("issue_ids")) and not similar_issue_search and plan_source != "both"
    historical_snapshot_used = False
    structured_snapshot_eligible = bool(
        has_constraint(constraints)
        and (
            # 화면번호는 의미 검색보다 필드/제목의 정확 일치가 우선이다.
            # keyword 생성 여부와 무관하게 전체 구조화 스냅샷을 먼저 확인한다.
            constraints.get("screen_numbers")
            or
            constraints.get("created_at")
            or not keyword
            # 고객사에 매체나 상태까지 겹친 질의는 키워드/의미 검색이 후보를
            # 못 올려 결과가 0건이 되곤 한다(예: "KB증권 MTS 미종료"). 조건이
            # 이만큼 분명하면 키워드가 있어도 전체 스냅샷을 정확 조건으로 훑는다.
            or (
                constraints.get("customer_aliases")
                and (
                    constraints.get("platform_key")
                    or constraints.get("statuses")
                    or constraints.get("excluded_statuses")
                )
            )
        )
    )

    if tools.get("notion") and notion_intent:
        stages.append(
            execute_notion_search(
                query_text,
                limit,
                notion_constraints,
                planned_terms=notion_terms,
            )
        )

    if tools.get("mantis") and mantis_intent:
        if constraints.get("issue_ids"):
            try:
                id_stage = execute_mantis_id_lookup(constraints["issue_ids"], constraints)
                if similar_issue_search:
                    id_stage = {
                        **id_stage,
                        "source": "mantis_id_lookup_reference",
                        "issues_list": [],
                        "meta": {
                            **id_stage.get("meta", {}),
                            "reference_issues": id_stage.get("issues_list", []),
                            "reference_only": True,
                        },
                    }
                stages.append(id_stage)
            except HTTPException as exc:
                stages.append(
                    {
                        "source": "mantis_id_lookup_reference" if similar_issue_search else "mantis_id_lookup",
                        "status": "error",
                        "error_kind": "dependency",
                        "reason": exc.detail,
                        "issues_list": [],
                        "meta": {"displayed_count": 0},
                    }
                )

        if similar_issue_search and tools.get("vectorDb"):
            reference_issues: List[Dict[str, Any]] = []
            for stage in stages:
                reference_issues.extend(stage.get("meta", {}).get("reference_issues") or [])
            if reference_issues:
                base_issue = reference_issues[0]
                similar_constraints = {
                    **constraints,
                    "issue_ids": [],
                    "exclude_issue_ids": list(constraints.get("issue_ids") or []),
                    "max_issue_id_exclusive": int(base_issue.get("id") or 0),
                    "semantic_focus_terms": build_keyword_terms(query_text, constraints),
                }
                similar_query = build_similar_issue_search_text(base_issue) or query_text
                similar_stage = execute_chroma_search(
                    similar_query,
                    limit,
                    similar_constraints,
                    semantic=True,
                )
                similar_stage = {
                    **similar_stage,
                    "source": "chroma_similar",
                    "meta": {
                        **similar_stage.get("meta", {}),
                        "base_issue_ids": list(constraints.get("issue_ids") or []),
                        "derived_query": similar_query[:500],
                    },
                }
                stages.append(similar_stage)

        if not exact_issue_lookup and not similar_issue_search and structured_snapshot_eligible:
            snapshot_stage = execute_mantis_snapshot_search(limit, constraints)
            snapshot_stage = revalidate_mantis_snapshot_stage(snapshot_stage, constraints)
            stages.append(snapshot_stage)
            historical_snapshot_used = snapshot_stage.get("status") == "ok"

        # 스냅샷은 마지막 재구축 시점까지의 데이터만 갖는다. 요청 범위가 기준일
        # 이후를 포함하면(또는 날짜 조건이 없어 "현재까지"가 암묵 범위이면)
        # 라이브 페이지 스캔을 함께 돌려 최신 구간을 보완한다. 결과 병합 시
        # 이슈 ID 기준 중복 제거는 collect_retrieved_issues가 처리한다.
        snapshot_covers_range = False
        if historical_snapshot_used:
            snapshot_as_of = get_mantis_snapshot_freshness()
            requested_to = str((constraints.get("created_at") or {}).get("to") or "")
            snapshot_covers_range = bool(
                snapshot_as_of and requested_to and requested_to <= snapshot_as_of
            )

        if not exact_issue_lookup and not similar_issue_search and (not historical_snapshot_used or not snapshot_covers_range):
            # 저장 필터는 화면번호를 부분 문자열이나 별도 조건으로 해석할 수 있다.
            # 화면번호 질의에서는 정확 일치를 보장하는 스냅샷/페이지 스캔만 사용한다.
            if not constraints.get("screen_numbers"):
                try:
                    stages.append(execute_mantis_filter_search(query_text, limit, constraints))
                except HTTPException as exc:
                    stages.append(
                        {
                            "source": "mantis_filter",
                            "status": "error",
                            "error_kind": "dependency",
                            "reason": exc.detail,
                            "issues_list": [],
                            "meta": {"displayed_count": 0},
                        }
                    )

            try:
                stages.append(execute_mantis_page_scan(keyword, limit, constraints))
            except HTTPException as exc:
                stages.append(
                    {
                        "source": "mantis_page_scan",
                        "status": "error",
                        "error_kind": "dependency",
                        "reason": exc.detail,
                        "issues_list": [],
                        "meta": {"displayed_count": 0},
                    }
                )

    if (
        tools.get("vectorDb")
        and mantis_intent
        and not exact_issue_lookup
        and not similar_issue_search
        and not historical_snapshot_used
    ):
        stages.append(execute_chroma_search(query_text, limit, constraints))

    for stage in stages:
        stage_meta = dict(stage.get("meta") or {})
        audit_constraints = notion_constraints if stage.get("source") == "notion" else constraints
        stage_meta["filter_audit"] = filter_audit_for_source(
            str(stage.get("source") or ""),
            audit_constraints.get("filter_clauses") or [],
        )
        stage["meta"] = stage_meta

    result_constraints = notion_constraints if literal_notion_title and notion_only else constraints

    return {
        "query": query_text,
        "keyword": keyword,
        "constraints": result_constraints,
        "intent_plan": intent_plan,
        "query_plan": intent_plan,
        "limit": limit,
        "stages": stages,
        "filter_audit": {
            "planned_filters": result_constraints.get("filter_clauses") or [],
            "sources": [stage.get("meta", {}).get("filter_audit", {}) for stage in stages],
        },
    }


def build_model_retrieval_context(retrieval_result: Dict[str, Any], limit: int) -> Dict[str, Any]:
    issue_limit = max(5, min(limit, 10))
    notion_limit = max(5, min(limit, 10))
    issues = collect_retrieved_issues(retrieval_result, issue_limit)
    notion_pages = collect_retrieved_notion_pages(retrieval_result, notion_limit)

    return {
        "query": retrieval_result.get("query", ""),
        "keyword": retrieval_result.get("keyword", ""),
        "constraints": retrieval_result.get("constraints", {}),
        "limit": limit,
        "stages": [
            {
                "source": stage.get("source"),
                "status": stage.get("status"),
                "meta": stage.get("meta", {}),
                "reason": stage.get("reason", ""),
            }
            for stage in retrieval_result.get("stages", [])
        ],
        "issues": [
            {
                "id": issue.get("id"),
                "project": issue.get("project"),
                "title": issue.get("summary"),
                "status": display_mantis_status_label(issue.get("status")),
                "handler": issue.get("handler"),
                "created_at": issue.get("created_at"),
                "screen_number": (issue.get("detail") or {}).get("screen_number", ""),
                "media_type": (issue.get("detail") or {}).get("media_type", ""),
                "date_fields": (issue.get("detail") or {}).get("date_fields", {}),
                "description": compact_text(
                    (issue.get("detail") or {}).get("description") or issue.get("preview", ""),
                    620,
                ),
            }
            for issue in issues
        ],
        "notion_pages": [
            {
                "title": page.get("title", "제목 없음"),
                "database": page.get("database_label", "Notion DB"),
                "status": page.get("status", "상태 미상"),
                "owner": page.get("owner", "담당자 미지정"),
                "due_date": page.get("due_date", ""),
                "updated_at": page.get("last_edited_time", ""),
                "content": compact_text(page.get("content", ""), 520),
            }
            for page in notion_pages
        ],
    }


def build_sources(retrieval_result: Optional[Dict[str, Any]], max_sources: int = 8) -> List[Dict[str, Any]]:
    if not retrieval_result:
        return []

    sources: List[Dict[str, Any]] = []
    seen_issue_ids = set()

    for stage in retrieval_result.get("stages", []):
        source_name = stage.get("source")
        for page in stage.get("notion_pages", []):
            page_id = page.get("id")
            if not page_id:
                continue
            sources.append(
                {
                    "type": "notion",
                    "source": source_name,
                    "page_id": page_id,
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
            )

        for issue in stage.get("issues_list", []):
            issue_id = issue.get("id")
            if not issue_id or issue_id in seen_issue_ids:
                continue
            seen_issue_ids.add(issue_id)
            sources.append(
                {
                    "type": "mantis",
                    "source": source_name,
                    "issue_id": issue_id,
                    "title": issue.get("summary", "제목 없음"),
                    "project": issue.get("project", "미상"),
                    "status": issue.get("status", "상태 미상"),
                    "priority": issue.get("priority", ""),
                    "severity": issue.get("severity", ""),
                    "handler": issue.get("handler", "담당자 미지정"),
                    "created_at": issue.get("created_at", ""),
                    "preview": issue.get("description", "")[:260],
                    "detail": issue,
                }
            )

        for doc in stage.get("documents", []):
            issue_id = doc.get("issue_id")
            if not issue_id or issue_id in seen_issue_ids:
                continue
            latest_issue = doc.get("latest_issue")
            if latest_issue:
                seen_issue_ids.add(issue_id)
                sources.append(
                    {
                        "type": "mantis",
                        "source": f"{source_name}_mantis_latest",
                        "issue_id": issue_id,
                        "title": latest_issue.get("summary", "제목 없음"),
                        "project": latest_issue.get("project", "미상"),
                        "status": latest_issue.get("status", "상태 미상"),
                        "priority": latest_issue.get("priority", ""),
                        "severity": latest_issue.get("severity", ""),
                        "handler": latest_issue.get("handler", "담당자 미지정"),
                        "created_at": latest_issue.get("created_at", ""),
                        "preview": latest_issue.get("description", "")[:260],
                        "detail": latest_issue,
                    }
                )
                continue
            content = doc.get("content", "")
            seen_issue_ids.add(issue_id)
            sources.append(
                {
                    "type": "chroma",
                    "source": source_name,
                    "issue_id": issue_id,
                    "title": extract_title_from_chroma_text(content),
                    "project": extract_project_from_chroma_text(content),
                    "status": "ChromaDB",
                    "priority": "",
                    "severity": "",
                    "handler": "",
                    "created_at": "",
                    "preview": re.sub(r"\s+", " ", content)[:260],
                }
            )

    plan_source = str((retrieval_result.get("intent_plan") or {}).get("source", ""))
    if plan_source == "both":
        notion_sources = [source for source in sources if source.get("type") == "notion"]
        issue_sources = [source for source in sources if source.get("type") != "notion"]
        balanced: List[Dict[str, Any]] = []
        while len(balanced) < max_sources and (notion_sources or issue_sources):
            if notion_sources:
                balanced.append(notion_sources.pop(0))
            if len(balanced) < max_sources and issue_sources:
                balanced.append(issue_sources.pop(0))
        return balanced
    return sources[:max_sources]


def collect_retrieved_issues(retrieval_result: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    rows: Dict[int, Dict[str, Any]] = {}
    similarity_search = any(
        stage.get("source") == "chroma_similar" for stage in retrieval_result.get("stages", [])
    )
    for stage in retrieval_result.get("stages", []):
        for issue in stage.get("issues_list", []):
            issue_id = issue.get("id")
            if not issue_id or issue_id in rows:
                continue
            rows[issue_id] = {
                "type": "mantis",
                "source": stage.get("source", "mantis_page_scan"),
                "id": issue_id,
                "project": issue.get("project", "미상"),
                "summary": issue.get("summary", "제목 없음"),
                "status": issue.get("status", "상태 미상"),
                "priority": issue.get("priority", ""),
                "severity": issue.get("severity", ""),
                "handler": issue.get("handler", "담당자 미지정"),
                "created_at": issue.get("created_at", ""),
                "preview": issue.get("description", "")[:260],
                "detail": issue,
            }

        for document_index, doc in enumerate(stage.get("documents", []), start=1):
            issue_id = doc.get("issue_id")
            if not issue_id or issue_id in rows:
                continue
            latest_issue = doc.get("latest_issue")
            if latest_issue:
                rows[issue_id] = {
                    "type": "mantis",
                    "source": f"{stage.get('source', 'chroma_db')}_mantis_latest",
                    "id": issue_id,
                    "project": latest_issue.get("project", "미상"),
                    "summary": latest_issue.get("summary", "제목 없음"),
                    "status": latest_issue.get("status", "상태 미상"),
                    "priority": latest_issue.get("priority", ""),
                    "severity": latest_issue.get("severity", ""),
                    "handler": latest_issue.get("handler", "담당자 미지정"),
                    "created_at": latest_issue.get("created_at", ""),
                    "preview": latest_issue.get("description", "")[:260],
                    "detail": latest_issue,
                    "_retrieval_rank": int(doc.get("similarity_rank") or document_index),
                }
                continue
            content = doc.get("content", "")
            rows[issue_id] = {
                "type": "chroma",
                "source": stage.get("source", "chroma_db"),
                "id": issue_id,
                "project": extract_project_from_chroma_text(content),
                "summary": extract_title_from_chroma_text(content),
                "status": extract_chroma_field(content, "현재 상태") or "상태 미상",
                "priority": "",
                "severity": "",
                "handler": extract_chroma_field(content, "담당자") or "담당자 미지정",
                "created_at": extract_chroma_date(content),
                "preview": re.sub(r"\s+", " ", content)[:260],
                "detail": {"content": content},
                "_retrieval_rank": int(doc.get("similarity_rank") or document_index),
            }

    if similarity_search:
        return sorted(
            rows.values(),
            key=lambda issue: (issue.get("_retrieval_rank", 10**9), -int(issue["id"])),
        )[:limit]
    return sorted(rows.values(), key=lambda issue: issue["id"], reverse=True)[:limit]


def count_retrieved_issues(retrieval_result: Dict[str, Any]) -> int:
    """검색 단계 간 중복을 제거하고 라이브 상태로 스냅샷을 보정한다.

    스냅샷과 라이브 검색은 같은 Mantis 이슈를 함께 반환한다. 단계별 건수를 단순히
    더하지 않고 전체 일치 ID의 합집합을 사용한다. 라이브 단계가 직접 확인한 ID는
    기존 집합에서 제거한 뒤 현재 조건에 맞는 ID만 다시 넣어, 스냅샷 이후 해결·종료된
    이슈도 총 건수에서 제외한다.
    """
    issue_ids: set[int] = set()
    fallback_count = 0

    for stage in retrieval_result.get("stages", []):
        meta = stage.get("meta") or {}
        if stage.get("status") != "ok" or meta.get("reference_only"):
            continue

        scanned_ids = {
            int(issue_id)
            for issue_id in meta.get("scanned_issue_ids", [])
            if str(issue_id).isdigit()
        }
        issue_ids.difference_update(scanned_ids)

        if "matched_issue_ids" in meta:
            issue_ids.update(
                int(issue_id)
                for issue_id in meta.get("matched_issue_ids", [])
                if str(issue_id).isdigit()
            )
            continue

        stage_ids = {
            int(issue.get("id") or 0)
            for issue in stage.get("issues_list", [])
            if issue.get("id")
        }
        if stage_ids:
            issue_ids.update(stage_ids)
            reported_count = int(meta.get("found_count") or meta.get("displayed_count") or 0)
            fallback_count += max(0, reported_count - len(stage_ids))
        else:
            fallback_count += int(meta.get("found_count") or meta.get("displayed_count") or 0)

    return len(issue_ids) + fallback_count


def combined_source_key(source: Dict[str, Any]) -> str:
    title = re.sub(r"^\s*(?:#?\d+\s*)?(?:\[[^]]+\]\s*)?", "", source.get("title", ""))
    return re.sub(r"[^0-9a-z가-힣]", "", title.casefold())


def merge_combined_sources(sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    merged: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    for source in sources:
        key = combined_source_key(source)
        if not key or key not in by_key:
            item = {**source, "matched_sources": [source.get("type", "unknown")]}
            merged.append(item)
            if key:
                by_key[key] = item
            continue
        existing = by_key[key]
        source_type = source.get("type", "unknown")
        if source_type not in existing["matched_sources"]:
            existing["matched_sources"].append(source_type)
    return merged


def build_snapshot_note(retrieval_result: Dict[str, Any]) -> Optional[str]:
    """스냅샷 기반 결과에 데이터 기준일을 알려 최신 이슈 누락 가능성을 인지시킨다."""
    stages = retrieval_result.get("stages", [])
    snapshot_stage = next(
        (
            stage
            for stage in stages
            if stage.get("source") == "mantis_snapshot" and stage.get("status") == "ok"
        ),
        None,
    )
    if not snapshot_stage:
        return None
    as_of = snapshot_stage.get("meta", {}).get("snapshot_as_of") or get_mantis_snapshot_freshness()
    if not as_of:
        return None
    live_succeeded = any(
        stage.get("source") in {"mantis_page_scan", "mantis_filter"}
        and stage.get("status") == "ok"
        for stage in stages
    )
    # 실시간 단계가 정상 완료되면 결과가 0건이어도 그 조회가 최신 구간의 근거다.
    # 오래된 로컬 스냅샷 기준일을 함께 노출하면 전체 Mantis 데이터가 그 날짜에서
    # 멈춘 것처럼 오해되므로 스냅샷 경고를 표시하지 않는다.
    if live_succeeded:
        return None
    return f"데이터 기준: 로컬 스냅샷 {as_of} 수집분입니다. 이후 등록된 이슈는 포함되지 않았을 수 있어요."


def similar_base_issue_ids_from_retrieval(retrieval_result: Dict[str, Any]) -> List[int]:
    for stage in retrieval_result.get("stages", []):
        base_issue_ids = stage.get("meta", {}).get("base_issue_ids")
        if base_issue_ids:
            return [int(issue_id) for issue_id in base_issue_ids if issue_id]
    return []


def retrieval_trace(retrieval_result: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not retrieval_result:
        return []
    labels = {
        "notion": "Notion",
        "mantis_id_lookup": "Mantis ID 조회",
        "mantis_filter": "Mantis 조건 검색",
        "mantis_page_scan": "Mantis 최신 페이지",
        "mantis_snapshot": "Mantis 전체 인덱스",
        "mantis_count": "Mantis 건수 집계",
        "mantis_effort": "Mantis 공수 집계",
        "chroma_db": "ChromaDB",
    }
    trace = []
    for stage in retrieval_result.get("stages", []):
        count = len(stage.get("notion_pages", []))
        count += len(stage.get("issues_list", []))
        count += len(stage.get("documents", []))
        trace.append(
            {
                "source": stage.get("source", "unknown"),
                "label": labels.get(stage.get("source"), stage.get("source", "검색 단계")),
                "status": stage.get("status", "unknown"),
                "count": count,
                "reason": compact_text(stage.get("reason", ""), 160),
            }
        )
    return trace
