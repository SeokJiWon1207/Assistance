"""조회 결과의 중복·제외·실시간 우선순위를 보존한 순수 행 정리."""
import re
from typing import Any, Dict, List, Optional
from backend.sources.chroma_document import extract_chroma_date, extract_chroma_field, extract_project_from_chroma_text, extract_title_from_chroma_text

def _ordered_issue_stages(retrieval_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """저장본을 먼저 적용하고 실시간 확인은 실행 순서대로 덮어쓴다."""
    def priority(stage: Dict[str, Any]) -> int:
        meta = stage.get("meta") or {}
        if ("live_revalidation_complete" in meta
                or any(doc.get("latest_issue") for doc in stage.get("documents", []))
                or (str(stage.get("source", "")).startswith("chroma") and meta.get("scanned_issue_ids"))):
            return 2
        if stage.get("source") == "mantis_snapshot" or meta.get("status") == "snapshot":
            return 1
        return 2 if str(stage.get("source", "")).startswith("mantis_") else 0
    return sorted(retrieval_result.get("stages", []), key=priority)


def collect_retrieved_issues(retrieval_result: Dict[str, Any], limit: Optional[int]) -> List[Dict[str, Any]]:
    rows: Dict[int, Dict[str, Any]] = {}
    stages = [stage for stage in _ordered_issue_stages(retrieval_result)
              if stage.get("status") not in {"error", "skipped", "not_found"}
              and not stage.get("meta", {}).get("reference_only")]
    excluded_ids: set[int] = set()
    similarity_search = any(
        stage.get("source") == "chroma_similar" for stage in retrieval_result.get("stages", [])
    )
    for stage in stages:
        meta = stage.get("meta") or {}
        if "matched_issue_ids" in meta:
            scanned = {int(value) for value in meta.get("scanned_issue_ids", [])}
            matched = {int(value) for value in meta["matched_issue_ids"]}
            excluded_ids.update(scanned - matched)
            # 부분 재조회에 섞인 저장본은 앞선 실시간 제외를 취소할 수 없다.
            excluded_ids.difference_update(matched & scanned if "scanned_issue_ids" in meta else matched)
            for issue_id in scanned - matched:
                rows.pop(issue_id, None)
        for issue in stage.get("issues_list", []):
            issue_id = issue.get("id")
            if not issue_id or int(issue_id) in excluded_ids:
                continue
            rows[issue_id] = {
                "type": "mantis",
                "source": stage.get("source", "mantis_page_scan"),
                "id": issue_id,
                "project": issue.get("project", "미상"),
                "summary": issue.get("summary", "제목 없음"),
                "status": issue.get("status", "상태 미상"),
                "status_label": issue.get("status_label", ""),
                "priority": issue.get("priority", ""),
                "severity": issue.get("severity", ""),
                "handler": issue.get("handler", "담당자 미지정"),
                "created_at": issue.get("created_at", ""),
                "updated_at": issue.get("updated_at", ""),
                "media_type": issue.get("media_type", "기타/미상"),
                "preview": issue.get("description", "")[:260],
                "detail": issue,
                "_retrieval_rank": (0 if stage.get("source") in {
                    "mantis_explanation_reference", "mantis_explanation_focus"
                } else 10**9),
            }

        for document_index, doc in enumerate(stage.get("documents", []), start=1):
            issue_id = doc.get("issue_id")
            if not issue_id:
                continue
            latest_issue = doc.get("latest_issue")
            if latest_issue:
                excluded_ids.discard(int(issue_id))
                rows[issue_id] = {
                    "type": "mantis",
                    "source": f"{stage.get('source', 'chroma_db')}_mantis_latest",
                    "id": issue_id,
                    "project": latest_issue.get("project", "미상"),
                    "summary": latest_issue.get("summary", "제목 없음"),
                    "status": latest_issue.get("status", "상태 미상"),
                    "status_label": latest_issue.get("status_label", ""),
                    "priority": latest_issue.get("priority", ""),
                    "severity": latest_issue.get("severity", ""),
                    "handler": latest_issue.get("handler", "담당자 미지정"),
                    "created_at": latest_issue.get("created_at", ""),
                    "updated_at": latest_issue.get("updated_at", ""),
                    "media_type": latest_issue.get("media_type", "기타/미상"),
                    "preview": latest_issue.get("description", "")[:260],
                    "detail": latest_issue,
                    "_retrieval_rank": min(
                        rows.get(issue_id, {}).get("_retrieval_rank", 10**9),
                        int(doc.get("similarity_rank") or document_index),
                    ),
                }
                continue
            if issue_id in rows or int(issue_id) in excluded_ids:
                continue
            content = doc.get("content", "")
            rows[issue_id] = {
                "type": "chroma",
                "source": stage.get("source", "chroma_db"),
                "id": issue_id,
                "project": extract_project_from_chroma_text(content),
                "summary": extract_title_from_chroma_text(content),
                "status": extract_chroma_field(content, "현재 상태") or "상태 미상",
                "status_label": extract_chroma_field(content, "상태 표시명"),
                "priority": extract_chroma_field(content, "우선순위"),
                "severity": extract_chroma_field(content, "이슈 유형"),
                "handler": extract_chroma_field(content, "담당자") or "담당자 미지정",
                "created_at": extract_chroma_date(content),
                "media_type": extract_chroma_field(content, "매체구분") or "기타/미상",
                "preview": re.sub(r"\s+", " ", content)[:260],
                "detail": {"content": content, "reporter": extract_chroma_field(content, "요청자") or "미상",
                           "status_label": extract_chroma_field(content, "상태 표시명"),
                           "priority_label": extract_chroma_field(content, "우선순위 표시명"),
                           "severity_label": extract_chroma_field(content, "이슈 유형 표시명")},
                "_retrieval_rank": int(doc.get("similarity_rank") or document_index),
            }

    if (retrieval_result.get("intent_plan") or {}).get("operation") == "explain":
        base_ids = (retrieval_result.get("constraints") or {}).get("issue_ids") or []
        return sorted(rows.values(), key=lambda issue: (int(issue["id"]) not in base_ids,
                      issue.get("_retrieval_rank", 10**9), -int(issue["id"])))[:limit]
    if similarity_search:
        return sorted(
            rows.values(),
            key=lambda issue: (issue.get("_retrieval_rank", 10**9), -int(issue["id"])),
        )[:limit]
    ordering = (retrieval_result.get('constraints') or {}).get('sort') or {}
    if ordering:
        field = ordering.get('field')
        key = 'id' if field == 'issue_id' else str(field)
        return sorted(rows.values(), key=lambda issue: (issue.get(key) or '', issue['id']),
                      reverse=ordering.get('direction') != 'asc')[:limit]
    return sorted(rows.values(), key=lambda issue: issue["id"], reverse=True)[:limit]


def collect_retrieved_customer_groups(retrieval_result: Dict[str, Any]) -> Dict[int, str]:
    """표시 개수와 관계없이 집계하고 라이브 재조회 결과로 중복·상태 변경을 보정한다."""
    groups: Dict[int, str] = {}
    for stage in _ordered_issue_stages(retrieval_result):
        meta = stage.get("meta") or {}
        if stage.get("status") != "ok" or meta.get("reference_only"):
            continue
        for issue_id in meta.get("scanned_issue_ids", []):
            groups.pop(int(issue_id), None)
        stage_groups = {
            int(issue_id): str(project)
            for issue_id, project in (meta.get("matched_customer_groups") or {}).items()
        }
        for issue in stage.get("issues_list", []):
            if issue.get("id"):
                stage_groups[int(issue["id"])] = str(issue.get("project") or "미상")
        groups.update(stage_groups)
    return groups


def count_retrieved_issues(retrieval_result: Dict[str, Any]) -> int:
    """검색 단계 간 중복을 제거하고 라이브 상태로 스냅샷을 보정한다.

    스냅샷과 라이브 검색은 같은 Mantis 이슈를 함께 반환한다. 단계별 건수를 단순히
    더하지 않고 전체 일치 ID의 합집합을 사용한다. 라이브 단계가 직접 확인한 ID는
    기존 집합에서 제거한 뒤 현재 조건에 맞는 ID만 다시 넣어, 스냅샷 이후 해결·종료된
    이슈도 총 건수에서 제외한다.
    """
    issue_ids: set[int] = set()
    excluded_ids: set[int] = set()
    fallback_count = 0

    for stage in _ordered_issue_stages(retrieval_result):
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
            matched_ids = {
                int(issue_id)
                for issue_id in meta.get("matched_issue_ids", [])
                if str(issue_id).isdigit()
            }
            excluded_ids.update(scanned_ids - matched_ids)
            excluded_ids.difference_update(matched_ids & scanned_ids if "scanned_issue_ids" in meta else matched_ids)
            issue_ids.update(matched_ids - excluded_ids)
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
