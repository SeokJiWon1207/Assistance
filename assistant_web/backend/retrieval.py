"""검색 결과의 출처·신선도·실행 흔적 표시와 기존 import 경로.

검색 실행은 services.retrieval_pipeline이 소유한다. 행 정리와 모델 문맥은
P5-A의 evidence 소유자를 사용하며 옛 진입점은 같은 객체를 재내보낸다.
"""

import re
from typing import Any, Dict, List, Optional


from backend.display import korean_day, korean_time
from backend.utils.text import (
    compact_text,
)
from backend.sources.mantis_fields import build_sources_from_issue_rows
from backend.sources.mantis import get_mantis_snapshot_freshness


def build_sources(retrieval_result: Optional[Dict[str, Any]], max_sources: int = 8) -> List[Dict[str, Any]]:
    if not retrieval_result:
        return []

    sources: List[Dict[str, Any]] = []

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

    sources.extend(build_sources_from_issue_rows(collect_retrieved_issues(retrieval_result, None)))

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
    stored_stage = next((stage for stage in stages
                         if stage.get("meta", {}).get("fetchedAt")
                         and (stage.get("meta", {}).get("status") == "snapshot"
                              or stage.get("source") == "mantis_snapshot")), None)
    if stored_stage:
        fetched_at = str(stored_stage["meta"]["fetchedAt"])
        return f"데이터 기준: {korean_time(fetched_at)}에 저장한 Mantis 원본입니다. 이후 변경은 다음 동기화에 반영됩니다."
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
    if snapshot_stage.get("meta", {}).get("live_fallback_count"):
        return "실시간 확인에 실패해 로컬 스냅샷 참고값을 표시합니다. 현재 상태나 담당자가 다를 수 있습니다."
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
    return f"데이터 기준: {korean_day(as_of)}에 수집한 로컬 스냅샷입니다. 이후 등록된 이슈는 포함되지 않았을 수 있어요."


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
        "mantis_effort_trend": "Mantis 월별 집계 원문 예시",
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

from backend.evidence.retrieval_rows import (
    _ordered_issue_stages as _ordered_issue_stages,
    collect_retrieved_issues as collect_retrieved_issues,
    collect_retrieved_customer_groups as collect_retrieved_customer_groups,
    count_retrieved_issues as count_retrieved_issues,
)
from backend.evidence.model_context import (
    _notion_model_content as _notion_model_content,
    build_model_retrieval_context as build_model_retrieval_context,
    build_model_evidence_sources as build_model_evidence_sources,
)


# 기존 import 경로는 실제 소유자의 동일 객체를 재내보낸다.
from backend.services.retrieval_pipeline import (
    notion_question_needs_blocks as notion_question_needs_blocks,
    _similar_reference_lookup as _similar_reference_lookup,
    source_specific_terms as source_specific_terms,
    grounded_mantis_terms as grounded_mantis_terms,
    _SOURCE_TITLE_REFERENCE as _SOURCE_TITLE_REFERENCE,
    _REFERENCE_GENERIC_TERMS as _REFERENCE_GENERIC_TERMS,
    _reference_matches_question as _reference_matches_question,
    _explanation_referenced_issues as _explanation_referenced_issues,
    _explanation_retrieval as _explanation_retrieval,
    run_retrieval_pipeline as run_retrieval_pipeline,
)
