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
from dataclasses import asdict, dataclass
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

load_dotenv(APP_DIR / ".env")

from backend.config import NOTION_API_BASE, parse_notion_database_ids
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
    """Complete a handler/status oracle with live-verified local candidates.

    The paginated live sample can omit older IDs that are still newer than
    other sampled matches. Combine it with the full local index, but include
    local candidates only when every displayed candidate was revalidated by an
    exact live lookup. This avoids defining expectations from stale fields.
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


def fetch_mantis_snapshot() -> List[Dict[str, Any]]:
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
    database_ids = parse_notion_database_ids()
    if not token or not database_ids:
        raise RuntimeError("NOTION_API_TOKEN/NOTION_DATABASE_IDS is not configured")

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
        "issue_ids", [int(exact["id"])],
    ))

    project_counts = Counter(str(item.get("project", {}).get("name") or "") for item in issues)
    projects = [name for name, count in project_counts.items() if name and count >= 3]
    rng.shuffle(projects)
    project = projects[0] if projects else str(exact.get("project", {}).get("name") or "")
    project_issues = [item for item in issues if item.get("project", {}).get("name") == project]
    cases.append(AccuracyCase(
        "mantis-project-latest", "mantis", "고객사 최신 이슈",
        f"{human_project_name(project)} 최근 이슈 3개 보여줘", "issue_ids",
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
        pair_question, "issue_ids", pair_expected,
    ))

    screen_groups: Dict[str, List[Dict[str, Any]]] = {}
    for item in issues:
        for screen in exact_screen_numbers(item):
            screen_groups.setdefault(screen, []).append(item)
    if screen_groups:
        screen = rng.choice(sorted(screen_groups))
        cases.append(AccuracyCase(
            "mantis-screen", "mantis", "화면번호", f"화면번호 {screen} 관련 이슈 3개 보여줘",
            "issue_ids", [int(item["id"]) for item in screen_groups[screen][:3]],
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
            "issue_ids", handler_expected,
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
    return cases[:6]


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
    return [
        AccuracyCase(
            f"notion-title-{index + 1}", "notion", "제목 검색",
            f"노션에서 '{page['title']}' 항목을 찾아서 알려줘", "titles", [page["title"]],
        )
        for index, page in enumerate(selected)
    ]


def build_follow_up_scenarios(
    cases: List[AccuracyCase],
    mantis_issues: List[Dict[str, Any]],
    limit: int = 4,
) -> List[FollowUpScenario]:
    """Build deterministic conversation checks from already generated base cases.

    The setup turn is one of the ten base cases, so four follow-up scenarios add
    only four model calls. Expected values come from the live snapshot oracle,
    never from the model's first answer.
    """
    if limit <= 0:
        return []
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
    if status_case and status_case.expected:
        scenarios.append(FollowUpScenario(
            "followup-project-status-latest",
            "mantis",
            "후속 질문·조건 상속",
            status_case.id,
            "방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘",
            "exact_issue_ids",
            [int(status_case.expected[0])],
        ))

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
            "titles",
            list(notion_case.expected),
        ))

    return scenarios[:limit]


def observed_issue_ids(response: Dict[str, Any]) -> set[int]:
    ids = {
        int(source["issue_id"])
        for source in response.get("sources") or []
        if source.get("issue_id")
    }
    ids.update(int(value) for value in re.findall(r"#?0*(\d{5,7})\b", response.get("content", "")))
    return ids


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


def filter_recall_matches(question: str, actual: Dict[str, Any]) -> bool:
    expected = extract_query_constraints(question)
    scalar_keys = ("customer_key", "platform_key")
    if any(expected.get(key) and expected.get(key) != actual.get(key) for key in scalar_keys):
        return False
    list_keys = ("statuses", "issue_ids", "screen_numbers", "excluded_statuses")
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
    elif case.expected_kind == "titles":
        haystacks = [content, *(str(source.get("title") or "") for source in sources)]
        expected_titles = [str(value) for value in case.expected]
        missing = [title for title in expected_titles if not any(normalize(title) in normalize(text) for text in haystacks)]
        passed = not missing
        actual = {"matched": len(expected_titles) - len(missing), "missing": missing}
    elif case.expected_kind == "text_any":
        haystack = normalize(
            " ".join([
                content,
                json.dumps(sources, ensure_ascii=False, default=str),
            ])
        )
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
    else:
        actual = None
        passed = False
    dependency_reason = ""
    for stage in response.get("retrievalStages") or []:
        if stage.get("status") not in {"error", "partial_error"}:
            continue
        reason = str(stage.get("reason") or "")
        if any(token in reason.casefold() for token in ("timeout", "timed out", "connection failed")):
            dependency_reason = reason
            break
    if not dependency_reason and any(
        token in content.casefold()
        for token in ("connection failed", "timed out", "readtimeout", "연결 실패")
    ):
        dependency_reason = content[:500]

    retrieval = response.get("retrieval") or {}
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
    outcome = "passed" if passed else "dependency_error" if dependency_reason else "assertion_failed"
    return {
        **asdict(case),
        "passed": passed,
        "outcome": outcome,
        "actual": actual,
        "answer_passed": passed,
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


def run_case(case: AccuracyCase, model_id: str) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        tools = {
            "mantis": case.source == "mantis",
            "notion": case.source == "notion",
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
            stream=False,
            tools=tools,
        ))
        return {**grade(case, response), "latency_ms": round((time.perf_counter() - started) * 1000, 1)}
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
            "mantis": scenario.source == "mantis",
            "notion": scenario.source == "notion",
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
    args = parser.parse_args()

    rng = random.Random(args.seed)
    # Page blocks add many requests and are not needed for title/metadata checks.
    os.environ["NOTION_FETCH_PAGE_BLOCKS"] = "false"
    errors: List[str] = []
    emit_progress(args.progress, {"type": "status", "message": "Mantis·Notion 실시간 스냅샷을 수집하고 있습니다"})
    try:
        mantis_issues = fetch_mantis_snapshot()
    except Exception as exc:
        mantis_issues = []
        errors.append(f"Mantis snapshot failed: {type(exc).__name__}: {str(exc)[:300]}")
    try:
        notion_pages = fetch_notion_snapshot()
    except Exception as exc:
        notion_pages = []
        errors.append(f"Notion snapshot failed: {type(exc).__name__}: {str(exc)[:300]}")

    cases = build_mantis_cases(
        mantis_issues,
        rng,
        augment_mutable_oracle=True,
    ) + build_notion_cases(notion_pages, rng)
    cases = cases[: args.questions]
    follow_up_scenarios = build_follow_up_scenarios(
        cases,
        mantis_issues,
        args.follow_up_scenarios,
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
    latency_summary = summarize_latency(results)
    incomplete = (
        len(cases) < args.questions
        or len(follow_up_scenarios) < args.follow_up_scenarios
        or totals["dependency_errors"] > 0
        or totals["execution_errors"] > 0
        or bool(errors)
    )
    run_status = "incomplete" if incomplete else "failed" if totals["failed"] else "passed"
    report = {
        "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seed": args.seed,
        "model_id": model_id,
        "requested_questions": args.questions,
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
