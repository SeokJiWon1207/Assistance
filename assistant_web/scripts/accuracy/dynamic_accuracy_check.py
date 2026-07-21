#!/usr/bin/env python3
"""Run reproducible end-to-end accuracy checks against live Mantis and Notion data."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from backend import app as assistant


REPORT_DIR = APP_DIR / "runtime" / "accuracy_reports"
MANTIS_PAGES = 8
MANTIS_PAGE_SIZE = 100
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
    constraints = assistant.extract_query_constraints(question)
    expected_ids = {
        int(issue["id"])
        for issue in live_issues
        if issue.get("id") and assistant.issue_matches_constraints(issue, constraints)
    }
    try:
        stage = assistant.execute_mantis_snapshot_search(max(limit * 2, 5), constraints)
        stage = assistant.revalidate_mantis_snapshot_stage(stage, constraints)
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
        response = requests.get(
            url,
            headers=headers,
            params={"page_size": MANTIS_PAGE_SIZE, "page": page},
            timeout=30,
        )
        response.raise_for_status()
        batch = response.json().get("issues", [])
        if not batch:
            break
        issues.extend(batch)
    return sorted(issues, key=lambda item: int(item.get("id") or 0), reverse=True)


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
    database_ids = assistant.parse_notion_database_ids()
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
            f"{assistant.NOTION_API_BASE}/databases/{database_id}",
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
                f"{assistant.NOTION_API_BASE}/databases/{database_id}/query",
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


def grade(case: AccuracyCase, response: Dict[str, Any]) -> Dict[str, Any]:
    content = str(response.get("content") or "")
    sources = response.get("sources") or []
    if case.expected_kind == "issue_ids":
        expected = {int(value) for value in case.expected}
        observed = observed_issue_ids(response)
        passed = expected.issubset(observed)
        actual: Any = sorted(observed)
    elif case.expected_kind == "titles":
        haystacks = [content, *(str(source.get("title") or "") for source in sources)]
        expected_titles = [str(value) for value in case.expected]
        missing = [title for title in expected_titles if not any(normalize(title) in normalize(text) for text in haystacks)]
        passed = not missing
        actual = {"matched": len(expected_titles) - len(missing), "missing": missing}
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

    outcome = "passed" if passed else "dependency_error" if dependency_reason else "assertion_failed"
    return {
        **asdict(case),
        "passed": passed,
        "outcome": outcome,
        "actual": actual,
        "answer": content[:2000],
        "source_count": len(sources),
        "audit_id": response.get("auditId"),
        **({"dependency_error": dependency_reason} if dependency_reason else {}),
    }


def run_case(case: AccuracyCase, model_id: str) -> Dict[str, Any]:
    try:
        tools = {
            "mantis": case.source == "mantis",
            "notion": case.source == "notion",
            "vectorDb": False,
        }
        response = assistant.chat(assistant.ChatRequest(
            modelId=model_id,
            messages=[assistant.ChatMessage(role="user", content=case.question)],
            temperature=0.0,
            seed=42,
            maxTokens=900,
            searchLimit=5,
            showSources=True,
            stream=False,
            tools=tools,
        ))
        return grade(case, response)
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
        }


def write_report(report: Dict[str, Any]) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    json_path = REPORT_DIR / f"accuracy-{stamp}.json"
    markdown_path = REPORT_DIR / f"accuracy-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = report["summary"]
    score_label = f"{summary['score']}%" if summary.get("score") is not None else "N/A"
    lines = [
        "# Dynamic accuracy report",
        "",
        f"- Run: {report['run_at']}",
        f"- Status: {summary.get('status', 'unknown')}",
        f"- Accuracy: {score_label} ({summary['passed']}/{summary['total']} evaluated)",
        f"- Attempted: {summary.get('attempted', summary['total'])}",
        f"- Dependency errors: {summary.get('dependency_errors', 0)}",
        f"- Mantis snapshot: {report['snapshots']['mantis']} items",
        f"- Notion snapshot: {report['snapshots']['notion']} items",
        "",
        "| Result | Source | Category | Question | Expected | Actual |",
        "| --- | --- | --- | --- | --- | --- |",
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
        lines.append(
            f"| {mark} | {result['source']} | {result['category']} | "
            f"{result['question'].replace('|', '/')} | {expected} | {actual} |"
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
    models = [model for model in assistant.get_model_registry() if model.get("enabled", True)]
    model_id = args.model_id or (str(models[0]["id"]) if models else "")
    if not model_id:
        errors.append("No enabled model is configured")

    emit_progress(args.progress, {"type": "run_start", "total": len(cases) if model_id else 0})
    results: List[Dict[str, Any]] = []
    if model_id:
        for index, case in enumerate(cases, start=1):
            emit_progress(args.progress, {
                "type": "case_start",
                "caseId": case.id,
                "index": index,
                "total": len(cases),
                "question": case.question,
                "source": case.source,
                "category": case.category,
            })
            result = run_case(case, model_id)
            results.append(result)
            emit_progress(args.progress, {"type": "case_result", "result": result})
    passed = sum(1 for result in results if result.get("passed"))
    dependency_errors = sum(1 for result in results if result.get("outcome") == "dependency_error")
    execution_errors = sum(1 for result in results if result.get("outcome") == "execution_error")
    attempted = len(results)
    evaluated = attempted - dependency_errors - execution_errors
    failed = sum(1 for result in results if result.get("outcome") == "assertion_failed")
    incomplete = len(cases) < args.questions or dependency_errors > 0 or execution_errors > 0 or bool(errors)
    run_status = "incomplete" if incomplete else "failed" if failed else "passed"
    report = {
        "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seed": args.seed,
        "model_id": model_id,
        "requested_questions": args.questions,
        "generated_questions": len(cases),
        "snapshots": {"mantis": len(mantis_issues), "notion": len(notion_pages)},
        "summary": {
            "attempted": attempted,
            "total": evaluated,
            "evaluated": evaluated,
            "passed": passed,
            "failed": failed,
            "dependency_errors": dependency_errors,
            "execution_errors": execution_errors,
            "score": round(passed / evaluated * 100) if evaluated else None,
            "status": run_status,
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
    print(f"Accuracy score: {score_label} ({passed}/{evaluated}) · status={run_status}")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    for result in results:
        if result.get("outcome") == "assertion_failed":
            print(f"FAIL [{result['source']}] {result['question']}")
        elif result.get("outcome") in {"dependency_error", "execution_error"}:
            print(f"ERROR [{result['source']}] {result['question']}")
    for error in errors:
        print(f"ERROR {error}", file=sys.stderr)
    return 0 if run_status == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
