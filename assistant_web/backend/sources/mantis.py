"""Mantis 이슈 소스 어댑터 — API 호출·정규화·포맷·키워드 스코어링(Layer 3).

Mantis REST API 조회, 이슈 정규화, 커스텀 날짜/열거형 포맷, 필터 로딩,
스냅샷 조회 한도 파싱, 키워드 매칭 점수 계산을 담당한다.

제약조건 매칭(issue_matches_constraints), Mantis 검색 실행(execute_mantis_*),
스냅샷 신선도 확인까지 이 어댑터가 담당한다.
"""

import json
import re
import sqlite3
from datetime import date, datetime
from functools import lru_cache
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
from backend.query_filters import normalize_filter_clauses, values_for_clause
from backend.tools.mantis_snapshot import scan_mantis_documents
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
        "project": issue.get("project", {}).get("name", "미상"),
        "summary": issue.get("summary", "제목 없음"),
        "status": mantis_enum_label(issue.get("status"), "상태 미상"),
        "priority": mantis_enum_label(issue.get("priority"), ""),
        "severity": mantis_enum_label(issue.get("severity"), ""),
        "media_type": media_type,
        "screen_number": screen_number,
        "work_subject": work_subject,
        "effort_fields": effort_fields,
        "date_fields": date_fields,
        "reporter": issue.get("reporter", {}).get("real_name", "미상"),
        "handler": issue.get("handler", {}).get("real_name", "담당자 미지정"),
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


def fetch_mantis_issues(params: Dict[str, Any]) -> List[Dict[str, Any]]:
    settings = get_mantis_settings()
    response = requests.get(
        settings["url"],
        headers=get_mantis_headers(),
        params=params,
        timeout=30,
    )
    if response.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Mantis API failed: {response.status_code} {response.text[:300]}",
        )
    return response.json().get("issues", [])


def fetch_mantis_issue_by_id(issue_id: int) -> Optional[Dict[str, Any]]:
    if not issue_id:
        return None

    settings = get_mantis_settings()
    issue_url = f"{settings['url'].rstrip('/')}/{issue_id}"
    try:
        response = requests.get(
            issue_url,
            headers=get_mantis_headers(),
            timeout=20,
        )
    except requests.RequestException:
        return None

    if response.status_code != 200:
        return None

    issues = response.json().get("issues", [])
    if not issues:
        return None
    return issues[0]


def fetch_mantis_recent_issues(total_limit: int = 800, page_size: int = 100) -> List[Dict[str, Any]]:
    total_limit = max(1, min(total_limit, 3000))
    page_size = max(1, min(page_size, 100))
    issues: List[Dict[str, Any]] = []
    page = 1
    while len(issues) < total_limit:
        batch = fetch_mantis_issues(
            {
                "page_size": page_size,
                "page": page,
            }
        )
        if not batch:
            break
        issues.extend(batch)
        if len(batch) < page_size:
            break
        page += 1
    issues = issues[:total_limit]
    issues.sort(key=lambda issue: int(issue.get("id") or 0), reverse=True)
    return issues


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
    notes_text = " ".join(note.get("text", "") for note in issue.get("notes", []))
    return " ".join(
        [
            str(issue.get("project", {}).get("name", "")),
            str(issue.get("summary", "")),
            str(issue.get("description", "")),
            str(issue.get("steps_to_reproduce", "")),
            str(issue.get("reporter", {}).get("real_name", "")),
            str(issue.get("handler", {}).get("real_name", "")),
            custom_text,
            notes_text,
        ]
    )


def mantis_keyword_match_score(issue: Dict[str, Any], keywords: List[str]) -> int:
    if not keywords:
        return 0
    title = normalize_for_match(str(issue.get("summary", "")))
    project = normalize_for_match(str(issue.get("project", {}).get("name", "")))
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
                " ".join(note.get("text", "") for note in issue.get("notes", [])),
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
        str(issue.get("project", {}).get("name", ""))
    ) != normalize_for_match(constraints["customer_exact"]):
        return False
    if constraints.get("customer_aliases") and not text_matches_customer_aliases(
        str(issue.get("project", {}).get("name", "")),
        constraints["customer_aliases"],
    ):
        return False
    project_name = str(issue.get("project", {}).get("name", ""))
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
    status_text = str(issue.get("status", {}).get("name") or issue.get("status", {}).get("label", ""))
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
    handler_text = str(issue.get("handler", {}).get("real_name", ""))
    if constraints.get("unassigned_people") and handler_text.strip() not in {
        "",
        "담당자 미지정",
        "미지정",
        "미상",
    }:
        return False
    if not text_matches_people(combined_text, constraints.get("people", {}), handler_text):
        return False
    reporter_text = str(issue.get("reporter", {}).get("real_name", ""))
    field_text = {
        "title": str(issue.get("summary", "")),
        "keyword": combined_text,
        "owner": handler_text,
        "reporter": reporter_text,
    }
    for clause in normalize_filter_clauses(constraints.get("filter_clauses") or []):
        if clause["field"] not in field_text:
            continue
        candidate = normalize_for_match(field_text[clause["field"]])
        values = [normalize_for_match(value) for value in values_for_clause(clause.get("value"))]
        operator = clause["operator"]
        if operator in {"equals", "in"} and not any(value == candidate for value in values):
            return False
        if operator in {"not_equals", "not_in"} and any(value == candidate for value in values):
            return False
        if operator == "contains" and not any(value in candidate for value in values):
            return False
        if operator == "not_contains" and any(value in candidate for value in values):
            return False
        if operator == "exists" and not candidate:
            return False
        if operator == "not_exists" and candidate:
            return False
    return True


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
) -> Optional[Dict[str, Dict[str, Any]]]:
    """라이브 Mantis API를 페이징하며 제약(고객사/상태/등록일 범위 등)에 맞는 이슈를
    ID별로 수집한다. 라이브 이슈는 실제 created_at을 갖고 있어 날짜 필터가 정확하다.

    로컬 스냅샷은 재구축 시점(예: 2026-05-07)의 옛 포맷이라 상당수 문서에 등록일이
    없어 월/연 단위 카운트가 0으로 새는 문제가 있다. 날짜 조건이 있는 집계에서는 이
    함수로 라이브 결과를 병합해 정확도를 보완한다. 실패하면 None을 반환해 호출부가
    스냅샷 단독 집계로 폴백하게 한다."""
    settings = get_mantis_settings()
    headers = get_mantis_headers()
    collected: Dict[str, Dict[str, Any]] = {}
    try:
        for page in range(1, max_pages + 1):
            response = requests.get(
                settings["url"],
                headers=headers,
                params={"page_size": page_size, "page": page},
                timeout=30,
            )
            if response.status_code != 200:
                return None
            issues = response.json().get("issues", [])
            if not issues:
                break
            for issue in issues:
                if issue_matches_constraints(issue, constraints):
                    issue_id = issue.get("id")
                    if issue_id is not None:
                        collected[str(issue_id)] = issue
            if len(issues) < page_size:
                break
    except requests.RequestException:
        return None
    return collected


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
            "missing_ids": missing_ids,
            "displayed_count": len(issues),
        },
        "issues_list": [normalize_issue(issue) for issue in issues],
    }


@lru_cache(maxsize=4)
def _snapshot_freshness_for_mtime(_mtime: float) -> str:
    """스냅샷 문서의 최신 등록일을 SQLite 내부에서 집계한다.

    문서 본문 전체를 Python으로 전송하던 방식은 데이터가 커질수록 첫 요청이 수십 초
    지연됐다. 최신 포맷의 ``등록일: YYYY-MM-DD`` 헤더를 SQL에서 잘라 MAX로 계산하고,
    파일 mtime을 캐시 키로 사용해 재구축 전까지 재계산하지 않는다.
    """
    # 과거 이슈 본문에 "2032-07-27 메일 수신" 같은 오타 날짜가 실존한다.
    # 미래 날짜는 데이터 기준일이 될 수 없으므로 오늘 이후 값은 무시한다.
    today = date.today().isoformat()
    try:
        with sqlite3.connect(CHROMA_DB_PATH) as connection:
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
    if not CHROMA_DB_PATH.exists():
        return ""
    return _snapshot_freshness_for_mtime(CHROMA_DB_PATH.stat().st_mtime)


def get_mantis_snapshot_freshness_status() -> Dict[str, Any]:
    """Return a UI-friendly age and health status for the local snapshot."""
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
            "scanned_count": snapshot["scanned_count"],
            "found_count": snapshot["matched_count"],
            "displayed_count": len(snapshot["issues"]),
            "applied_filters": constraints,
            "tool": "mantis_chroma_snapshot",
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
        or any(
            clause.get("field") in {"owner", "status"}
            for clause in constraints.get("filter_clauses", [])
        )
    )
    if stage.get("status") != "ok" or not mutable_filter:
        return stage

    refreshed: List[Dict[str, Any]] = []
    revalidated_count = 0
    removed_count = 0
    fallback_count = 0
    for snapshot_issue in stage.get("issues_list", []):
        issue_id = int(snapshot_issue.get("id") or 0)
        live_issue = fetch_mantis_issue_by_id(issue_id) if issue_id else None
        if live_issue is None:
            refreshed.append(snapshot_issue)
            fallback_count += 1
            continue
        revalidated_count += 1
        if issue_matches_constraints(live_issue, constraints):
            refreshed.append(normalize_issue(live_issue))
        else:
            removed_count += 1

    return {
        **stage,
        "issues_list": refreshed,
        "meta": {
            **(stage.get("meta") or {}),
            "displayed_count": len(refreshed),
            "live_revalidated_count": revalidated_count,
            "stale_removed_count": removed_count,
            "live_fallback_count": fallback_count,
        },
    }


def execute_mantis_page_scan(
    search_keyword: str,
    limit: int = 5,
    constraints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    settings = get_mantis_settings()

    keywords = [kw.strip().casefold() for kw in search_keyword.split() if kw.strip()]
    filtered_issues: List[Dict[str, Any]] = []
    status_excluded_count = 0
    page = 1
    page_size = 100
    max_pages = 8
    headers = get_mantis_headers()

    try:
        while page <= max_pages:
            response = requests.get(
                settings["url"],
                headers=headers,
                params={"page_size": page_size, "page": page},
                timeout=30,
            )
            if response.status_code != 200:
                raise HTTPException(
                    status_code=502,
                    detail=f"Mantis API failed: {response.status_code} {response.text[:300]}",
                )

            issues = response.json().get("issues", [])
            if not issues:
                break

            for issue in issues:
                combined_text = issue_to_search_text(issue)
                normalized_text = combined_text.casefold()
                if constraints and has_constraint(constraints) and not issue_matches_constraints(
                    issue,
                    constraints,
                ):
                    # "확인된 이슈"처럼 상태 필터가 걸린 질문에서 다른 조건은 전부
                    # 만족하는데 상태만 달라 탈락한 이슈를 센다. 결과가 예상보다
                    # 적을 때 사용자에게 "상태 조건 제외 시 N건 더 있음"을 알려
                    # 의도와 해석이 어긋난 경우 스스로 복구할 수 있게 한다.
                    if constraints.get("statuses") and issue_matches_constraints(
                        issue,
                        {**constraints, "statuses": []},
                    ):
                        status_excluded_count += 1
                    continue
                if not keywords or all(keyword in normalized_text for keyword in keywords):
                    issue["_assistant_match_score"] = mantis_keyword_match_score(issue, keywords)
                    filtered_issues.append(issue)

            # API가 마지막 페이지임을 알려주면 더 이상 호출하지 않는다. 요청 건수를
            # 이미 채웠더라도 여기서 중단하지 않아야, 뒤 페이지의 더 최신 ID를
            # 전역 정렬 후보에 포함할 수 있다.
            if len(issues) < page_size:
                break
            page += 1
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Mantis connection failed: {exc}")

    if not filtered_issues and len(keywords) > 1 and not (constraints and has_constraint(constraints)):
        fallback_keyword = keywords[0]
        response = requests.get(
            settings["url"],
            headers=headers,
            params={"page_size": page_size, "page": 1},
            timeout=30,
        )
        if response.status_code == 200:
            filtered_issues = [
                issue
                for issue in response.json().get("issues", [])
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
            "displayed_count": len(target_batch),
            "searched_pages": min(page, max_pages),
            "status_excluded_count": status_excluded_count,
        },
        "issues_list": [normalize_issue(issue) for issue in target_batch],
    }
