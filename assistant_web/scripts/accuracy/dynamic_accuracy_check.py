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

from backend.config import NOTION_API_BASE, discover_notion_database_ids, parse_notion_database_ids
from backend.llm import get_model_registry
from backend.schemas import ChatMessage, ChatRequest
from backend.services.chat_service import chat_service
from backend.sources import mantis
from backend.understanding import extract_query_constraints


REPORT_DIR = APP_DIR / "runtime" / "accuracy_reports"
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
    match = re.search(r"\(([^()]*(?:증권|금융|투자)[^()]*)\)", value)
    return match.group(1).strip() if match else value


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


def complete_mutable_mantis_ids(
    question: str,
    live_issues: List[Dict[str, Any]],
    limit: int,
) -> List[int]:
    """Complete a list oracle with live-sampled and full-index candidates.

    The paginated live sample can omit historical matches that are still the
    newest rows for a sparse filter such as a screen number. Combine it with
    the full local index. Mutable filters are revalidated by the Mantis source
    adapter before their candidates are accepted, which avoids defining
    handler/status expectations from stale fields.
    """
    constraints = extract_query_constraints(question)
    expected_ids = {
        int(issue["id"])
        for issue in live_issues
        if issue.get("id") and mantis.issue_matches_constraints(issue, constraints)
    }
    try:
        stage = mantis.execute_mantis_snapshot_search(max(limit * 2, 5), constraints)
        stage = mantis.revalidate_mantis_snapshot_stage(stage, constraints)
    except Exception:
        stage = {"status": "error", "issues_list": [], "meta": {}}
    if stage.get("status") == "ok" and not stage.get("meta", {}).get("live_fallback_count"):
        expected_ids.update(
            int(issue["id"])
            for issue in stage.get("issues_list", [])
            if issue.get("id")
        )
    return sorted(expected_ids, reverse=True)[:limit]


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
    """Run the chat service without depending on the authenticated HTTP route."""
    return chat_service.build_response(request, user=None)


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

    project_counts = Counter(str(item.get("project", {}).get("name") or "") for item in issues)
    projects = [name for name, count in project_counts.items() if name and count >= 3]
    rng.shuffle(projects)
    project = projects[0] if projects else str(exact.get("project", {}).get("name") or "")
    project_issues = [item for item in issues if item.get("project", {}).get("name") == project]
    cases.append(AccuracyCase(
        "mantis-project-latest", "mantis", "고객사 최신 이슈",
        f"{human_project_name(project)} 최근 이슈 3개 보여줘", "ordered_issue_ids",
        [int(item["id"]) for item in project_issues[:3]],
    ))

    pairs = Counter((str(item.get("project", {}).get("name") or ""), status_name(item)) for item in issues)
    valid_pairs = [pair for pair, count in pairs.items() if pair[0] and pair[1] and count >= 2]
    pair_project, pair_status = rng.choice(valid_pairs) if valid_pairs else (project, status_name(project_issues[0]))
    pair_issues = [
        item for item in issues
        if item.get("project", {}).get("name") == pair_project and status_name(item) == pair_status
    ]
    pair_question = f"{human_project_name(pair_project)}에서 상태가 {pair_status}인 최근 이슈 2개 보여줘"
    pair_expected = [int(item["id"]) for item in pair_issues[:2]]
    if augment_mutable_oracle:
        pair_expected = complete_mutable_mantis_ids(pair_question, issues, 2) or pair_expected
    cases.append(AccuracyCase(
        "mantis-project-status", "mantis", "고객사·상태",
        pair_question, "ordered_issue_ids", pair_expected, mutable_oracle=True,
    ))

    project_platform_groups: Dict[tuple[str, str], List[Dict[str, Any]]] = {}
    for item in issues:
        item_project = str(item.get("project", {}).get("name") or "").strip()
        platform = normalize(custom_field(item, "매체구분"))
        if item_project and platform in {"hts", "mts"}:
            project_platform_groups.setdefault((item_project, platform.upper()), []).append(item)
    project_platform_pairs = [
        pair
        for pair, rows in project_platform_groups.items()
        if len(rows) >= 2
    ]
    if project_platform_pairs:
        platform_project, platform = rng.choice(sorted(project_platform_pairs))
        platform_question = (
            f"{human_project_name(platform_project)} {platform} 최근 이슈 2개만 최신순으로 보여줘"
        )
        platform_expected = [
            int(item["id"])
            for item in issues
            if mantis.issue_matches_constraints(item, extract_query_constraints(platform_question))
        ][:2]
        if augment_mutable_oracle:
            platform_expected = (
                complete_mutable_mantis_ids(platform_question, issues, 2) or platform_expected
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
            ))

    exclusion_candidates: List[tuple[str, str, List[int]]] = []
    for candidate_project in sorted(project_counts):
        if not candidate_project:
            continue
        for excluded_platform in ("HTS", "MTS"):
            exclusion_question = (
                f"{human_project_name(candidate_project)} 이슈에서 {excluded_platform}는 빼고 "
                "최근 3개만 보여줘"
            )
            matching_ids = [
                int(item["id"])
                for item in issues
                if mantis.issue_matches_constraints(item, extract_query_constraints(exclusion_question))
            ][:3]
            if len(matching_ids) >= 2:
                exclusion_candidates.append((candidate_project, excluded_platform, matching_ids))
    if exclusion_candidates:
        excluded_project, excluded_platform, exclusion_expected = rng.choice(exclusion_candidates)
        exclusion_question = (
            f"{human_project_name(excluded_project)} 이슈에서 {excluded_platform}는 빼고 "
            "최근 3개만 보여줘"
        )
        if augment_mutable_oracle:
            exclusion_expected = (
                complete_mutable_mantis_ids(exclusion_question, issues, 3) or exclusion_expected
            )
        cases.append(AccuracyCase(
            "mantis-project-platform-exclusion",
            "mantis",
            "고객사·플랫폼 제외",
            exclusion_question,
            "ordered_issue_ids",
            exclusion_expected,
            mutable_oracle=True,
        ))

    screen_groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in issues:
        for screen in exact_screen_numbers(item):
            screen_groups.setdefault(screen, []).append(item)
    if screen_groups:
        screen = rng.choice(sorted(screen_groups))
        screen_question = f"화면번호 {screen} 관련 이슈 3개 보여줘"
        screen_expected = sorted(
            (int(item["id"]) for item in screen_groups[screen]),
            reverse=True,
        )[:3]
        if augment_mutable_oracle:
            screen_expected = (
                complete_mutable_mantis_ids(screen_question, issues, 3)
                or screen_expected
            )
        cases.append(AccuracyCase(
            "mantis-screen", "mantis", "화면번호", screen_question,
            "ordered_issue_ids", screen_expected, mutable_oracle=True,
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
        handler_expected = [int(item["id"]) for item in handler_groups[handler][:2]]
        if augment_mutable_oracle:
            handler_expected = complete_mutable_mantis_ids(handler_question, issues, 2) or handler_expected
        cases.append(AccuracyCase(
            "mantis-handler", "mantis", "담당자", handler_question,
            "ordered_issue_ids", handler_expected, mutable_oracle=True,
        ))

    dated = [item for item in project_issues if re.match(r"\d{4}-\d{2}", str(item.get("created_at") or ""))]
    month_counts = Counter(str(item.get("created_at"))[:7] for item in dated)
    months = [month for month, count in month_counts.items() if count > 0]
    if months:
        month = rng.choice(sorted(months))
        year, month_number = month.split("-")
        cases.append(AccuracyCase(
            "mantis-month-count", "mantis", "월별 건수",
            f"최신 {len(issues)}건 기준으로 {year}년 {int(month_number)}월에 {human_project_name(project)}에서 들어온 이슈는 몇 개야?",
            "count", month_counts[month],
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
        f"화면번호 {absent_screen} 관련 Mantis 이슈가 있는지 확인해줘",
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
            f"노션에서 '{page['title']}' 항목을 찾아서 알려줘", "titles", [page["title"]],
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
            question = case.question.replace("3개", "세 개").replace("2개", "두 개").replace("보여줘", "보여줄래?")
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
            project_alias = human_project_name(project)
            scenarios.append(FollowUpScenario(
                "followup-exact-project",
                "mantis",
                "후속 질문·대상 속성",
                exact_case.id,
                "이 이슈는 어느 고객사 건이야?",
                "text_any",
                list(dict.fromkeys([project_alias, project])),
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
        setup_project = str((setup_issue or {}).get("project", {}).get("name") or "")
        setup_status = status_name(setup_issue or {})
        alternate_groups: Dict[str, List[Dict[str, Any]]] = {}
        for issue in mantis_issues:
            if str(issue.get("project", {}).get("name") or "") != setup_project:
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
                f"아니, 고객사는 그대로 두고 상태만 {alternate_status}로 바꿔서 가장 최근 이슈 하나를 알려줘",
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


def filter_recall_matches(question: str, actual: Dict[str, Any]) -> bool:
    expected = extract_query_constraints(question)
    scalar_keys = ("customer_key", "platform_key")
    if any(expected.get(key) and expected.get(key) != actual.get(key) for key in scalar_keys):
        return False
    list_keys = (
        "statuses",
        "issue_ids",
        "screen_numbers",
        "excluded_statuses",
        "excluded_platform_keys",
    )
    for key in list_keys:
        if not set(expected.get(key) or []).issubset(set(actual.get(key) or [])):
            return False
    expected_people = set((expected.get("people") or {}).get("names") or [])
    actual_people = set((actual.get("people") or {}).get("names") or [])
    if not expected_people.issubset(actual_people):
        return False
    expected_range = expected.get("created_at") or {}
    actual_range = actual.get("created_at") or {}
    return not expected_range or expected_range == actual_range


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
        passed = not actual_ids and not sources and any(
            marker in normalize(content) for marker in ("없", "찾지못", "0건", "0개")
        )
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
    grounding_text = str(retrieval.get("query") or case.question)
    ungrounded = (
        ungrounded_constraint_labels(grounding_text, actual_constraints)
        if actual_constraints
        else []
    )
    filter_precision_passed = not ungrounded if actual_constraints else None
    filter_recall_passed = (
        filter_recall_matches(case.question, actual_constraints)
        if actual_constraints
        else None
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

    # The oracle must be independent from the system under test. Rebuild it
    # from the question and a fresh live candidate universe; using the returned
    # constraints or only expected/actual IDs can bless the same retrieval bug
    # that produced the wrong answer.
    try:
        live_issues = fetch_mantis_snapshot()
        refreshed_expected = complete_mutable_mantis_ids(case.question, live_issues, limit)
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

    candidate_cases = build_mantis_cases(
        mantis_issues,
        rng,
        augment_mutable_oracle=True,
    ) + build_notion_cases(notion_pages, rng)
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
