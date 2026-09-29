"""Mantis 이슈 소스 어댑터 — API 호출·정규화·포맷·키워드 스코어링(Layer 3).

Mantis REST API 조회, 이슈 정규화, 커스텀 날짜/열거형 포맷, 필터 로딩,
스냅샷 조회 한도 파싱, 키워드 매칭 점수 계산을 담당한다.

제약조건 매칭(issue_matches_constraints), Mantis 검색 실행(execute_mantis_*),
스냅샷 신선도 확인까지 이 어댑터가 담당한다.
"""

import json
import os
import re
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime
from functools import lru_cache
from threading import BoundedSemaphore
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException

from backend.common import (
    format_mantis_custom_date,
    normalize_chroma_issue_document,
    normalize_for_match,
    text_matches_any_alias,
    text_matches_customer_aliases,
)
from backend.config import (
    CHROMA_DB_PATH,
    CONFIG_DIR,
    MANTIS_DATE_CUSTOM_FIELDS,
    get_mantis_headers,
    get_mantis_settings,
)
from backend.query_planning import plan_created_at_matches
from backend.query_filters import matches_source_filters, screen_filter_values
from backend.tools.mantis_snapshot import scan_mantis_documents
from backend.sources.mantis_collection import clear_recent_cache, fetch_recent_cached, collection_metadata
from backend.sources.mantis_store import read_complete_store, read_store_metadata
from backend.understanding import (
    chroma_text_matches_constraints,
    field_matches_screen_number,
    has_constraint,
    text_matches_people,
    text_matches_status,
    title_has_screen_number,
)


def get_issue_custom_field_value(issue: Dict[str, Any], field_name: str) -> str:
    for custom_field in issue.get("custom_fields", []):
        if custom_field.get("field", {}).get("name") == field_name:
            return str(custom_field.get("value", "")).strip()
    return ""


def issue_matches_date_fields(issue: Dict[str, Any], date_fields: Dict[str, str]) -> bool:
    if not date_fields:
        return True
    return all(
        format_mantis_custom_date(get_issue_custom_field_value(issue, field_name)) == expected_date
        for field_name, expected_date in date_fields.items()
    )


def issue_matches_created_at(issue: Dict[str, Any], created_at: Optional[Dict[str, str]]) -> bool:
    return plan_created_at_matches(str(issue.get("created_at") or ""), created_at)


def issue_matches_platform(issue: Dict[str, Any], platform_aliases: List[str]) -> bool:
    """플랫폼(HTS/MTS) 조건은 신뢰 가능한 '매체구분' 커스텀 필드를 우선 사용한다.

    본문에는 "HTS에서는 안 되는데 MTS에서는 됩니다"처럼 상대 플랫폼이 비교/언급되는
    경우가 흔해서, 본문 전체를 fuzzy하게 검색하면 실제로는 매체구분=HTS인 이슈가
    MTS 조건에도 잘못 걸리는 오탐이 난다. '매체구분' 필드가 채워진 이슈는 그 값으로만
    판정하고, 필드가 비어 있는 과거 이슈에 한해서만 본문 전체 검색으로 폴백한다."""
    if not platform_aliases:
        return True
    media_type = get_issue_custom_field_value(issue, "매체구분")
    if media_type:
        return text_matches_any_alias(media_type, platform_aliases)
    return text_matches_any_alias(issue_to_search_text(issue), platform_aliases)


def mantis_date_search_variants(value: Any) -> str:
    formatted = format_mantis_custom_date(value)
    raw_value = str(value or "").strip()
    variants = [raw_value, formatted]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", formatted):
        year, month, day = formatted.split("-")
        variants.extend(
            [
                f"{year}{month}{day}",
                f"{year}년 {int(month)}월 {int(day)}일",
                f"{year}년 {int(month)}월",
            ]
        )
    return " ".join(dict.fromkeys(variant for variant in variants if variant))


def mantis_enum_label(value: Any, default: str = "") -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("label") or default)
    return str(value or default)


def normalize_issue(issue: Dict[str, Any]) -> Dict[str, Any]:
    media_type = "기타/미상"
    screen_number = "미상"
    work_subject = "미상"
    effort_fields: Dict[str, str] = {}
    date_fields: Dict[str, str] = {}
    for custom_field in issue.get("custom_fields", []):
        field_name = custom_field.get("field", {}).get("name")
        field_value = custom_field.get("value", "")
        if field_name == "매체구분":
            media_type = field_value or media_type
        elif field_name == "화면번호":
            screen_number = field_value or screen_number
        elif field_name == "작업주체":
            work_subject = field_value or work_subject
        elif field_name in MANTIS_DATE_CUSTOM_FIELDS:
            date_fields[str(field_name)] = format_mantis_custom_date(field_value)
        elif field_name and "공수" in field_name:
            effort_fields[str(field_name)] = str(field_value)
    if screen_number == "미상":
        title_screen = re.match(r"^\s*\[\s*(\d{3,5})\s*\]", str(issue.get("summary") or ""))
        if title_screen:
            screen_number = title_screen.group(1)

    notes = [
        {
            "id": note.get("id"),
            "reporter": (note.get("reporter") or {}).get("real_name", "미상"),
            "created_at": str(note.get("created_at", ""))[:19],
            "updated_at": str(note.get("updated_at", ""))[:19],
            "text": str(note.get("text", ""))[:2400],
        }
        for note in (issue.get("notes") or [])[-12:]
        if note.get("text")
    ]
    history = [
        {
            "created_at": str(item.get("created_at", ""))[:19],
            "user": (item.get("user") or {}).get("real_name", "미상"),
            "field": (item.get("field") or {}).get("label") or (item.get("field") or {}).get("name") or "",
            "message": str(item.get("message") or ""),
            "change": str(item.get("change") or ""),
        }
        for item in (issue.get("history") or [])[-40:]
    ]

    return {
        "id": issue.get("id"),
        "project": (issue.get("project") or {}).get("name", "미상"),
        "summary": issue.get("summary", "제목 없음"),
        "status": mantis_enum_label(issue.get("status"), "상태 미상"),
        "priority": mantis_enum_label(issue.get("priority"), ""),
        "severity": mantis_enum_label(issue.get("severity"), ""),
        "media_type": media_type,
        "screen_number": screen_number,
        "work_subject": work_subject,
        "effort_fields": effort_fields,
        "date_fields": date_fields,
        "reporter": (issue.get("reporter") or {}).get("real_name", "미상"),
        "handler": (issue.get("handler") or {}).get("real_name", "담당자 미지정"),
        "created_at": str(issue.get("created_at", ""))[:10],
        "updated_at": str(issue.get("updated_at", ""))[:10],
        "description": str(issue.get("description", ""))[:2400],
        "notes": notes,
        "history": history,
    }


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


_mantis_request_slots = BoundedSemaphore(6)

# 정상 페이지 요청은 6초 안에 끝난다. 드물게 응답 없이 멈추는 요청을 45초까지
# 기다리면 첫 질문이 50초를 넘고, 캐시 TTL도 지나 결과가 저장되지 않는다.
MANTIS_PAGE_SCAN_TIMEOUT_SECONDS = 12


def fetch_mantis_issues(
    params: Dict[str, Any],
    timeout: int = 30,
    attempts: int = 3,
) -> List[Dict[str, Any]]:
    settings = get_mantis_settings()
    safe_attempts = max(1, min(int(attempts), 3))
    for attempt in range(1, safe_attempts + 1):
        try:
            with _mantis_request_slots:
                response = requests.get(
                    settings["url"],
                    headers=get_mantis_headers(),
                    params=params,
                    timeout=max(3, min(int(timeout), 60)),
                )
        except (requests.ConnectionError, requests.Timeout):
            if attempt == safe_attempts:
                raise
            time.sleep(2 ** (attempt - 1))
            continue

        retryable_status = response.status_code == 429 or response.status_code >= 500
        if retryable_status and attempt < safe_attempts:
            time.sleep(2 ** (attempt - 1))
            continue
        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Mantis API failed: {response.status_code} {response.text[:300]}",
            )
        return response.json().get("issues", [])
    return []


def fetch_mantis_issue_by_id(issue_id: int) -> Optional[Dict[str, Any]]:
    if not issue_id:
        return None

    settings = get_mantis_settings()
    issue_url = f"{settings['url'].rstrip('/')}/{issue_id}"
    for attempt in range(1, 4):
        try:
            response = requests.get(
                issue_url,
                headers=get_mantis_headers(),
                timeout=20,
            )
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 3:
                raise
            time.sleep(2 ** (attempt - 1))
            continue

        retryable_status = response.status_code == 429 or response.status_code >= 500
        if retryable_status and attempt < 3:
            time.sleep(2 ** (attempt - 1))
            continue
        if retryable_status:
            raise HTTPException(
                status_code=502,
                detail=f"Mantis API failed: {response.status_code} {response.text[:300]}",
            )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Mantis 이슈 접근이 거절되었습니다.")

        issues = response.json().get("issues", [])
        return issues[0] if issues else None
    return None


def fetch_mantis_recent_issues(
    total_limit: int = 800,
    page_size: int = 100,
    request_timeout: int = 30,
    *,
    force_refresh: bool = False,
) -> List[Dict[str, Any]]:
    """API 선두 범위를 병렬 수집한 뒤 ID순으로 정렬한다(전역 최신순 보장 없음)."""
    stored = None if force_refresh else read_complete_store()
    if stored is not None:
        # 명시적인 최신 N건 요청은 전체 원본을 정렬한 뒤 자른다.
        from backend.sources.mantis_collection import IssueBatch
        return IssueBatch(stored[:total_limit], {**stored.meta, "search_complete": len(stored) <= total_limit})
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


def issue_created_year_month(issue: Dict[str, Any]) -> str:
    created_at = str(issue.get("created_at") or "")[:10]
    if re.match(r"\d{4}-\d{2}-\d{2}", created_at):
        return created_at[:7]
    return ""


def issue_to_search_text(issue: Dict[str, Any]) -> str:
    custom_text_parts = []
    for field in issue.get("custom_fields", []):
        field_name = str(field.get("field", {}).get("name", ""))
        field_value = field.get("value", "")
        if field_name in MANTIS_DATE_CUSTOM_FIELDS:
            custom_text_parts.append(f"{field_name} {mantis_date_search_variants(field_value)}")
        else:
            custom_text_parts.append(f"{field_name} {field_value}")
    custom_text = " ".join(custom_text_parts)
    notes_text = " ".join(str(note.get("text") or "") for note in (issue.get("notes") or []))
    return " ".join(
        [
            str((issue.get("project") or {}).get("name", "")),
            str(issue.get("summary", "")),
            str(issue.get("description", "")),
            str(issue.get("steps_to_reproduce", "")),
            str((issue.get("reporter") or {}).get("real_name", "")),
            str((issue.get("handler") or {}).get("real_name", "")),
            custom_text,
            notes_text,
        ]
    )


def mantis_keyword_match_score(issue: Dict[str, Any], keywords: List[str]) -> int:
    if not keywords:
        return 0
    title = normalize_for_match(str(issue.get("summary", "")))
    project = normalize_for_match(str((issue.get("project") or {}).get("name", "")))
    custom_fields = normalize_for_match(
        " ".join(
            f"{field.get('field', {}).get('name', '')} {field.get('value', '')}"
            for field in issue.get("custom_fields", [])
        )
    )
    body = normalize_for_match(
        " ".join(
            [
                str(issue.get("description", "")),
                str(issue.get("steps_to_reproduce", "")),
                " ".join(str(note.get("text") or "") for note in (issue.get("notes") or [])),
            ]
        )
    )
    score = 0
    for keyword in keywords:
        normalized = normalize_for_match(keyword)
        if normalized in title:
            score += 40
        elif normalized in custom_fields:
            score += 30
        elif normalized in project:
            score += 20
        elif normalized in body:
            score += 10
    return score


def mantis_issue_effort_date(issue: Dict[str, Any]) -> str:
    return str(issue.get("created_at", ""))[:10]


def issue_matches_screen_numbers(issue: Dict[str, Any], screen_numbers: List[str]) -> bool:
    if not screen_numbers:
        return True
    screen_field = get_issue_custom_field_value(issue, "화면번호")
    title = str(issue.get("summary", ""))
    return any(
        field_matches_screen_number(screen_field, screen_number)
        or title_has_screen_number(title, screen_number)
        for screen_number in screen_numbers
    )


def issue_matches_constraints(issue: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    if issue.get("id") in constraints.get("exclude_issue_ids", []):
        return False
    max_issue_id = constraints.get("max_issue_id_exclusive")
    if max_issue_id and int(issue.get("id") or 0) >= int(max_issue_id):
        return False
    if constraints.get("customer_exact") and normalize_for_match(
        str((issue.get("project") or {}).get("name", ""))
    ) != normalize_for_match(constraints["customer_exact"]):
        return False
    if constraints.get("customer_aliases") and not text_matches_customer_aliases(
        str((issue.get("project") or {}).get("name", "")),
        constraints["customer_aliases"],
    ):
        return False
    project_name = str((issue.get("project") or {}).get("name", ""))
    if constraints.get("excluded_customer_aliases") and text_matches_customer_aliases(
        project_name,
        constraints["excluded_customer_aliases"],
    ):
        return False
    if constraints.get("platform_aliases") and not issue_matches_platform(
        issue,
        constraints["platform_aliases"],
    ):
        return False
    if constraints.get("excluded_platform_aliases") and issue_matches_platform(
        issue,
        constraints["excluded_platform_aliases"],
    ):
        return False
    combined_text = issue_to_search_text(issue)
    status_text = str((issue.get("status") or {}).get("name") or (issue.get("status") or {}).get("label", ""))
    if not text_matches_status(
        status_text,
        constraints.get("statuses", []),
    ):
        return False
    if constraints.get("excluded_statuses") and text_matches_status(
        status_text,
        constraints["excluded_statuses"],
    ):
        return False
    if constraints.get("issue_ids") and issue.get("id") not in constraints["issue_ids"]:
        return False
    if not issue_matches_screen_numbers(issue, constraints.get("screen_numbers", [])):
        return False
    if not issue_matches_date_fields(issue, constraints.get("date_fields", {})):
        return False
    if not issue_matches_created_at(issue, constraints.get("created_at")):
        return False
    handler_text = str((issue.get("handler") or {}).get("real_name", ""))
    if constraints.get("unassigned_people") and handler_text.strip() not in {
        "",
        "담당자 미지정",
        "미지정",
        "미상",
    }:
        return False
    if not text_matches_people(combined_text, constraints.get("people", {}), handler_text):
        return False
    reporter_text = str((issue.get("reporter") or {}).get("real_name", ""))
    return matches_source_filters("mantis", {
        "customer": project_name,
        "platform": get_issue_custom_field_value(issue, "매체구분") or combined_text,
        "status": status_text,
        "issue_id": str(issue.get("id") or ""),
        "screen_number": screen_filter_values(get_issue_custom_field_value(issue, "화면번호"), str(issue.get("summary") or "")),
        "created_at": issue.get("created_at", ""),
        "title": str(issue.get("summary", "")), "keyword": combined_text,
        "owner": handler_text, "reporter": reporter_text,
    }, constraints.get("filter_clauses") or [])


def build_sources_from_issue_rows(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "type": issue.get("type", "mantis"),
            "source": issue.get("source", ""),
            "issue_id": issue.get("id"),
            "title": issue.get("summary", "제목 없음"),
            "project": issue.get("project", "미상"),
            "status": issue.get("status", "상태 미상"),
            "priority": issue.get("priority", ""),
            "severity": issue.get("severity", ""),
            "handler": issue.get("handler", "담당자 미지정"),
            "created_at": issue.get("created_at", ""),
            "preview": issue.get("preview") or issue.get("description", ""),
            "detail": issue.get("detail") or issue,
        }
        for issue in issues
    ]


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
    stored = read_complete_store()
    if stored is not None:
        selected = stored[:scan_limit] if scan_limit else stored
        return {"status": "ok", "error_kind": "", "reason": "", "complete": True,
                "issues": {str(row["id"]): row for row in selected if issue_matches_constraints(row, constraints)},
                "scanned_pages": 0, "scanned_ids": [int(row["id"]) for row in selected],
                "fetchedAt": stored.meta["fetchedAt"]}
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
        "issues_list": [normalize_issue(issue) for issue in issues],
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
        with closing(sqlite3.connect(CHROMA_DB_PATH)) as connection, connection:
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
        (constraints.get("people") or {}).get("names")
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
            refreshed.append(normalize_issue(live_issue))
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
    refill_candidates = sorted(matched_issue_ids - checked_issue_ids, reverse=True)
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
            refreshed.append(normalize_issue(live_issue))
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


def execute_mantis_page_scan(
    search_keyword: str,
    limit: int = 5,
    constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    keywords = [kw.strip().casefold() for kw in search_keyword.split() if kw.strip()]
    filtered_issues: List[Dict[str, Any]] = []
    scanned_issue_ids: set[int] = set()
    status_excluded_count = 0
    page_size = 50
    max_pages = 16

    try:
        # 기존과 같은 50건 × 16페이지를 전부 읽은 뒤 조건을 적용한다.
        # API 정렬이 보장되지 않으므로 표시 건수를 채워도 조기 종료하지 않는다.
        stored = read_complete_store()
        raw_issues = stored if stored is not None else _fetch_mantis_raw_issues(
            max_pages * page_size, page_size, MANTIS_PAGE_SCAN_TIMEOUT_SECONDS,
        )
        for issue in raw_issues:
            issue_id = int(issue.get("id") or 0)
            if issue_id:
                scanned_issue_ids.add(issue_id)
            combined_text = issue_to_search_text(issue)
            normalized_text = combined_text.casefold()
            if constraints and has_constraint(constraints) and not issue_matches_constraints(
                issue,
                constraints,
            ):
                # 상태만 달라 탈락한 항목 수를 유지해 조건 안내에 사용한다.
                if constraints.get("statuses") and issue_matches_constraints(
                    issue,
                    {**constraints, "statuses": []},
                ):
                    status_excluded_count += 1
                continue
            if not keywords or all(keyword in normalized_text for keyword in keywords):
                filtered_issues.append({**issue, "_assistant_match_score": mantis_keyword_match_score(issue, keywords)})
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Mantis connection failed: {exc}") from exc

    if not filtered_issues and len(keywords) > 1 and not (constraints and has_constraint(constraints)):
        fallback_keyword = keywords[0]
        # 폴백은 기존처럼 첫 페이지에서만 찾되 같은 수집 세대의 원본을 재사용한다.
        filtered_issues = [
            issue
            for issue in raw_issues[:page_size]
            if fallback_keyword
            in f"{issue.get('project', {}).get('name', '')} {issue.get('summary', '')}".lower()
        ]

    filtered_issues.sort(
        key=lambda issue: (issue.get("_assistant_match_score", 0), issue.get("id", 0)),
        reverse=True,
    )
    target_batch = filtered_issues[:limit]

    return {
        "source": "mantis_page_scan",
        "status": "ok",
        "meta": {
            "search_keyword": search_keyword,
            "found_count": len(filtered_issues),
            "matched_customer_groups": {
                issue["id"]: (issue.get("project") or {}).get("name") or "미상"
                for issue in filtered_issues if issue.get("id")
            },
            "displayed_count": len(target_batch),
            "matched_issue_ids": sorted(
                int(issue.get("id") or 0)
                for issue in filtered_issues
                if issue.get("id")
            ),
            "scanned_issue_ids": sorted(scanned_issue_ids),
            **collection_metadata(raw_issues),
            "searched_pages": 0 if stored is not None else min(len(raw_issues) // page_size + 1, max_pages),
            "status_excluded_count": status_excluded_count,
        },
        "issues_list": [normalize_issue(issue) for issue in target_batch],
    }
