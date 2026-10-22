#!/usr/bin/env python3
"""Run reproducible end-to-end accuracy checks against live Mantis and Notion data."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import time
import uuid
from copy import deepcopy
from collections import Counter
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from scripts.accuracy._runtime import reexec_in_project_venv

if __name__ == "__main__":
    reexec_in_project_venv(APP_DIR)

import requests

from backend.environment import load_dotenv
from backend.evaluation.verification import ungrounded_constraint_labels
from backend.intent.search_terms import extract_requested_limit
from backend.notion.search import extract_notion_constraints

load_dotenv(APP_DIR / ".env")

from backend.config import CUSTOMER_ALIASES, NOTION_API_BASE, discover_notion_database_ids, parse_notion_database_ids
from backend.config import CUSTOMER_DIRECTORY
from backend.common import customer_alias_owner, display_mantis_status_label
from backend.display import customer_name
from backend.mantis_status import canonical_mantis_status
from backend.llm import get_model_registry
from backend.schemas import ChatMessage, ChatRequest
from backend.services.chat_service import chat_service
from backend.sources import mantis
from backend.sources.mantis_store import read_complete_store, read_store_metadata, refresh_in_progress, sync_complete_store
from backend.understanding import extract_query_constraints
from backend.team_scope import TEAM_MEMBERS


REPORT_DIR = APP_DIR / "runtime" / "accuracy_reports"
# 외부 계정 생성 없이 실행별 감사 문맥을 분리한다. 기본/후속 호출이 같은
# UID를 사용해야 웹 요청처럼 직전의 확정 조건을 안전하게 복원할 수 있다.
ACCURACY_USER_UID = "accuracy-" + uuid.uuid4().hex
MANTIS_PAGES = 16
MANTIS_PAGE_SIZE = 50
MANTIS_REQUEST_ATTEMPTS = 3
MANTIS_REQUEST_TIMEOUT = (10, 45)
NOTION_PAGES = 3
NOTION_PAGE_SIZE = 100


@dataclass
class AccuracyCase:
    id: str
    source: str
    category: str
    question: str
    expected_kind: str
    expected: Any
    mutable_oracle: bool = False
    expected_constraints: Optional[Dict[str, Any]] = None
    oracle_filters: Optional[Dict[str, str]] = None


@dataclass
class FollowUpScenario:
    id: str
    source: str
    category: str
    parent_case_id: str
    question: str
    expected_kind: str
    expected: Any


def normalize(value: Any) -> str:
    return re.sub(r"[\s\W_]+", "", str(value or "").casefold())


def human_project_name(value: str) -> str:
    display = customer_name(value)
    if display != value:
        return display
    match = re.search(r"\(([^()]*(?:증권|금융|투자)[^()]*)\)", value)
    return match.group(1).strip() if match else value


def oracle_project_group(value: str) -> str:
    """원본 프로젝트 코드와 조사된 이름표만 사용하고 검색기 매처는 사용하지 않는다."""
    code = re.sub(r"_Project$", "", value.split("(", 1)[0].strip(), flags=re.IGNORECASE)
    return next((key for key, entry in CUSTOMER_DIRECTORY.items() if code in entry.get("projects", [])), value)


def customer_answer_aliases(project: str) -> List[str]:
    """원본 프로젝트에 대응하는 표시 이름도 정답으로 인정한다."""
    aliases = [human_project_name(project), project]
    matching_groups = [values for values in CUSTOMER_ALIASES.values()
                       if any(normalize(value) in normalize(project) for value in values)]
    # 겹치는 짧은 별칭보다 프로젝트를 가장 구체적으로 식별한 그룹을 사용한다.
    if matching_groups:
        selected = max(matching_groups, key=lambda values: max(
            len(normalize(value)) for value in values if normalize(value) in normalize(project)
        ))
        aliases.extend(selected)
    return list(dict.fromkeys(aliases))


def status_name(issue: Dict[str, Any]) -> str:
    status = issue.get("status") or {}
    return str(status.get("name") or status.get("label") or "").strip()


def custom_field(issue: Dict[str, Any], name: str) -> str:
    for field in issue.get("custom_fields") or []:
        if (field.get("field") or {}).get("name") == name:
            return str(field.get("value") or "").strip()
    return ""


def exact_screen_numbers(issue: Dict[str, Any]) -> set[str]:
    """Mirror the app's trusted screen-number semantics for the test oracle.

    A screen number is valid when the custom field equals the number or the title
    contains the exact ``[1234]`` marker. Free-text mentions are intentionally
    excluded so the generated expected set and production filter cannot drift.
    """
    values = set(re.findall(r"\[\s*(\d{3,5})\s*\]", str(issue.get("summary") or "")))
    field_value = custom_field(issue, "화면번호")
    if re.fullmatch(r"\d{3,5}", normalize(field_value)):
        values.add(normalize(field_value))
    return values


def mantis_oracle_matches(issue: Dict[str, Any], filters: Dict[str, str]) -> bool:
    """사례 생성 시 고정한 원본 필드만 비교한다. 검색기의 조건 추출/매처는 쓰지 않는다."""
    scope = filters.get("scope") or ("target" if filters.get("handler") else "team")
    if scope == "team" and normalize((issue.get("handler") or {}).get("real_name")) not in {
        normalize(name) for name in TEAM_MEMBERS
    }:
        return False
    if filters.get("project") and oracle_project_group(str((issue.get("project") or {}).get("name") or "")) != oracle_project_group(filters["project"]):
        return False
    if filters.get("created_month") and str(issue.get("created_at") or "")[:7] != filters["created_month"]:
        return False
    if filters.get("status") and status_name(issue) != filters["status"]:
        return False
    if filters.get("handler") and filters["handler"] not in {
        (issue.get("handler") or {}).get("real_name"), (issue.get("handler") or {}).get("name"),
    }:
        return False
    if filters.get("screen") and filters["screen"] not in exact_screen_numbers(issue):
        return False
    media = custom_field(issue, "매체구분")
    text = normalize(media)
    for key in ("platform", "excluded_platform"):
        platform = filters.get(key)
        if not platform:
            continue
        aliases = {"HTS": ("hts", "hable"), "MTS": ("mts", "엠티에스", "모바일", "mobile")}[platform]
        matched = any(value in text for value in aliases)
        if matched != (key == "platform"):
            return False
    return True


def complete_mutable_mantis_ids(
    filters: Dict[str, str],
    live_issues: List[Dict[str, Any]],
    limit: int,
) -> List[int]:
    """전체 원본과 최신 표본을 직접 필터링한 뒤 후보의 최신 필드를 재확인한다."""
    rows = read_complete_store()
    if rows is None:
        raise RuntimeError("독립 정답 계산에 필요한 최신 Mantis 전체 원본이 없습니다.")
    candidates = {int(issue["id"]): issue for issue in [*rows, *live_issues] if issue.get("id")}
    expected_ids = []
    for issue_id in sorted(candidates, reverse=True):
        if not mantis_oracle_matches(candidates[issue_id], filters):
            continue
        latest = mantis.fetch_mantis_issue_by_id(issue_id)
        if latest and mantis_oracle_matches(latest, filters):
            expected_ids.append(issue_id)
        if len(expected_ids) >= limit:
            break
    return expected_ids


def fetch_mantis_snapshot(progress: bool = False) -> List[Dict[str, Any]]:
    url = os.getenv("MANTIS_API_URL", "").strip()
    token = os.getenv("MANTIS_API_TOKEN", "").strip()
    if not url or not token:
        raise RuntimeError("MANTIS_API_URL/MANTIS_API_TOKEN is not configured")

    issues: List[Dict[str, Any]] = []
    headers = {"Authorization": token, "Content-Type": "application/json"}
    for page in range(1, MANTIS_PAGES + 1):
        for attempt in range(1, MANTIS_REQUEST_ATTEMPTS + 1):
            try:
                response = requests.get(
                    url,
                    headers=headers,
                    params={"page_size": MANTIS_PAGE_SIZE, "page": page},
                    timeout=MANTIS_REQUEST_TIMEOUT,
                )
                response.raise_for_status()
                batch = response.json().get("issues", [])
                break
            except requests.RequestException as exc:
                status_code = getattr(exc.response, "status_code", None)
                retryable = (
                    isinstance(exc, (requests.ConnectionError, requests.Timeout))
                    or status_code == 429
                    or bool(status_code and status_code >= 500)
                )
                if not retryable or attempt == MANTIS_REQUEST_ATTEMPTS:
                    raise
                time.sleep(2 ** (attempt - 1))
        if not batch:
            break
        issues.extend(batch)
        emit_progress(progress, {"type": "status", "message": f"Mantis 원문 {len(issues)}건 수집 완료"})
        if len(batch) < MANTIS_PAGE_SIZE:
            break
    return sorted(issues, key=lambda item: int(item.get("id") or 0), reverse=True)


def execute_chat(request: ChatRequest) -> Dict[str, Any]:
    """Run the service with the same audit owner throughout this accuracy run."""
    return chat_service.build_response(request, user={"uid": ACCURACY_USER_UID})


def notion_plain_text(items: Iterable[Dict[str, Any]]) -> str:
    return "".join(str(item.get("plain_text") or "") for item in items).strip()


def notion_property_text(prop: Dict[str, Any]) -> str:
    prop_type = prop.get("type")
    value = prop.get(prop_type) if prop_type else None
    if prop_type in {"title", "rich_text"}:
        return notion_plain_text(value or [])
    if prop_type in {"select", "status"}:
        return str((value or {}).get("name") or "")
    if prop_type == "multi_select":
        return ", ".join(str(item.get("name") or "") for item in value or [])
    if prop_type == "date":
        return str((value or {}).get("start") or "")
    if prop_type == "people":
        return ", ".join(str(item.get("name") or "") for item in value or [])
    if prop_type in {"number", "checkbox", "url", "email", "phone_number"}:
        return str(value if value is not None else "")
    return ""


def notion_page_summary(page: Dict[str, Any], database_label: str) -> Dict[str, str]:
    properties = page.get("properties") or {}
    title = ""
    status = ""
    text_parts = []
    for name, prop in properties.items():
        text = notion_property_text(prop)
        if text:
            text_parts.append(f"{name}: {text}")
        if prop.get("type") == "title":
            title = text
        if prop.get("type") in {"status", "select"} and any(
            keyword in normalize(name) for keyword in ("상태", "status", "진행")
        ):
            status = text
    return {
        "id": str(page.get("id") or ""),
        "title": title,
        "status": status,
        "database": database_label,
        "last_edited_time": str(page.get("last_edited_time") or ""),
        "search_text": " ".join(text_parts),
    }


def fetch_notion_snapshot() -> List[Dict[str, str]]:
    token = os.getenv("NOTION_API_TOKEN") or os.getenv("NOTION_TOKEN") or ""
    if not token:
        raise RuntimeError("NOTION_API_TOKEN is not configured")
    # 챗봇과 같은 범위를 정답 근거로 쓴다. 명시 목록이 없으면 연결된 DB 전체다.
    database_ids = parse_notion_database_ids()
    if not database_ids:
        database_ids, discovery_error, _ = discover_notion_database_ids(token)
        if discovery_error:
            raise RuntimeError(discovery_error)
    if not database_ids:
        raise RuntimeError("No Notion database is connected to the integration")

    from backend.notion.read_scope import ensure_notion_read_scope
    _read_scope, scope_complete = ensure_notion_read_scope(database_ids, token)
    if not scope_complete:
        raise RuntimeError("Notion child database scope could not be verified completely")

    headers = {
        "Authorization": f"Bearer {token}",
        "Notion-Version": os.getenv("NOTION_API_VERSION", "2022-06-28"),
        "Content-Type": "application/json",
    }
    pages: List[Dict[str, str]] = []
    for database_id in database_ids:
        metadata = requests.get(
            f"{NOTION_API_BASE}/databases/{database_id}",
            headers=headers,
            timeout=30,
        )
        metadata.raise_for_status()
        label = notion_plain_text(metadata.json().get("title") or []) or database_id[-8:]
        cursor: Optional[str] = None
        for _ in range(NOTION_PAGES):
            payload: Dict[str, Any] = {
                "page_size": NOTION_PAGE_SIZE,
                "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
            }
            if cursor:
                payload["start_cursor"] = cursor
            response = requests.post(
                f"{NOTION_API_BASE}/databases/{database_id}/query",
                headers=headers,
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            data = response.json()
            pages.extend(notion_page_summary(page, label) for page in data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
    return pages


def build_mantis_cases(
    issues: List[Dict[str, Any]],
    rng: random.Random,
    augment_mutable_oracle: bool = False,
) -> List[AccuracyCase]:
    if not issues:
        return []
    cases: List[AccuracyCase] = []
    candidates = issues[: min(100, len(issues))]
    exact = rng.choice(candidates)
    cases.append(AccuracyCase(
        "mantis-exact-id", "mantis", "정확 ID", f"{int(exact['id']):07d} 이슈 내용을 알려줘",
        "exact_issue_ids", [int(exact["id"])],
    ))

    team_issues = [item for item in issues if mantis_oracle_matches(item, {"scope": "team"})]
    projects_by_group = {oracle_project_group(str(item.get("project", {}).get("name") or "")):
                         str(item.get("project", {}).get("name") or "") for item in team_issues}
    project_counts = Counter(oracle_project_group(str(item.get("project", {}).get("name") or "")) for item in team_issues)
    projects = [name for name, count in project_counts.items() if name and count >= 3]
    rng.shuffle(projects)
    if not projects:
        return cases
    project = projects_by_group[projects[0]]
    project_filters = {"project": project, "scope": "team"}
    project_issues = [item for item in team_issues if mantis_oracle_matches(item, project_filters)]
    project_expected = [int(item["id"]) for item in project_issues[:3]]
    if augment_mutable_oracle:
        project_expected = complete_mutable_mantis_ids(project_filters, issues, 3)
    cases.append(AccuracyCase(
        "mantis-project-latest", "mantis", "고객사 최신 이슈",
        f"{human_project_name(project)} 최근 이슈 세 개만 보여줘", "ordered_issue_ids",
        project_expected, mutable_oracle=True, oracle_filters=project_filters,
    ))

    pairs = Counter((oracle_project_group(str(item.get("project", {}).get("name") or "")), status_name(item)) for item in team_issues)
    valid_pairs = [pair for pair, count in pairs.items() if pair[0] and pair[1] and count >= 2]
    pair_group, pair_status = rng.choice(valid_pairs) if valid_pairs else (oracle_project_group(project), status_name(project_issues[0]))
    pair_project = projects_by_group[pair_group]
    pair_issues = [
        item for item in team_issues
        if oracle_project_group(str(item.get("project", {}).get("name") or "")) == pair_group and status_name(item) == pair_status
    ]
    pair_question = f"{human_project_name(pair_project)}에서 {display_mantis_status_label(pair_status)} 최근 두 개만 보여줄래?"
    pair_filters = {"project": pair_project, "status": pair_status, "scope": "team"}
    pair_expected = [int(item["id"]) for item in pair_issues[:2]]
    if augment_mutable_oracle:
        pair_expected = complete_mutable_mantis_ids(pair_filters, issues, 2)
    cases.append(AccuracyCase(
        "mantis-project-status", "mantis", "고객사·상태",
        pair_question, "ordered_issue_ids", pair_expected, mutable_oracle=True, oracle_filters=pair_filters,
    ))

    project_platform_groups: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    for item in team_issues:
        item_project = oracle_project_group(str(item.get("project", {}).get("name") or "").strip())
        platform = normalize(custom_field(item, "매체구분"))
        if item_project and platform in {"hts", "mts"}:
            project_platform_groups.setdefault((item_project, platform.upper()), []).append(item)
    project_platform_pairs = [
        pair
        for pair, rows in project_platform_groups.items()
        if len(rows) >= 2
    ]
    if project_platform_pairs:
        platform_group, platform = rng.choice(sorted(project_platform_pairs))
        platform_project = projects_by_group[platform_group]
        platform_question = (
            f"{human_project_name(platform_project)} {platform} 이슈 최근 두 개만 최신순으로 알려줘"
        )
        platform_filters = {"project": platform_project, "platform": platform, "scope": "team"}
        platform_expected = [
            int(item["id"])
            for item in team_issues
            if mantis_oracle_matches(item, platform_filters)
        ][:2]
        if augment_mutable_oracle:
            platform_expected = (
                complete_mutable_mantis_ids(platform_filters, issues, 2)
            )
        if len(platform_expected) >= 2:
            cases.append(AccuracyCase(
                "mantis-project-platform",
                "mantis",
                "고객사·플랫폼·정렬",
                platform_question,
                "ordered_issue_ids",
                platform_expected,
                mutable_oracle=True,
                oracle_filters=platform_filters,
            ))

    exclusion_candidates: List[tuple[str, str, List[int]]] = []
    for candidate_group in sorted(project_counts):
        candidate_project = projects_by_group[candidate_group]
        if not candidate_project:
            continue
        for excluded_platform in ("HTS", "MTS"):
            exclusion_filters = {"project": candidate_project, "excluded_platform": excluded_platform, "scope": "team"}
            matching_ids = [
                int(item["id"])
                for item in team_issues
                if mantis_oracle_matches(item, exclusion_filters)
            ][:3]
            if len(matching_ids) >= 2:
                exclusion_candidates.append((candidate_project, excluded_platform, matching_ids))
    if exclusion_candidates:
        excluded_project, excluded_platform, exclusion_expected = rng.choice(exclusion_candidates)
        exclusion_question = (
            f"{human_project_name(excluded_project)} 이슈에서 {excluded_platform}는 빼고 "
            "최근 세 개만 보여줘"
        )
        exclusion_filters = {"project": excluded_project, "excluded_platform": excluded_platform, "scope": "team"}
        if augment_mutable_oracle:
            exclusion_expected = (
                complete_mutable_mantis_ids(exclusion_filters, issues, 3)
            )
        cases.append(AccuracyCase(
            "mantis-project-platform-exclusion",
            "mantis",
            "고객사·플랫폼 제외",
            exclusion_question,
            "ordered_issue_ids",
            exclusion_expected,
            mutable_oracle=True,
            oracle_filters=exclusion_filters,
        ))

    screen_groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in team_issues:
        for screen in exact_screen_numbers(item):
            screen_groups.setdefault(screen, []).append(item)
    if screen_groups:
        screen = rng.choice(sorted(screen_groups))
        screen_question = f"화면번호 {screen} 관련 이슈 최근 세 개만 찾아줘"
        screen_filters = {"screen": screen, "scope": "team"}
        screen_expected = sorted(
            (int(item["id"]) for item in screen_groups[screen]),
            reverse=True,
        )[:3]
        if augment_mutable_oracle:
            screen_expected = (
                complete_mutable_mantis_ids(screen_filters, issues, 3)
            )
        cases.append(AccuracyCase(
            "mantis-screen", "mantis", "화면번호", screen_question,
            "ordered_issue_ids", screen_expected, mutable_oracle=True, oracle_filters=screen_filters,
        ))

    handler_groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in issues:
        handler = str((item.get("handler") or {}).get("real_name") or "").strip()
        if handler and len(handler_groups.setdefault(handler, [])) < 10:
            handler_groups[handler].append(item)
    handlers = [name for name, rows in handler_groups.items() if len(rows) >= 2]
    if handlers:
        handler = rng.choice(sorted(handlers))
        handler_question = f"담당자 {handler}의 최근 이슈 2개 보여줘"
        handler_filters = {"handler": handler}
        handler_expected = [int(item["id"]) for item in handler_groups[handler][:2]]
        if augment_mutable_oracle:
            handler_expected = complete_mutable_mantis_ids(handler_filters, issues, 2)
        cases.append(AccuracyCase(
            "mantis-handler", "mantis", "담당자", handler_question,
            "ordered_issue_ids", handler_expected, mutable_oracle=True, oracle_filters=handler_filters,
        ))

    count_rows = read_complete_store() if augment_mutable_oracle else issues
    if count_rows is None:
        raise RuntimeError("팀 범위 월별 정답 계산에 필요한 전체 원본이 없습니다.")
    dated = [item for item in count_rows if mantis_oracle_matches(item, project_filters)
             and re.match(r"\d{4}-\d{2}", str(item.get("created_at") or ""))]
    month_counts = Counter(str(item.get("created_at"))[:7] for item in dated)
    months = [month for month, count in month_counts.items() if count > 0]
    if months:
        month = rng.choice(sorted(months)[-3:])
        year, month_number = month.split("-")
        cases.append(AccuracyCase(
            "mantis-month-count", "mantis", "월별 건수",
            f"{year}년 {int(month_number)}월에 {human_project_name(project)}에서 들어온 이슈가 몇 건이야?",
            "count", month_counts[month], oracle_filters={**project_filters, "created_month": month},
        ))

    used_screens = {
        screen
        for item in issues
        for screen in exact_screen_numbers(item)
    }
    absent_screen = next(
        screen
        for screen in (str(value) for value in range(99999, 89999, -1))
        if screen not in used_screens
    )
    cases.append(AccuracyCase(
        "mantis-screen-no-result",
        "mantis",
        "결과 없음·환각 방지",
        f"화면번호 {absent_screen} 관련 이슈 있는지 확인해줘",
        "no_results",
        [],
    ))
    return cases


def useful_title(page: Dict[str, str]) -> bool:
    return len(page.get("title", "").strip()) >= 4


def build_notion_cases(pages: List[Dict[str, str]], rng: random.Random) -> List[AccuracyCase]:
    candidates = [page for page in pages if useful_title(page)]
    rng.shuffle(candidates)
    selected: List[Dict[str, str]] = []
    seen = set()
    for page in candidates:
        key = normalize(page["title"])
        if key and key not in seen:
            seen.add(key)
            selected.append(page)
        if len(selected) == 4:
            break
    cases = [
        AccuracyCase(
            f"notion-title-{index + 1}", "notion", "제목 검색",
            f"노션에서 '{page['title']}' 문서 좀 찾아줘", "titles", [page["title"]],
        )
        for index, page in enumerate(selected)
    ]
    status_candidates = [page for page in selected if page.get("status", "").strip()]
    for index, page in enumerate(status_candidates[:2]):
        cases.append(AccuracyCase(
            f"notion-title-status-{index + 1}",
            "notion",
            "제목·상태 복합 조건",
            f"노션에서 상태가 {page['status']}인 '{page['title']}' 항목을 정확히 찾아줘",
            "titles",
            [page["title"]],
            expected_constraints={"exact_title": page["title"], "status": page["status"]},
        ))
        # 같은 제목에 상반된 상태를 요구해도 제목 일치만으로 성공할 수 없어야 한다.
        cases.append(AccuracyCase(
            f"notion-title-excluded-status-{index + 1}", "notion", "제목·상태 제외",
            f"노션에서 '{page['title']}' 항목을 찾아줘. {page['status']} 상태는 빼고",
            "titles" if any(
                other["title"] == page["title"] and other.get("status") != page["status"]
                for other in pages
            ) else "no_results",
            [page["title"]],
            expected_constraints={"exact_title": page["title"], "excluded_statuses": [page["status"]]},
        ))
    nonce = rng.randrange(100000, 1000000)
    cases.append(AccuracyCase(
        "notion-title-no-result",
        "notion",
        "결과 없음·환각 방지",
        f"노션에서 '자동 정확도 점검용 존재하지 않는 항목 {nonce}'을 찾아줘",
        "no_results",
        [],
    ))
    return cases


def select_rotating_cases(
    cases: List[AccuracyCase],
    limit: int,
    rng: random.Random,
) -> List[AccuracyCase]:
    """Keep conversation prerequisites and rotate the remaining daily coverage."""
    if limit <= 0:
        return []
    mantis_cases = [case for case in cases if case.source == "mantis"]
    notion_cases = [case for case in cases if case.source == "notion"]
    by_id = {case.id: case for case in cases}
    required_ids = [
        "mantis-exact-id",
        "mantis-project-latest",
        "mantis-project-status",
        "notion-title-1",
    ]
    selected = [by_id[case_id] for case_id in required_ids if case_id in by_id][:limit]
    selected_ids = {case.id for case in selected}

    mantis_target = min(len(mantis_cases), max(min(3, limit), round(limit * 0.6)))
    notion_target = min(len(notion_cases), max(0, limit - mantis_target))
    targets = {"mantis": mantis_target, "notion": notion_target}
    pools = {
        "mantis": [case for case in mantis_cases if case.id not in selected_ids],
        "notion": [case for case in notion_cases if case.id not in selected_ids],
    }
    for pool in pools.values():
        rng.shuffle(pool)
    for source in ("mantis", "notion"):
        current = sum(case.source == source for case in selected)
        take = max(0, targets[source] - current)
        selected.extend(pools[source][:take])
        selected_ids.update(case.id for case in pools[source][:take])

    remainder = [case for case in cases if case.id not in selected_ids]
    rng.shuffle(remainder)
    selected.extend(remainder[: max(0, limit - len(selected))])
    return selected[:limit]


def build_natural_language_cases(cases: List[AccuracyCase]) -> List[AccuracyCase]:
    """독립 정답을 유지한 채 팀원이 쓰는 구어체·한글 개수 표현도 검사한다."""
    variants = []
    for case in cases:
        question = ""
        if case.id == "mantis-exact-id":
            question = f"{int(case.expected[0]):07d}번 이슈 내용 좀 알려줄래?"
        elif case.id in {"mantis-project-latest", "mantis-project-status", "mantis-handler"}:
            question = case.question.replace("세 개", "3건").replace("두 개", "2건").replace("보여줘", "보여줄래?")
        elif case.id == "mantis-project-platform-exclusion":
            question = case.question.replace("는 빼고", "는 제외하고").replace("3개", "세 건").replace("보여줘", "보여주세요")
        elif case.id == "mantis-month-count":
            question = case.question.replace("몇 개야?", "총 몇 건인지 알려주세요")
        elif case.id == "notion-title-1":
            question = f"노션에서 '{case.expected[0]}' 문서 좀 찾아줄래?"
        elif case.id == "notion-title-status-1":
            filters = case.expected_constraints or {}
            question = f"노션에서 '{case.expected[0]}' 찾아주세요. 상태가 {filters['status']}인 것만요"
        if question:
            variants.append(replace(case, id=f"natural-{case.id}", question=question, category=f"자연어·{case.category}"))
    return variants


def build_follow_up_scenarios(
    cases: List[AccuracyCase],
    mantis_issues: List[Dict[str, Any]],
    limit: int = 4,
    rng: Optional[random.Random] = None,
) -> List[FollowUpScenario]:
    """Build deterministic conversation checks from already generated base cases.

    The setup turn is one of the ten base cases, so four follow-up scenarios add
    only four model calls. Expected values come from the live snapshot oracle,
    never from the model's first answer.
    """
    if limit <= 0:
        return []
    rng = rng or random.Random(0)
    by_id = {case.id: case for case in cases}
    issues_by_id = {
        int(issue["id"]): issue
        for issue in mantis_issues
        if issue.get("id")
    }
    scenarios: List[FollowUpScenario] = []

    exact_case = by_id.get("mantis-exact-id")
    if exact_case and exact_case.expected:
        issue = issues_by_id.get(int(exact_case.expected[0]))
        project = str((issue or {}).get("project", {}).get("name") or "").strip()
        if project:
            scenarios.append(FollowUpScenario(
                "followup-exact-project",
                "mantis",
                "후속 질문·대상 속성",
                exact_case.id,
                "이 이슈는 어느 고객사 건이야?",
                "text_any",
                customer_answer_aliases(project),
            ))

    project_case = by_id.get("mantis-project-latest")
    if project_case and len(project_case.expected) >= 2:
        scenarios.append(FollowUpScenario(
            "followup-project-second",
            "mantis",
            "후속 질문·순번 참조",
            project_case.id,
            "그중 두 번째 이슈를 자세히 알려줘",
            "exact_issue_ids",
            [int(project_case.expected[1])],
        ))

    status_case = by_id.get("mantis-project-status")
    status_scenarios: List[FollowUpScenario] = []
    if status_case and status_case.expected:
        status_scenarios.append(FollowUpScenario(
            "followup-project-status-latest",
            "mantis",
            "후속 질문·조건 상속",
            status_case.id,
            "방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘",
            "exact_issue_ids",
            [int(status_case.expected[0])],
        ))
        setup_issue = issues_by_id.get(int(status_case.expected[0]))
        setup_project = str((setup_issue or {}).get("project", {}).get("name") or (status_case.oracle_filters or {}).get("project") or "")
        setup_status = status_name(setup_issue or {})
        alternate_groups: Dict[str, List[Dict[str, Any]]] = {}
        for issue in mantis_issues:
            if not mantis_oracle_matches(issue, {"project": setup_project, "scope": "team"}):
                continue
            alternate_status = status_name(issue)
            if alternate_status and alternate_status != setup_status:
                alternate_groups.setdefault(alternate_status, []).append(issue)
        if alternate_groups:
            alternate_status = rng.choice(sorted(alternate_groups))
            status_scenarios.append(FollowUpScenario(
                "followup-project-status-correction",
                "mantis",
                "후속 질문·조건 교정",
                status_case.id,
                f"아니, 같은 고객사에서 {display_mantis_status_label(alternate_status)} 상태인 가장 최근 이슈 하나만 알려줘",
                "exact_issue_ids",
                [int(alternate_groups[alternate_status][0]["id"])],
            ))
    if status_scenarios:
        scenarios.append(rng.choice(status_scenarios))

    notion_case = next(
        (case for case in cases if case.source == "notion" and case.expected),
        None,
    )
    if notion_case:
        scenarios.append(FollowUpScenario(
            "followup-notion-title",
            "notion",
            "후속 질문·Notion 문맥",
            notion_case.id,
            "그 항목의 제목을 정확히 다시 알려줘",
            # 대화 문맥에서 이미 검증된 제목을 다시 말하는 후속 응답은 검색을
            # 반복하지 않아 sources가 비어 있을 수 있다. 본문의 정확한 제목을
            # 검증하는 text_any가 이 계약을 직접 표현한다.
            "text_any",
            list(notion_case.expected),
        ))

    return scenarios[:limit]


def observed_issue_ids(response: Dict[str, Any]) -> set[int]:
    # '이슈 #31397을 찾지 못했습니다'에 번호가 있어도 검색 성공이 아니다.
    return {
        int(source["issue_id"])
        for source in response.get("sources") or []
        if source.get("issue_id")
    }


def observed_ordered_issue_ids(response: Dict[str, Any]) -> List[int]:
    ordered: List[int] = []
    for source in response.get("sources") or []:
        issue_id = source.get("issue_id")
        if issue_id and int(issue_id) not in ordered:
            ordered.append(int(issue_id))
    return ordered


def extract_answer_count(content: str) -> Optional[int]:
    patterns = (
        r"(?:총|모두|결과(?:는)?|이슈(?:는)?)\s*[:：]?\s*(\d+)\s*건",
        r"(\d+)\s*건(?:입니다|이에요|이다|으로)",
    )
    for pattern in patterns:
        match = re.search(pattern, content)
        if match:
            return int(match.group(1))
    return None


def observed_route_source(response: Dict[str, Any]) -> Optional[str]:
    retrieval = response.get("retrieval") or {}
    plan = retrieval.get("intent_plan") or retrieval.get("query_plan") or {}
    source = str(plan.get("source") or "").lower()
    if source in {"mantis", "notion", "both", "general"}:
        return source
    stage_sources = {
        "notion" if str(stage.get("source") or "").startswith("notion") else "mantis"
        for stage in retrieval.get("stages") or []
        if str(stage.get("source") or "").startswith(("notion", "mantis", "chroma"))
    }
    if stage_sources == {"notion", "mantis"}:
        return "both"
    return next(iter(stage_sources), None)


_DEPENDENCY_REASON_MARKERS = (
    "timeout",
    "timed out",
    "readtimeout",
    "connection failed",
    "connectionerror",
    "httpconnectionpool",
    "httpsconnectionpool",
    "rate limit",
    "too many requests",
    "service unavailable",
    "연결 실패",
    "시간 초과",
)


def dependency_reason_from_stage(stage: Dict[str, Any]) -> str:
    """Return a reason only for an external dependency failure.

    New stages should set ``error_kind=dependency``. The conservative text
    fallback keeps old reports useful without treating validation, no-result,
    or internal product failures as unavailable dependencies.
    """
    if stage.get("status") not in {"error", "partial_error"}:
        return ""
    reason = str(stage.get("reason") or "").strip()
    error_kind = str(stage.get("error_kind") or "").strip().casefold()
    if error_kind:
        return reason or "external dependency failure" if error_kind == "dependency" else ""
    normalized = reason.casefold()
    if any(marker in normalized for marker in _DEPENDENCY_REASON_MARKERS):
        return reason
    if re.search(r"(?:http(?: status)?|api failed:?)\s*5\d\d\b", normalized):
        return reason
    if re.search(r"\b429\b", normalized):
        return reason
    return ""


def filter_recall_matches(
    question: str,
    actual: Dict[str, Any],
    expected_constraints: Optional[Dict[str, Any]] = None,
) -> bool:
    expected = expected_constraints if expected_constraints is not None else extract_query_constraints(question)
    def customer_identity(value):
        return customer_alias_owner(str(value)) or normalize(value)
    if expected.get("customer_key") and customer_identity(expected["customer_key"]) != customer_identity(actual.get("customer_key")):
        return False
    if expected.get("platform_key") and expected["platform_key"] != actual.get("platform_key"):
        return False
    list_keys = (
        "statuses",
        "issue_ids",
        "screen_numbers",
        "excluded_statuses",
        "excluded_platform_keys",
        "excluded_customer_keys",
    )
    for key in list_keys:
        actual_values = set(actual.get(key) or [])
        if key == "excluded_platform_keys":
            for clause in actual.get("filter_clauses") or []:
                if clause.get("field") == "platform" and clause.get("operator") in {"not_equals", "not_in"}:
                    values = clause.get("value")
                    actual_values.update(str(value).casefold() for value in (values if isinstance(values, list) else [values]))
        expected_values = set(expected.get(key) or [])
        if key in {"statuses", "excluded_statuses"}:
            expected_values = {canonical_mantis_status(value) or value for value in expected_values}
            actual_values = {canonical_mantis_status(value) or value for value in actual_values}
        if not expected_values.issubset(actual_values):
            return False
    expected_people = set((expected.get("people") or {}).get("names") or [])
    actual_people = set((actual.get("people") or {}).get("names") or [])
    if not expected_people.issubset(actual_people):
        return False
    expected_range = expected.get("created_at") or {}
    actual_range = actual.get("created_at") or {}
    if expected.get("mantis_team_members") and set(expected["mantis_team_members"]) != set(actual.get("mantis_team_members") or []):
        return False
    return not expected_range or all(expected_range.get(key) == actual_range.get(key) for key in ("from", "to"))


def grade(case: AccuracyCase, response: Dict[str, Any]) -> Dict[str, Any]:
    content = str(response.get("content") or "")
    sources = response.get("sources") or []
    if case.expected_kind == "issue_ids":
        expected = {int(value) for value in case.expected}
        observed = observed_issue_ids(response)
        passed = expected.issubset(observed)
        actual: Any = sorted(observed)
    elif case.expected_kind == "exact_issue_ids":
        expected = {int(value) for value in case.expected}
        observed = observed_issue_ids(response)
        passed = expected == observed
        actual = sorted(observed)
    elif case.expected_kind == "ordered_issue_ids":
        expected_order = [int(value) for value in case.expected]
        actual = observed_ordered_issue_ids(response)
        passed = expected_order == actual
    elif case.expected_kind == "titles":
        expected_titles = [str(value) for value in case.expected]
        source_titles = {normalize(source.get("title")) for source in sources}
        missing = [title for title in expected_titles
                   if normalize(title) not in source_titles or normalize(title) not in normalize(content)]
        passed = not missing
        actual = {"matched": len(expected_titles) - len(missing), "missing": missing}
    elif case.expected_kind == "text_any":
        haystack = normalize(content)
        alternatives = [str(value) for value in case.expected]
        matched = next(
            (value for value in alternatives if normalize(value) in haystack),
            None,
        )
        passed = matched is not None
        actual = {"matched": matched}
    elif case.expected_kind == "count":
        actual = extract_answer_count(content)
        passed = actual == int(case.expected)
    elif case.expected_kind == "no_results":
        actual_ids = sorted(
            int(source["issue_id"])
            for source in sources
            if source.get("issue_id")
        )
        actual = {"source_issue_ids": actual_ids, "source_count": len(sources)}
        # '관계없는 상태', '접근 권한이 없습니다'는 빈 검색 결과가 아니다.
        empty_answer = bool(re.search(
            r"(?:이슈|항목|문서|결과|티켓)(?:가|이|는|을|를)?"
            r"(?:지금조건으로는)?"
            r"(?:없(?:습니다|어요|다|음)|찾지못(?:했|하))|(?:^|[^0-9])0(?:건|개)(?![0-9])",
            normalize(content),
        ))
        passed = not actual_ids and not sources and empty_answer
    else:
        actual = None
        passed = False
    dependency_reason = ""
    retrieval = response.get("retrieval") or {}
    stages = [
        *(response.get("retrievalStages") or []),
        *(retrieval.get("stages") or []),
    ]
    for stage in stages:
        dependency_reason = dependency_reason_from_stage(stage)
        if dependency_reason:
            break
    if not dependency_reason and any(
        token in content.casefold()
        for token in _DEPENDENCY_REASON_MARKERS
    ):
        dependency_reason = content[:500]

    observed_route = observed_route_source(response)
    routing_passed = observed_route == case.source if observed_route else None
    actual_constraints = retrieval.get("constraints") or {}
    status_resolution = actual_constraints.get("status_resolution") or {}
    response_mode = str((response.get("raw") or {}).get("mode") or "")
    clarification = "clarification" in response_mode or bool(status_resolution.get("needs_clarification"))
    if response_mode == "empty_evidence":
        passed = False
    if clarification:
        passed = False
        if status_resolution.get("origin") == "unavailable" and status_resolution.get("reason") == "planner_unavailable":
            dependency_reason = dependency_reason or "status planner unavailable"
    grounding_text = str(retrieval.get("query") or case.question)
    ungrounded = (
        ungrounded_constraint_labels(grounding_text, actual_constraints, case.source,
            query_plan=retrieval.get("query_plan") or retrieval.get("intent_plan"))
        if actual_constraints
        else []
    )
    filter_precision_passed = not ungrounded if actual_constraints else None
    filter_recall_passed = (
        filter_recall_matches(case.question, actual_constraints, case.expected_constraints)
        if actual_constraints or case.expected_constraints is not None
        else None
    )
    if case.source == "mantis" and (case.oracle_filters or {}).get("scope") == "team":
        filter_recall_passed = filter_recall_passed is not False and (
            actual_constraints.get("mantis_team_scope") in {"team", "default"}
            and set(actual_constraints.get("mantis_team_members") or []) == set(TEAM_MEMBERS)
        )
    source_constraints_passed = None
    if case.source == "notion":
        # Notion 제목 속 숫자·고객사명을 Mantis 필터로 해석하지 않는다.
        expected_notion = case.expected_constraints or extract_notion_constraints(grounding_text)
        observed_notion = next((
            stage.get("meta", {}).get("constraints", {})
            for stage in stages if stage.get("source") == "notion"
        ), {})
        scalar_keys = ("exact_title", "status", "owner", "database_label")
        filter_recall_passed = all(
            not expected_notion.get(key)
            or normalize(expected_notion[key]) == normalize(observed_notion.get(key))
            for key in scalar_keys
        ) and all(
            normalize(value) in {normalize(item) for item in observed_notion.get("excluded_statuses", [])}
            for value in expected_notion.get("excluded_statuses", [])
        ) and (not expected_notion.get("unassigned_owner") or bool(observed_notion.get("unassigned_owner")))
        filter_precision_passed = all(
            not observed_notion.get(key)
            or normalize(observed_notion[key]) == normalize(expected_notion.get(key))
            for key in scalar_keys
        )
        # 실제 원문 속성을 독립적으로 확인한다. 계획끼리 일치해도 오답이면 실패다.
        source_constraints_passed = all(
            source.get("type") == "notion"
            and all(not expected_notion.get(key)
                    or normalize(source.get(field)) == normalize(expected_notion[key])
                    for key, field in (("exact_title", "title"), ("status", "status"), ("owner", "owner")))
            and normalize(source.get("status")) not in {
                normalize(value) for value in expected_notion.get("excluded_statuses", [])
            }
            and (not expected_notion.get("unassigned_owner")
                 or normalize(source.get("owner")) in {"", "미지정", "담당자미지정"})
            for source in sources
        )
    answer_passed = passed
    incomplete_search = any(stage.get("meta", {}).get("search_complete") is False for stage in stages)
    internal_error = any(stage.get("error_kind") == "internal" for stage in stages)
    # A literal-title lookup has already satisfied its contract when the exact
    # title and requested properties are present. An unrelated database that
    # could not be scanned is still useful coverage information, but it cannot
    # turn the verified item into a wrong answer. Counts, empty-result claims,
    # and ordered lists still require complete coverage.
    completeness_required = not (
        case.source == "notion"
        and case.expected_kind == "titles"
        and answer_passed
    )
    passed = passed and not any(value is False for value in (
        routing_passed, filter_precision_passed, filter_recall_passed, source_constraints_passed,
    )) and (not incomplete_search or not completeness_required) and not internal_error
    failed_checks = [label for label, value in (
        ("답변 내용", answer_passed), ("검색 소스", routing_passed),
        ("추가 조건", filter_precision_passed), ("조건 누락", filter_recall_passed),
        ("원문 속성", source_constraints_passed),
        ("조회 완전성", not incomplete_search if completeness_required else None),
        ("실행 오류", not internal_error),
    ) if value is False]
    # 외부 소스가 불완전한 상태에서 우연히 기대값이 포함됐더라도 완전한 성공으로
    # 집계하면 다음 실행에서 같은 응답을 재현할 수 없다. 의존성 오류를 우선한다.
    outcome = "dependency_error" if dependency_reason else "passed" if passed else "assertion_failed"
    return {
        **asdict(case),
        "passed": passed and not dependency_reason,
        "outcome": outcome,
        "actual": actual,
        "answer_passed": answer_passed,
        "source_constraints_passed": source_constraints_passed,
        "incomplete_search": incomplete_search,
        "coverage_warning": incomplete_search and not completeness_required,
        "failed_checks": failed_checks,
        "observed_constraints": observed_notion if case.source == "notion" else actual_constraints,
        "expected_route": case.source,
        "observed_route": observed_route,
        "routing_passed": routing_passed,
        "filter_precision_passed": filter_precision_passed,
        "filter_recall_passed": filter_recall_passed,
        "ungrounded_filters": ungrounded,
        "answer": content[:2000],
        "source_count": len(sources),
        "audit_id": response.get("auditId"),
        **({"dependency_error": dependency_reason} if dependency_reason else {}),
    }


def reconcile_mutable_mantis_oracle(
    case: AccuracyCase,
    response: Dict[str, Any],
    result: Dict[str, Any],
) -> Dict[str, Any]:
    """실패한 최신 목록 케이스의 기대값과 실제값을 같은 시점에 재검증한다."""
    if (
        case.source != "mantis"
        or not case.mutable_oracle
        or not case.oracle_filters
        or result.get("outcome") != "assertion_failed"
        or case.expected_kind not in {"issue_ids", "exact_issue_ids", "ordered_issue_ids"}
        or not isinstance(case.expected, list)
        or not case.expected
        or result.get("routing_passed") is False
        or result.get("filter_precision_passed") is False
        or result.get("filter_recall_passed") is False
        or result.get("incomplete_search")
        or any(label != "답변 내용" for label in result.get("failed_checks", []))
    ):
        return result

    actual_order = observed_ordered_issue_ids(response)
    expected_ids = [int(value) for value in case.expected]
    limit = extract_requested_limit(case.question) or len(expected_ids)

    # 생성 시 원본 필드로 고정한 조건을 사용한다. 질문 재해석이나 검색기의
    # 매처/후보 ID를 정답에 사용하면 같은 검색 버그를 정답으로 인정하게 된다.
    try:
        live_issues = fetch_mantis_snapshot()
        refreshed_expected = complete_mutable_mantis_ids(case.oracle_filters, live_issues, limit)
    except Exception as exc:
        return {
            **result,
            "oracle_reconciliation_error": f"{type(exc).__name__}: {str(exc)[:500]}",
        }
    if refreshed_expected == expected_ids:
        # The old oracle is still current, so this is a real answer failure.
        return result
    if case.expected_kind == "ordered_issue_ids":
        reconciled = actual_order == refreshed_expected
    else:
        reconciled = set(actual_order) == set(refreshed_expected)
    if not reconciled:
        return result

    return {
        **result,
        "passed": True,
        "outcome": "passed",
        "answer_passed": True,
        "failed_checks": [label for label in result.get("failed_checks", []) if label != "답변 내용"],
        "original_expected": case.expected,
        "expected": refreshed_expected,
        "oracle_reconciled": True,
        "oracle_reconciled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def run_case(case: AccuracyCase, model_id: str) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        tools = {
            "mantis": True,
            "notion": True,
            "vectorDb": False,
        }
        response = execute_chat(ChatRequest(
            modelId=model_id,
            messages=[ChatMessage(role="user", content=case.question)],
            temperature=0.0,
            seed=42,
            maxTokens=900,
            searchLimit=5,
            showSources=True,
            # 채점 대상은 본 답변이다. 미채점 비교안을 생성하면 모델 장애 시
            # 매 건 60초씩 더 대기하고 본 답변의 지연 측정도 왜곡된다.
            abTest=False,
            stream=False,
            tools=tools,
        ))
        result = grade(case, response)
        result = reconcile_mutable_mantis_oracle(case, response, result)
        return {**result, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
        dependency_error = isinstance(exc, requests.RequestException) or any(
            token in error.casefold() for token in ("timeout", "timed out", "connection")
        )
        return {
            **asdict(case),
            "passed": False,
            "outcome": "dependency_error" if dependency_error else "execution_error",
            "error": error,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }


def blocked_follow_up_result(
    scenario: FollowUpScenario,
    parent_case: AccuracyCase,
    parent_result: Dict[str, Any],
) -> Dict[str, Any]:
    return {
        **asdict(scenario),
        "case_group": "follow_up",
        "setup_question": parent_case.question,
        "setup_outcome": parent_result.get("outcome"),
        "setup_audit_id": parent_result.get("audit_id"),
        "passed": False,
        "outcome": "blocked_by_prerequisite",
        "actual": None,
        "error": "1차 질문이 통과하지 않아 후속 질문을 실행하지 않았습니다.",
    }


def expected_follow_up_constraints(
    scenario: FollowUpScenario,
    parent_case: AccuracyCase,
) -> Optional[Dict[str, Any]]:
    """상속/교정의 기대 조건은 반환된 질의나 실행 필터와 독립적으로 만든다."""
    if scenario.source == "notion":
        return deepcopy(parent_case.expected_constraints or extract_notion_constraints(parent_case.question))
    if scenario.id == "followup-project-second":
        # 선택 ID는 선행 목록의 정답에서 고정한다. 기본 팀 범위의 새 목록
        # 조회가 아니라 선택한 단건 조회이며, 실제 실행 ID를 정답으로 쓰지 않는다.
        return {"issue_ids": list(scenario.expected)}
    if scenario.id == "followup-exact-project":
        return {"issue_ids": list(parent_case.expected)}
    if scenario.source != "mantis" or scenario.id not in {
        "followup-project-status-latest", "followup-project-status-correction",
    }:
        # 순번/단건 참조는 선택한 ID로 직접 조회하므로 목록의 필터를 강제하지 않는다.
        return None
    expected = deepcopy(parent_case.expected_constraints if parent_case.expected_constraints is not None
                        else extract_query_constraints(parent_case.question))
    if (parent_case.oracle_filters or {}).get("scope") == "team":
        expected["mantis_team_members"] = list(TEAM_MEMBERS)
    if scenario.id == "followup-project-status-correction":
        corrected = extract_query_constraints(scenario.question)
        for key in ("statuses", "excluded_statuses"):
            expected[key] = corrected.get(key) or []
        expected["filter_clauses"] = [
            clause for clause in expected.get("filter_clauses") or [] if clause["field"] != "status"
        ] + [clause for clause in corrected.get("filter_clauses") or [] if clause["field"] == "status"]
        expected.pop("status_resolution", None)
    return expected


def run_follow_up_scenario(
    scenario: FollowUpScenario,
    parent_case: AccuracyCase,
    parent_result: Dict[str, Any],
    model_id: str,
) -> Dict[str, Any]:
    if not parent_result.get("passed") or not parent_result.get("audit_id"):
        return blocked_follow_up_result(scenario, parent_case, parent_result)

    case = AccuracyCase(
        scenario.id,
        scenario.source,
        scenario.category,
        scenario.question,
        scenario.expected_kind,
        scenario.expected,
        expected_constraints=expected_follow_up_constraints(scenario, parent_case),
    )
    started = time.perf_counter()
    try:
        tools = {
            "mantis": True,
            "notion": True,
            "vectorDb": False,
        }
        response = execute_chat(ChatRequest(
            modelId=model_id,
            messages=[
                ChatMessage(role="user", content=parent_case.question),
                ChatMessage(role="assistant", content=str(parent_result.get("answer") or "")),
                ChatMessage(role="user", content=scenario.question),
            ],
            contextAuditId=str(parent_result["audit_id"]),
            temperature=0.0,
            seed=42,
            maxTokens=900,
            searchLimit=5,
            showSources=True,
            abTest=False,
            stream=False,
            tools=tools,
        ))
        result = {**grade(case, response), "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
        dependency_error = isinstance(exc, requests.RequestException) or any(
            token in error.casefold() for token in ("timeout", "timed out", "connection")
        )
        result = {
            **asdict(case),
            "passed": False,
            "outcome": "dependency_error" if dependency_error else "execution_error",
            "error": error,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }
    return {
        **result,
        "case_group": "follow_up",
        "parent_case_id": scenario.parent_case_id,
        "setup_question": parent_case.question,
        "setup_outcome": parent_result.get("outcome"),
        "setup_audit_id": parent_result.get("audit_id"),
    }


def summarize_results(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    passed = sum(1 for result in results if result.get("passed"))
    dependency_errors = sum(1 for result in results if result.get("outcome") == "dependency_error")
    execution_errors = sum(1 for result in results if result.get("outcome") == "execution_error")
    blocked = sum(1 for result in results if result.get("outcome") == "blocked_by_prerequisite")
    attempted = len(results)
    evaluated = attempted - dependency_errors - execution_errors
    failed = sum(
        1
        for result in results
        if result.get("outcome") in {"assertion_failed", "blocked_by_prerequisite"}
    )
    return {
        "attempted": attempted,
        "total": evaluated,
        "evaluated": evaluated,
        "passed": passed,
        "failed": failed,
        "blocked_by_prerequisite": blocked,
        "dependency_errors": dependency_errors,
        "execution_errors": execution_errors,
        "score": round(passed / evaluated * 100) if evaluated else None,
    }


def summarize_boolean_dimension(results: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    evaluated = [result.get(key) for result in results if result.get(key) is not None]
    passed = sum(value is True for value in evaluated)
    return {
        "total": len(evaluated),
        "passed": passed,
        "score": round(passed / len(evaluated) * 100) if evaluated else None,
    }


def summarize_external_reliability(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    attempted = len(results)
    failed = sum(
        result.get("outcome") in {"dependency_error", "execution_error"}
        for result in results
    )
    passed = attempted - failed
    return {
        "total": attempted,
        "passed": passed,
        "score": round(passed / attempted * 100) if attempted else None,
    }


def summarize_latency(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    values = sorted(float(result["latency_ms"]) for result in results if result.get("latency_ms") is not None)
    if not values:
        return {"p50_ms": None, "p95_ms": None}
    p95_index = min(len(values) - 1, max(0, round((len(values) - 1) * 0.95)))
    return {
        "p50_ms": round(statistics.median(values), 1),
        "p95_ms": round(values[p95_index], 1),
    }


def write_report(report: Dict[str, Any]) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    json_path = REPORT_DIR / f"accuracy-{stamp}.json"
    markdown_path = REPORT_DIR / f"accuracy-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = report["summary"]
    base_summary = summary.get("base", {})
    follow_up_summary = summary.get("follow_up", {})
    score_label = f"{summary['score']}%" if summary.get("score") is not None else "N/A"
    base_score_label = f"{base_summary.get('score')}%" if base_summary.get("score") is not None else "N/A"
    follow_up_score_label = (
        f"{follow_up_summary.get('score')}%"
        if follow_up_summary.get("score") is not None
        else "N/A"
    )
    routing = summary.get("routing", {})
    filter_precision = summary.get("filter_precision", {})
    filter_recall = summary.get("filter_recall", {})
    answer_accuracy = summary.get("answer_accuracy", {})
    source_properties = summary.get("source_properties", {})
    coverage = summary.get("coverage", {})
    external_reliability = summary.get("external_reliability", {})
    latency = summary.get("latency", {})
    routing_label = f"{routing.get('score')}%" if routing.get("score") is not None else "N/A"
    filter_precision_label = (
        f"{filter_precision.get('score')}%" if filter_precision.get("score") is not None else "N/A"
    )
    filter_recall_label = (
        f"{filter_recall.get('score')}%" if filter_recall.get("score") is not None else "N/A"
    )
    lines = [
        "# Dynamic accuracy report",
        "",
        f"- Run: {report['run_at']}",
        f"- Status: {summary.get('status', 'unknown')}",
        f"- Accuracy: {score_label} ({summary['passed']}/{summary['total']} evaluated)",
        f"- Base questions: {base_score_label} ({base_summary.get('passed', 0)}/{base_summary.get('total', 0)})",
        f"- Follow-up scenarios: {follow_up_score_label} ({follow_up_summary.get('passed', 0)}/{follow_up_summary.get('total', 0)})",
        f"- Source routing: {routing_label} ({routing.get('passed', 0)}/{routing.get('total', 0)})",
        f"- Filter precision: {filter_precision_label} ({filter_precision.get('passed', 0)}/{filter_precision.get('total', 0)})",
        f"- Filter recall: {filter_recall_label} ({filter_recall.get('passed', 0)}/{filter_recall.get('total', 0)})",
        f"- Answer content: {answer_accuracy.get('score')}% ({answer_accuracy.get('passed', 0)}/{answer_accuracy.get('total', 0)})",
        f"- Source properties: {source_properties.get('score')}% ({source_properties.get('passed', 0)}/{source_properties.get('total', 0)})",
        f"- Search coverage: {coverage.get('score')}% ({coverage.get('passed', 0)}/{coverage.get('total', 0)})",
        f"- External API reliability: {external_reliability.get('score')}% ({external_reliability.get('passed', 0)}/{external_reliability.get('total', 0)})",
        f"- Latency: p50 {latency.get('p50_ms')} ms / p95 {latency.get('p95_ms')} ms",
        f"- Attempted: {summary.get('attempted', summary['total'])}",
        f"- Blocked follow-ups: {summary.get('blocked_by_prerequisite', 0)}",
        f"- Dependency errors: {summary.get('dependency_errors', 0)}",
        f"- Mantis snapshot: {report['snapshots']['mantis']} items",
        f"- Notion snapshot: {report['snapshots']['notion']} items",
        "- 검증 범위: Notion은 표본 제목·속성 검사이며, 본문 검색 정확도와 전체 문서 재현율은 이 점수에 포함하지 않습니다.",
        "",
        "| Result | Group | Source | Category | Question | Expected | Actual |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in report["results"]:
        mark = {
            "passed": "PASS",
            "dependency_error": "ERROR",
            "execution_error": "ERROR",
        }.get(result.get("outcome"), "FAIL")
        expected = json.dumps(result.get("expected"), ensure_ascii=False)
        actual_value = (
            result.get("dependency_error")
            or result.get("error")
            or result.get("actual", "")
        )
        actual = json.dumps(actual_value, ensure_ascii=False)
        question = result["question"]
        if result.get("setup_question"):
            question = f"1차: {result['setup_question']} → 후속: {question}"
        lines.append(
            f"| {mark} | {result.get('case_group', 'base')} | {result['source']} | {result['category']} | "
            f"{question.replace('|', '/')} | {expected} | {actual} |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def emit_progress(enabled: bool, event: Dict[str, Any]) -> None:
    """웹 검증 센터가 서브프로세스 stdout을 파싱해 진행 상황을 중계할 수 있도록
    `PROGRESS {json}` 한 줄 형식으로 출력한다."""
    if enabled:
        print(f"PROGRESS {json.dumps(event, ensure_ascii=False)}", flush=True)


def prepare_mantis_store() -> Dict[str, Any]:
    """서버 수명주기 없이 실행하는 점검에도 최신 전체 검색 원본을 준비한다."""
    metadata = read_store_metadata()
    if metadata is None:
        deadline = time.monotonic() + 900
        result = sync_complete_store()
        # 서버 스케줄러가 이미 수집 중이면 같은 갱신의 완료를 기다린다.
        if result.get("status") == "running":
            while refresh_in_progress() and time.monotonic() < deadline:
                metadata = read_store_metadata()
                if metadata is not None:
                    return metadata
                time.sleep(1)
        metadata = read_store_metadata()
    if metadata is None:
        raise RuntimeError("Mantis 전체 저장본 갱신이 완료되지 않았습니다.")
    return metadata


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=int, default=10, choices=range(1, 11))
    parser.add_argument("--follow-up-scenarios", type=int, default=4, choices=range(0, 5))
    parser.add_argument("--seed", default=datetime.now().astimezone().strftime("%Y-%m-%d"))
    parser.add_argument("--model-id", default="")
    parser.add_argument("--progress", action="store_true", help="print PROGRESS json lines while running")
    parser.add_argument("--all-cases", action="store_true", help="run every generated question, without daily sampling")
    args = parser.parse_args()

    rng = random.Random(args.seed)
    # Page blocks add many requests and are not needed for title/metadata checks.
    os.environ["NOTION_FETCH_PAGE_BLOCKS"] = "false"
    errors: List[str] = []
    store_metadata: Dict[str, Any] = {}
    emit_progress(args.progress, {"type": "status", "message": "Mantis 전체 검색 원본의 최신 상태를 확인하고 있습니다"})
    try:
        store_metadata = prepare_mantis_store()
    except Exception as exc:
        errors.append(f"Mantis complete store preparation failed: {type(exc).__name__}")
    emit_progress(args.progress, {"type": "status", "message": "Mantis·Notion 실시간 스냅샷을 수집하고 있습니다"})
    try:
        mantis_issues = fetch_mantis_snapshot(progress=args.progress)
    except Exception as exc:
        mantis_issues = []
        errors.append(f"Mantis snapshot failed: {type(exc).__name__}: {str(exc)[:300]}")
    try:
        notion_pages = fetch_notion_snapshot()
    except Exception as exc:
        notion_pages = []
        errors.append(f"Notion snapshot failed: {type(exc).__name__}: {str(exc)[:300]}")

    try:
        mantis_cases = build_mantis_cases(mantis_issues, rng, augment_mutable_oracle=True)
    except Exception as exc:
        mantis_cases = []
        errors.append(f"Mantis oracle preparation failed: {type(exc).__name__}: {str(exc)[:300]}")
    candidate_cases = mantis_cases + build_notion_cases(notion_pages, rng)
    if args.all_cases:
        candidate_cases += build_natural_language_cases(candidate_cases)
    cases = candidate_cases if args.all_cases else select_rotating_cases(candidate_cases, args.questions, rng)
    requested_questions = len(candidate_cases) if args.all_cases else args.questions
    follow_up_scenarios = build_follow_up_scenarios(
        cases,
        mantis_issues,
        args.follow_up_scenarios,
        rng,
    )
    models = [model for model in get_model_registry() if model.get("enabled", True)]
    model_id = args.model_id or (str(models[0]["id"]) if models else "")
    if not model_id:
        errors.append("No enabled model is configured")

    planned_total = len(cases) + len(follow_up_scenarios)
    emit_progress(args.progress, {"type": "run_start", "total": planned_total if model_id else 0})
    results: List[Dict[str, Any]] = []
    base_results_by_id: Dict[str, Dict[str, Any]] = {}
    if model_id:
        for index, case in enumerate(cases, start=1):
            emit_progress(args.progress, {
                "type": "case_start",
                "caseId": case.id,
                "index": index,
                "total": planned_total,
                "question": case.question,
                "source": case.source,
                "category": case.category,
                "caseGroup": "base",
            })
            result = {**run_case(case, model_id), "case_group": "base"}
            results.append(result)
            base_results_by_id[case.id] = result
            emit_progress(args.progress, {"type": "case_result", "result": result})

        cases_by_id = {case.id: case for case in cases}
        for offset, scenario in enumerate(follow_up_scenarios, start=1):
            parent_case = cases_by_id[scenario.parent_case_id]
            parent_result = base_results_by_id[scenario.parent_case_id]
            emit_progress(args.progress, {
                "type": "case_start",
                "caseId": scenario.id,
                "index": len(cases) + offset,
                "total": planned_total,
                "question": scenario.question,
                "setupQuestion": parent_case.question,
                "source": scenario.source,
                "category": scenario.category,
                "caseGroup": "follow_up",
            })
            result = run_follow_up_scenario(
                scenario,
                parent_case,
                parent_result,
                model_id,
            )
            results.append(result)
            emit_progress(args.progress, {"type": "case_result", "result": result})

    base_results = [result for result in results if result.get("case_group") == "base"]
    follow_up_results = [result for result in results if result.get("case_group") == "follow_up"]
    totals = summarize_results(results)
    base_summary = summarize_results(base_results)
    follow_up_summary = summarize_results(follow_up_results)
    source_summaries = {
        source: summarize_results([result for result in results if result.get("source") == source])
        for source in ("mantis", "notion")
    }
    routing_summary = summarize_boolean_dimension(results, "routing_passed")
    filter_precision_summary = summarize_boolean_dimension(results, "filter_precision_passed")
    filter_recall_summary = summarize_boolean_dimension(results, "filter_recall_passed")
    answer_accuracy_summary = summarize_boolean_dimension(results, "answer_passed")
    source_properties_summary = summarize_boolean_dimension(results, "source_constraints_passed")
    coverage_summary = {
        "total": len(results),
        "passed": sum(not result.get("incomplete_search") for result in results),
    }
    coverage_summary["score"] = round(
        coverage_summary["passed"] / coverage_summary["total"] * 100
    ) if coverage_summary["total"] else None
    external_reliability_summary = summarize_external_reliability(results)
    latency_summary = summarize_latency(results)
    incomplete = (
        len(cases) < requested_questions
        or len(follow_up_scenarios) < args.follow_up_scenarios
        or totals["dependency_errors"] > 0
        or totals["execution_errors"] > 0
        or bool(errors)
    )
    run_status = "incomplete" if incomplete else "failed" if totals["failed"] else "passed"
    report = {
        "ab_test": False,
        "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seed": args.seed,
        "model_id": model_id,
        "requested_questions": requested_questions,
        "generated_questions": len(cases),
        "requested_follow_up_scenarios": args.follow_up_scenarios,
        "generated_follow_up_scenarios": len(follow_up_scenarios),
        "model_requests": len(base_results) + sum(
            1 for result in follow_up_results if result.get("outcome") != "blocked_by_prerequisite"
        ),
        "snapshots": {"mantis": len(mantis_issues), "notion": len(notion_pages)},
        "evaluation_scope": {
            "mantis_oracle": "raw_source_fields_with_live_candidate_revalidation",
            "notion_oracle": "sampled_titles_and_properties",
            "notion_pages_per_database": NOTION_PAGES,
            "notion_body_accuracy_evaluated": False,
        },
        "mantis_store": store_metadata,
        "summary": {
            **totals,
            "status": run_status,
            "base": base_summary,
            "follow_up": follow_up_summary,
            "by_source": source_summaries,
            "routing": routing_summary,
            "filter_precision": filter_precision_summary,
            "filter_recall": filter_recall_summary,
            "answer_accuracy": answer_accuracy_summary,
            "source_properties": source_properties_summary,
            "coverage": coverage_summary,
            "external_reliability": external_reliability_summary,
            "latency": latency_summary,
        },
        "errors": errors,
        "results": results,
    }
    json_path, markdown_path = write_report(report)
    emit_progress(args.progress, {
        "type": "summary",
        "summary": report["summary"],
        "errors": errors,
        "reportJson": json_path.name,
        "reportMarkdown": markdown_path.name,
    })
    score_label = f"{report['summary']['score']}%" if report["summary"]["score"] is not None else "N/A"
    print(
        f"Accuracy score: {score_label} ({totals['passed']}/{totals['total']}) · "
        f"base={base_summary['passed']}/{base_summary['total']} · "
        f"follow-up={follow_up_summary['passed']}/{follow_up_summary['total']} · "
        f"status={run_status}"
    )
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    for result in results:
        if result.get("outcome") in {"assertion_failed", "blocked_by_prerequisite"}:
            print(f"FAIL [{result['source']}] {result['question']}")
        elif result.get("outcome") in {"dependency_error", "execution_error"}:
            print(f"ERROR [{result['source']}] {result['question']}")
    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    return 0 if run_status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
