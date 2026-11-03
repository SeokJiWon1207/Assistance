"""Mantis 검색 실행과 저장본·실시간 조회 선택을 조정한다.

필드 정규화는 mantis_fields, 조건 판정은 mantis_matching, HTTP는
mantis_client, 저장본 순회는 mantis_snapshot_query가 소유한다.
기존 함수 경로는 이 모듈의 명시적 재내보내기로 유지한다.
"""

import json
import os
import re
import sqlite3 as sqlite3
from contextlib import closing
from datetime import date, datetime
from functools import lru_cache
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException

from backend.sources.chroma_document import normalize_chroma_issue_document
from backend.config import (
    CHROMA_DB_PATH,
    CONFIG_DIR,
    get_mantis_settings,
)
from backend.intent.keyword_terms import text_matches_keyword_terms
from backend.sources.mantis_snapshot_query import scan_mantis_documents
from backend.sources.mantis_collection import clear_recent_cache, fetch_recent_cached, collection_metadata
from backend.sources.mantis_store import read_complete_store as read_complete_store
from backend.sources.mantis_store import read_store_metadata, stream_complete_store, read_recent_store_rows
from backend.sources.mantis_evidence import constraint_evidence_terms
from backend.timing import measured
from backend.intent.matching import chroma_text_matches_constraints
from backend.intent.matching import text_matches_status as text_matches_status
from backend.intent.constraints import has_constraint


def load_mantis_filters() -> List[Dict[str, Any]]:
    filters_path = CONFIG_DIR / "mantis_filters.json"
    if not filters_path.exists():
        return []
    data = json.loads(filters_path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    return data.get("filters", [])


def find_matching_mantis_filter(text: str) -> Optional[Dict[str, Any]]:
    normalized_text = text.lower()
    for item in load_mantis_filters():
        if not item.get("enabled", True):
            continue
        filter_id = item.get("filter_id")
        if not filter_id:
            continue
        aliases = item.get("aliases", [])
        names = [item.get("name", ""), *aliases]
        for alias in names:
            if alias and str(alias).lower() in normalized_text:
                return item
    return None


# 정상 페이지 요청은 6초 안에 끝난다. 드물게 응답 없이 멈추는 요청을 45초까지
# 기다리면 첫 질문이 50초를 넘고, 캐시 TTL도 지나 결과가 저장되지 않는다.
MANTIS_PAGE_SCAN_TIMEOUT_SECONDS = 12


def fetch_mantis_recent_issues(
    total_limit: int = 800,
    page_size: int = 100,
    request_timeout: int = 30,
    *,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """API 선두 범위를 병렬 수집한 뒤 ID순으로 정렬한다(전역 최신순 보장 없음)."""
    stored = None if force_refresh else read_recent_store_rows(total_limit)
    if stored is not None:
        # 명시적인 최신 N건 요청은 전체 원본을 정렬한 뒤 자른다.
        from backend.sources.mantis_collection import IssueBatch
        return IssueBatch(stored[:total_limit], stored.meta)
    issues = _fetch_mantis_raw_issues(total_limit, page_size, request_timeout, force_refresh=force_refresh)
    issues.sort(key=lambda issue: int(issue.get("id") or 0), reverse=True)
    return issues


def _fetch_mantis_raw_issues(
    total_limit: int, page_size: int, request_timeout: int, *, force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """페이지 검색과 집계가 공유하는 원본 수집. 조건별 결과/점수는 캐시하지 않는다."""
    total_limit = max(1, min(total_limit, 3000))
    page_size = max(1, min(page_size, 100))
    settings = get_mantis_settings()

    def fetch_page(page: int) -> List[Dict[str, Any]]:
        return fetch_mantis_issues(
            {"page_size": page_size, "page": page},
            timeout=request_timeout,
        )

    return fetch_recent_cached(
        (settings["url"], settings["token"]), fetch_page,
        limit=total_limit, page_size=page_size, timeout=request_timeout,
        workers=mantis_live_max_workers(),
        ttl=_mantis_collection_setting("MANTIS_LIVE_CACHE_TTL_SECONDS", 30, 0, 60),
        force_refresh=force_refresh,
    )


def mantis_live_max_workers() -> int:
    return _mantis_collection_setting("MANTIS_LIVE_MAX_WORKERS", 6, 1, 6)


def _mantis_collection_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return max(minimum, min(int(os.getenv(name, str(default))), maximum))
    except ValueError:
        return default


def clear_mantis_recent_cache() -> None:
    clear_recent_cache()


def extract_snapshot_limit(text: str, default: int = 800) -> int:
    match = re.search(r"최신\s*(\d+)\s*건\s*기준", text)
    if not match:
        return default
    return max(1, min(int(match.group(1)), 3000))


def collect_mantis_live_ids_in_range(
    constraints: Dict[str, Any],
    max_pages: int = 8,
    page_size: int = 100,
    scan_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """라이브 Mantis API를 페이징하며 제약(고객사/상태/등록일 범위 등)에 맞는 이슈를
    ID별로 수집한다. 라이브 이슈는 실제 created_at을 갖고 있어 날짜 필터가 정확하다.

    로컬 스냅샷은 재구축 시점(예: 2026-05-07)의 옛 포맷이라 상당수 문서에 등록일이
    없어 월/연 단위 카운트가 0으로 새는 문제가 있다. 날짜 조건이 있는 집계에서는 이
    함수로 라이브 결과를 병합해 정확도를 보완한다. 실패 이유와 완료 여부를 함께
    반환해 호출부가 오래된 스냅샷 값을 현재의 확정값처럼 표시하지 않게 한다."""
    # 명시적인 최신 N건 창은 API 응답 순서를 기준으로 한다. 전체 저장본은
    # ID 내림차순이므로 이를 잘라 쓰면 최근 수정된 과거 이슈가 사라진다.
    if scan_limit is None:
        with stream_complete_store() as stored:
            if stored is not None:
                matched, scanned = {}, []
                for row in stored:
                    scanned.append(int(row['id']))
                    if issue_matches_constraints(row, constraints):
                        matched[str(row['id'])] = row
                return {'status': 'ok', 'error_kind': '', 'reason': '', 'complete': True,
                        'issues': matched, 'scanned_pages': 0, 'scanned_ids': scanned,
                        'fetchedAt': stored.meta['fetchedAt']}
    collected: Dict[str, Dict[str, Any]] = {}
    scanned_ids: set[int] = set()
    scanned_pages = 0
    complete = False
    try:
        for page in range(1, max_pages + 1):
            issues = fetch_mantis_issues({"page_size": page_size, "page": page}, timeout=30, attempts=2)
            scanned_pages += 1
            for issue in issues:
                issue_id = int(issue.get("id") or 0)
                if not issue_id or issue_id in scanned_ids:
                    continue
                scanned_ids.add(issue_id)
                if issue_matches_constraints(issue, constraints):
                    collected[str(issue_id)] = issue
                if scan_limit is not None and len(scanned_ids) >= scan_limit:
                    complete = True
                    break
            if complete or len(issues) < page_size:
                complete = True
                break
    except (requests.RequestException, HTTPException) as exc:
        return {"status": "error", "error_kind": "dependency", "issues": collected,
                "reason": f"Mantis live count query failed: {type(exc).__name__}",
                "scanned_pages": scanned_pages, "scanned_ids": sorted(scanned_ids), "complete": False}
    return {"status": "ok", "error_kind": "", "issues": collected, "reason": "",
            "scanned_pages": scanned_pages, "scanned_ids": sorted(scanned_ids), "complete": complete}


def execute_mantis_filter_search(
    query_text: str,
    limit: int,
    constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    matched_filter = find_matching_mantis_filter(query_text)
    if not matched_filter:
        return {
            "source": "mantis_filter",
            "status": "skipped",
            "reason": "질문에 매칭되는 mantis_filters.json 필터가 없습니다.",
            "issues_list": [],
            "meta": {"displayed_count": 0},
        }

    issues = fetch_mantis_issues(
        {
            "filter_id": matched_filter["filter_id"],
            "page_size": limit,
            "page": 1,
        }
    )
    scope_complete = len(issues) < limit
    if constraints and has_constraint(constraints):
        issues = [
            issue
            for issue in issues
            if issue_matches_constraints(issue, constraints)
        ]

    issues.sort(key=lambda issue: issue.get("id", 0), reverse=True)
    return {
        "source": "mantis_filter",
        "status": "ok",
        "filter": {
            "name": matched_filter.get("name", ""),
            "filter_id": matched_filter.get("filter_id"),
        },
        "meta": {
            "displayed_count": len(issues[:limit]),
            "query_complete": True,
            "search_complete": scope_complete,
            "coverage": "saved_filter" if scope_complete else "bounded",
            "result_window_complete": scope_complete,
        },
        "issues_list": [normalize_issue(issue) for issue in issues[:limit]],
    }


def execute_mantis_id_lookup(
    issue_ids: List[int],
    constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    issues = []
    missing_ids = []
    for issue_id in issue_ids:
        issue = fetch_mantis_issue_by_id(issue_id)
        if not issue:
            missing_ids.append(issue_id)
            continue
        if constraints and has_constraint(constraints) and not issue_matches_constraints(issue, constraints):
            continue
        issues.append(issue)

    issues.sort(key=lambda issue: issue.get("id", 0), reverse=True)
    return {
        "source": "mantis_id_lookup",
        "status": "ok" if issues else "not_found",
        "meta": {
            "issue_ids": issue_ids,
            "search_complete": True,
            "missing_ids": missing_ids,
            "displayed_count": len(issues),
        },
        "issues_list": [normalize_issue(issue, evidence_query=(constraints or {}).get("answer_query")) for issue in issues],
    }


@lru_cache(maxsize=4)
def _snapshot_freshness_for_mtime(_mtime: float, _metadata_mtime: float) -> str:
    """스냅샷 문서의 최신 등록일을 SQLite 내부에서 집계한다.

    문서 본문 전체를 Python으로 전송하던 방식은 데이터가 커질수록 첫 요청이 수십 초
    지연됐다. 최신 포맷의 ``등록일: YYYY-MM-DD`` 헤더를 SQL에서 잘라 MAX로 계산하고,
    파일 mtime을 캐시 키로 사용해 재구축 전까지 재계산하지 않는다.
    """
    metadata_path = CHROMA_DB_PATH.parent / "freshness.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            as_of = str(metadata.get("asOf") or "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of) and as_of <= date.today().isoformat():
                return as_of
        except (OSError, json.JSONDecodeError):
            pass

    # 과거 이슈 본문에 "2032-07-27 메일 수신" 같은 오타 날짜가 실존한다.
    # 미래 날짜는 데이터 기준일이 될 수 없으므로 오늘 이후 값은 무시한다.
    today = date.today().isoformat()
    try:
        with closing(open_sqlite(CHROMA_DB_PATH, profile=CHROMA)) as connection, connection:
            row = connection.execute(
                """
                SELECT MAX(candidate)
                FROM (
                    SELECT substr(
                        ltrim(substr(string_value, instr(string_value, '등록일:') + length('등록일:'))),
                        1,
                        10
                    ) AS candidate
                    FROM embedding_metadata
                    WHERE key = 'chroma:document'
                      AND instr(string_value, '등록일:') > 0
                )
                WHERE candidate GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'
                  AND candidate <= ?
                """,
                (today,),
            ).fetchone()
    except sqlite3.Error:
        return ""
    return str((row or [""])[0] or "")


def get_mantis_snapshot_freshness() -> str:
    """로컬 Mantis 스냅샷의 데이터 기준일(YYYY-MM-DD). 없으면 빈 문자열."""
    stored_meta = read_store_metadata()
    if stored_meta is not None:
        return datetime.fromisoformat(stored_meta["fetchedAt"]).astimezone().date().isoformat()
    if not CHROMA_DB_PATH.exists():
        return ""
    metadata_path = CHROMA_DB_PATH.parent / "freshness.json"
    metadata_mtime = metadata_path.stat().st_mtime if metadata_path.exists() else -1.0
    return _snapshot_freshness_for_mtime(
        CHROMA_DB_PATH.stat().st_mtime,
        metadata_mtime,
    )


def get_mantis_snapshot_freshness_status() -> Dict[str, Any]:
    """Return a UI-friendly age and health status for the local snapshot."""
    store_meta = read_store_metadata(allow_stale=True)
    if store_meta is not None:
        fetched = datetime.fromisoformat(store_meta["fetchedAt"]).astimezone()
        fresh = store_meta["search_complete"]
        return {"source": "mantis_complete_snapshot", "asOf": fetched.date().isoformat(),
                "updatedAt": fetched.isoformat(), "ageDays": max(0, (date.today() - fetched.date()).days),
                "status": "fresh" if fresh else "stale", "label": "최신" if fresh else "지연"}
    as_of = get_mantis_snapshot_freshness()
    updated_at = ""
    if CHROMA_DB_PATH.exists():
        updated_at = datetime.fromtimestamp(CHROMA_DB_PATH.stat().st_mtime).astimezone().isoformat(
            timespec="seconds"
        )
    age_days: Optional[int] = None
    if as_of:
        try:
            age_days = max(0, (date.today() - date.fromisoformat(as_of)).days)
        except ValueError:
            age_days = None
    if age_days is None:
        status = "unknown"
        label = "기준일 확인 필요"
    elif age_days <= 1:
        status = "fresh"
        label = "최신"
    elif age_days <= 3:
        status = "warning"
        label = "갱신 권장"
    else:
        status = "stale"
        label = "지연"
    return {
        "source": "mantis_local_snapshot",
        "asOf": as_of or None,
        "updatedAt": updated_at or None,
        "ageDays": age_days,
        "status": status,
        "label": label,
    }


def execute_mantis_snapshot_search(
    limit: int,
    constraints: Dict[str, Any],
) -> Dict[str, Any]:
    """Search the complete local Mantis snapshot for structured historical queries."""
    snapshot = scan_mantis_documents(
        CHROMA_DB_PATH,
        lambda content: chroma_text_matches_constraints(content, constraints),
        normalize_chroma_issue_document,
        example_limit=limit,
        group_key=lambda issue: str(issue.get("project") or "미상"),
        constraints=constraints,
        use_raw_constraints=True,
    )
    if not snapshot["available"]:
        return {
            "source": "mantis_snapshot",
            "status": "error",
            "reason": snapshot["reason"],
            "meta": {"displayed_count": 0, "found_count": 0},
            "issues_list": [],
        }
    return {
        "source": "mantis_snapshot",
        "status": "ok",
        "reason": "",
        "meta": {
            "search_complete": snapshot.get("search_complete", False),
            "fetchedAt": snapshot.get("fetchedAt"),
            "scanned_count": snapshot["scanned_count"],
            "found_count": snapshot["matched_count"],
            "displayed_count": len(snapshot["issues"]),
            "matched_issue_ids": sorted(snapshot["matched_ids"]),
            "ordered_matched_ids": snapshot.get("ordered_matched_ids") or [],
            "matched_customer_groups": snapshot.get("matched_groups") or {},
            "applied_filters": constraints,
            "tool": "mantis_complete_snapshot" if snapshot.get("fetchedAt") else "mantis_chroma_snapshot",
            "snapshot_as_of": get_mantis_snapshot_freshness(),
        },
        "issues_list": snapshot["issues"],
    }


def revalidate_mantis_snapshot_stage(
    stage: Dict[str, Any],
    constraints: Dict[str, Any],
) -> Dict[str, Any]:
    """Refresh snapshot candidates whose mutable fields may have changed.

    Handler and status values in the local index become stale between rebuilds.
    Re-fetch only the small displayed candidate set, drop rows that no longer
    satisfy the live constraints, and retain the snapshot row solely when the
    live API is unavailable so existing offline fallback behavior is preserved.
    """
    mutable_filter = bool(
        constraints.get("mantis_team_members")
        or (constraints.get("people") or {}).get("names")
        or constraints.get("statuses")
        or constraints.get("excluded_statuses")
        or constraints.get("platform_key")
        or constraints.get("excluded_platform_keys")
        or any(
            clause.get("field") in {"owner", "status", "platform"}
            for clause in constraints.get("filter_clauses", [])
        )
    )
    if stage.get("status") != "ok" or not mutable_filter:
        return stage

    refreshed: List[Dict[str, Any]] = []
    evidence_terms = constraint_evidence_terms(constraints)
    matched_issue_ids = {
        int(issue_id)
        for issue_id in (stage.get("meta") or {}).get("matched_issue_ids", [])
        if str(issue_id).isdigit()
    }
    revalidated_count = 0
    removed_count = 0
    fallback_count = 0
    target_count = int((stage.get("meta") or {}).get("displayed_count") or len(stage.get("issues_list", [])))
    checked_issue_ids: set[int] = set()
    for snapshot_issue in stage.get("issues_list", []):
        issue_id = int(snapshot_issue.get("id") or 0)
        checked_issue_ids.add(issue_id)
        try:
            live_issue = fetch_mantis_issue_by_id(issue_id) if issue_id else None
        except (requests.RequestException, HTTPException) as exc:
            if isinstance(exc, HTTPException) and exc.status_code < 500 and exc.status_code != 429:
                live_issue = None
            else:
                refreshed.append(snapshot_issue)
                fallback_count += 1
                continue
        if live_issue is None:
            removed_count += 1
            matched_issue_ids.discard(issue_id)
            continue
        revalidated_count += 1
        if issue_matches_constraints(live_issue, constraints):
            refreshed.append(normalize_issue(live_issue, search_terms=evidence_terms))
            matched_issue_ids.add(issue_id)
        else:
            removed_count += 1
            matched_issue_ids.discard(issue_id)

    # If a displayed snapshot row became stale, refill the requested result
    # from the remaining matched IDs instead of returning too few rows. The
    # snapshot scan already found these IDs across the complete local index;
    # each replacement is still validated against the live issue endpoint.
    try:
        refill_limit = max(
            0,
            min(int(os.getenv("MANTIS_LIVE_REFILL_MAX_ATTEMPTS", "12")), 50),
        )
    except ValueError:
        refill_limit = 12
    refill_attempt_count = 0
    refill_dependency_error = False
    remaining_ids = matched_issue_ids - checked_issue_ids
    ordered_ids = (stage.get('meta') or {}).get('ordered_matched_ids') or []
    refill_candidates = [issue_id for issue_id in ordered_ids if issue_id in remaining_ids]
    refill_candidates.extend(sorted(remaining_ids - set(refill_candidates), reverse=True))
    for issue_id in refill_candidates:
        if len(refreshed) >= target_count:
            break
        if refill_attempt_count >= refill_limit:
            break
        refill_attempt_count += 1
        try:
            live_issue = fetch_mantis_issue_by_id(issue_id)
        except (requests.RequestException, HTTPException) as exc:
            retryable_failure = not (
                isinstance(exc, HTTPException)
                and exc.status_code < 500
                and exc.status_code != 429
            )
            if retryable_failure:
                fallback_count += 1
                refill_dependency_error = True
                break
            continue
        checked_issue_ids.add(issue_id)
        if live_issue is None:
            removed_count += 1
            matched_issue_ids.discard(issue_id)
            continue
        revalidated_count += 1
        if issue_matches_constraints(live_issue, constraints):
            refreshed.append(normalize_issue(live_issue, search_terms=evidence_terms))
        else:
            removed_count += 1
            matched_issue_ids.discard(issue_id)

    refill_limit_reached = (
        len(refreshed) < target_count
        and refill_attempt_count >= refill_limit
        and refill_attempt_count < len(refill_candidates)
    )
    return {
        **stage,
        "issues_list": refreshed,
        "meta": {
            **(stage.get("meta") or {}),
            "found_count": len(matched_issue_ids),
            "matched_customer_groups": {
                **{
                    int(issue_id): project
                    for issue_id, project in (stage.get("meta", {}).get("matched_customer_groups") or {}).items()
                    if int(issue_id) in matched_issue_ids
                },
                **{issue["id"]: issue.get("project") or "미상" for issue in refreshed},
            },
            "displayed_count": len(refreshed),
            "matched_issue_ids": sorted(matched_issue_ids),
            "live_revalidated_count": revalidated_count,
            "stale_removed_count": removed_count,
            "live_fallback_count": fallback_count,
            "live_refill_attempt_count": refill_attempt_count,
            "live_refill_limit_reached": refill_limit_reached,
            "search_complete": stage.get("meta", {}).get("search_complete") is True and not (
                fallback_count or refill_dependency_error or refill_limit_reached
            ),
            "live_revalidation_complete": not (
                fallback_count or refill_dependency_error or refill_limit_reached
            ),
            "freshness_warning": (
                "실시간 확인이 끝나지 않아 스냅샷 참고값을 포함할 수 있습니다."
                if fallback_count or refill_limit_reached
                else ""
            ),
        },
    }


@measured("mantis_page_scan")
def _sort_search_matches(rows, constraints):
    ordering = (constraints or {}).get('sort') or {}
    field = ordering.get('field')
    if field in {'created_at', 'updated_at', 'issue_id'}:
        from backend.query.ordering import issue_order_key
        rows.sort(key=lambda row: issue_order_key(row, field),
                  reverse=ordering.get('direction') != 'asc')
    else:
        rows.sort(key=lambda row: (row.get('_assistant_match_score', 0), row.get('id', 0)), reverse=True)


def execute_mantis_page_scan(
    search_keyword: str,
    limit: int = 5,
    constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    keywords = [kw.strip().casefold() for kw in search_keyword.split() if kw.strip()]
    filtered_issues: List[Dict[str, Any]] = []
    matched_ids: list[int] = []
    matched_groups: Dict[int, str] = {}
    found_count = 0
    scanned_issue_ids: set[int] = set()
    status_excluded_count = 0
    page_size = 50
    max_pages = 16

    try:
        # 기존과 같은 50건 × 16페이지를 전부 읽은 뒤 조건을 적용한다.
        # API 정렬이 보장되지 않으므로 표시 건수를 채워도 조기 종료하지 않는다.
        with stream_complete_store() as stored:
            raw_issues = stored if stored is not None else _fetch_mantis_raw_issues(
                max_pages * page_size, page_size, MANTIS_PAGE_SCAN_TIMEOUT_SECONDS,
            )
            candidates = raw_issues
            if (stored is not None and getattr(stored, "has_search_projection", False)
                    and keywords and not (constraints or {}).get("statuses")):
                # 상태 제외 안내는 키워드와 무관한 건수이므로 상태 질의는 기존 전체 검사를 유지한다.
                scanned_issue_ids.update(issue_id for issue_id in stored.all_issue_ids() if issue_id)
                candidates = stored.keyword_candidates(keywords)
            for issue in candidates:
                issue_id = int(issue.get("id") or 0)
                if issue_id:
                    scanned_issue_ids.add(issue_id)
                combined_text = issue_to_search_text(issue)
                if constraints and has_constraint(constraints) and not issue_matches_constraints(
                    issue,
                    constraints,
                    search_text=combined_text,
                ):
                    # 상태만 달라 탈락한 항목 수를 유지해 조건 안내에 사용한다.
                    if constraints.get("statuses") and issue_matches_constraints(
                        issue,
                        {**constraints, "statuses": []},
                        search_text=combined_text,
                    ):
                        status_excluded_count += 1
                    continue
                if text_matches_keyword_terms(combined_text, keywords):
                    found_count += 1
                    if issue_id:
                        matched_ids.append(issue_id)
                        matched_groups[issue_id] = (issue.get("project") or {}).get("name") or "미상"
                    scored = {**issue, "_assistant_match_score": mantis_keyword_match_score(issue, keywords)}
                    filtered_issues.append(scored)
                    _sort_search_matches(filtered_issues, constraints)
                    del filtered_issues[limit:]
            source_meta = collection_metadata(raw_issues)
            searched_pages = 0 if stored is not None else min(len(raw_issues) // page_size + 1, max_pages)
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Mantis connection failed: {exc}") from exc

    _sort_search_matches(filtered_issues, constraints)
    target_batch = filtered_issues[:limit]

    return {
        "source": "mantis_page_scan",
        "status": "ok",
        "meta": {
            "search_keyword": search_keyword,
            "found_count": found_count,
            "matched_customer_groups": matched_groups,
            "displayed_count": len(target_batch),
            "matched_issue_ids": sorted(matched_ids),
            "scanned_issue_ids": sorted(scanned_issue_ids),
            **source_meta,
            "searched_pages": searched_pages,
            "status_excluded_count": status_excluded_count,
        },
        "issues_list": [normalize_issue(issue, search_terms=keywords + constraint_evidence_terms(constraints or {}))
                        for issue in target_batch],
    }


# 기존 import 경로는 실제 소유자의 동일 객체를 재내보낸다.
from backend.sources.mantis_fields import (
    get_issue_custom_field_value as get_issue_custom_field_value,
    mantis_date_search_variants as mantis_date_search_variants,
    mantis_enum_label as mantis_enum_label,
    normalize_issue as normalize_issue,
    issue_created_year_month as issue_created_year_month,
    issue_to_search_text as issue_to_search_text,
    mantis_issue_effort_date as mantis_issue_effort_date,
    build_sources_from_issue_rows as build_sources_from_issue_rows,
)
from backend.sources.mantis_matching import (
    issue_matches_date_fields as issue_matches_date_fields,
    issue_matches_created_at as issue_matches_created_at,
    issue_matches_platform as issue_matches_platform,
    mantis_keyword_match_score as mantis_keyword_match_score,
    issue_matches_screen_numbers as issue_matches_screen_numbers,
    issue_matches_constraints as issue_matches_constraints,
    select_mantis_rows as select_mantis_rows,
)
from backend.sources.mantis_client import (
    _mantis_request_slots as _mantis_request_slots,
    fetch_mantis_issues as fetch_mantis_issues,
    fetch_mantis_issue_by_id as fetch_mantis_issue_by_id,
    _fetch_mantis_issue_by_id as _fetch_mantis_issue_by_id,
)

from backend.storage.sqlite import open_sqlite
from backend.storage.profiles import CHROMA
