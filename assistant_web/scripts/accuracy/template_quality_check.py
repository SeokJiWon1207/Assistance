#!/usr/bin/env python3
"""팀 템플릿 12개로 만든 질문의 답변 품질을 실제 Mantis 저장본과 대조해 채점한다.

조회형 정확도 점검(dynamic_accuracy_check.py)은 '찾아서 보여 줘' 질문만 다룬다.
이 점검은 팀이 실제로 많이 쓰는 분석·보고서 템플릿 답변을 본다.

채점 항목(해당하는 것만 적용):
- completed: 실패 안내 없이 답변을 끝까지 만들었는가
- filter: 고객사·상태·기간 조건이 템플릿 입력대로 해석됐는가
- oracle: 건수·이슈 목록이 저장본에서 직접 센 값과 맞는가
- sections: 템플릿이 요구한 항목(원인, 다음 행동 등)을 담았는가
- grounding: 답변에 적힌 이슈 번호가 모두 실제로 있는가
- forbidden: 고객 안내문에 내부 식별자가 들어가지 않았는가
"""

from __future__ import annotations

import argparse
import json
import random
import re
import statistics
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from scripts.accuracy._runtime import reexec_in_project_venv

if __name__ == "__main__":
    reexec_in_project_venv(APP_DIR)

from backend.environment import load_dotenv

load_dotenv(APP_DIR / ".env")

from backend.config import CUSTOMER_ALIASES
from backend.mantis_status import mantis_status_bucket
from backend.template_questions import fill_template_question
from backend.team_templates import (
    customer_display_for_project,
    load_team_templates,
    recent_customer_issue_counts,
)


REPORT_DIR = APP_DIR / "runtime" / "accuracy_reports"
ACCURACY_USER_UID = "template-quality-" + uuid.uuid4().hex
# 한글 조사가 바로 붙어도(#31567을) 번호를 읽도록 \b 대신 숫자 경계만 본다.
ISSUE_REFERENCE = re.compile(r"#\s?0*(\d{4,6})(?!\d)")
# 정기점검 같은 반복 작업 티켓은 장애 분석·안내문 템플릿의 입력으로 맞지 않는다.
ROUTINE_SUMMARY = re.compile(r"정기\s*점검|서버\s*점검|점검\s*공지|월간\s*보고")
# 시스템이 답변을 끝내지 못했을 때만 쓰는 문구. '오류가 발생'처럼 장애 설명 본문에도 나오는 말은 넣지 않는다.
FAILURE_PHRASES = (
    "사용할 수 없습니다",
    "확정할 수 없습니다",
    "완료되지 않아",
    "응답하지 않",
    "시간이 초과",
)

# 템플릿 질문이 요구한 항목. 각 묶음 중 하나 이상의 표현이 답변에 있어야 한다.
REQUIRED_SECTIONS: Dict[str, List[tuple[str, ...]]] = {
    "daily-operations": [("고객사",)],
    "customer-analysis": [("|",)],
    "customer-profile": [("고객사",)],
    "aging-issues": [("오래", "경과", "일째", "일 경과", "등록")],
    "monthly-customer-report": [("전월", "지난달", "비교"), ("공수",)],
    "similar-issues": [("원인",), ("해결", "조치")],
    "effort-sla": [("|",), ("공수",), ("처리",)],
    "weekly-report": [("신규",), ("미종료",), ("장기", "적체"), ("공수",)],
    "incident-triage": [("영향",), ("확인",), ("유사",), ("전달", "담당")],
    "issue-handoff": [("현재 상태", "현재 상황"), ("다음",), ("주의",)],
    "incident-review": [("타임라인", "경과"), ("원인",), ("재발",)],
    "customer-notice": [("영향",), ("진행", "현재")],
}


@dataclass
class TemplateCase:
    id: str
    template_id: str
    title: str
    question: str
    values: Dict[str, str]
    customer: str = ""
    expected_statuses: List[str] = field(default_factory=list)
    expected_range: Optional[Dict[str, str]] = None
    expected_count: Optional[int] = None
    expected_issue_ids: List[int] = field(default_factory=list)
    base_issue_id: Optional[int] = None
    forbidden_terms: List[str] = field(default_factory=list)


# ── 질문 만들기 ─────────────────────────────────────────────────────────────


def fill_question(template: Mapping[str, Any], values: Mapping[str, str]) -> str:
    """frontend_core.js의 buildTeamTemplateQuestion과 같은 규칙으로 질문을 만든다."""
    normalized = {key: str(value or "").strip() for key, value in values.items()}
    for spec in template.get("fields") or []:
        name = spec["name"]
        if spec.get("normalize") == "issue-id":
            normalized[name] = normalized.get(name, "").lstrip("#")
        elif spec.get("normalize") == "month-korean":
            match = re.fullmatch(r"(20\d{2})-(0?[1-9]|1[0-2])", normalized.get(name, ""))
            if match:
                normalized[name] = f"{match.group(1)}년 {int(match.group(2))}월"
    return fill_template_question(template["questionTemplate"], normalized)


def default_values(template: Mapping[str, Any]) -> Dict[str, str]:
    return {spec["name"]: str(spec.get("value") or "") for spec in template.get("fields") or []}


# ── 저장본에서 직접 세기 ────────────────────────────────────────────────────


def created_date(issue: Mapping[str, Any]) -> Optional[date]:
    try:
        return date.fromisoformat(str(issue.get("created_at") or "")[:10])
    except ValueError:
        return None


def issue_customer(issue: Mapping[str, Any]) -> Optional[str]:
    project = issue.get("project") or {}
    name = str(project.get("name") or "") if isinstance(project, Mapping) else str(project)
    return customer_display_for_project(name) if name else None


def issue_bucket(issue: Mapping[str, Any]) -> str:
    status = issue.get("status") or {}
    return mantis_status_bucket(str(status.get("name") or "") if isinstance(status, Mapping) else str(status))


def count_issues(
    issues: Iterable[Mapping[str, Any]],
    *,
    start: date,
    end: date,
    customer: str = "",
    bucket: str = "",
) -> int:
    total = 0
    for issue in issues:
        created = created_date(issue)
        if created is None or not start <= created <= end:
            continue
        if customer and issue_customer(issue) != customer:
            continue
        if bucket and issue_bucket(issue) != bucket:
            continue
        total += 1
    return total


def aging_issue_ids(
    issues: Iterable[Mapping[str, Any]],
    *,
    customer: str,
    days: int,
    today: date,
) -> List[int]:
    """기준 일수 이상 열려 있는 이슈를 오래된 순(같은 날이면 번호순)으로 돌려준다.

    understanding.py가 'N일 넘게'를 등록일 ≤ 오늘-N일로 해석하므로 같은 경계를 쓴다.
    """
    cutoff = today - timedelta(days=days)
    matched = [
        issue for issue in issues
        if issue_bucket(issue) == "open"
        and (created := created_date(issue)) is not None
        and created <= cutoff
        and (not customer or issue_customer(issue) == customer)
    ]
    matched.sort(key=lambda issue: (str(issue.get("created_at") or ""), int(issue.get("id") or 0)))
    return [int(issue["id"]) for issue in matched]


def week_range(today: date, *, last: bool) -> tuple[date, date]:
    monday = today - timedelta(days=today.weekday())
    if last:
        monday -= timedelta(days=7)
    return monday, monday + timedelta(days=6)


def last_month_range(today: date) -> tuple[date, date]:
    first_this = today.replace(day=1)
    end = first_this - timedelta(days=1)
    return end.replace(day=1), end


def recent_months_range(today: date, months: int) -> tuple[date, date]:
    """'최근 N개월'은 이번 달을 포함한 달력 N개월로 해석한다(understanding.py와 같은 기준)."""
    year, month = today.year, today.month - (months - 1)
    while month < 1:
        month += 12
        year -= 1
    return date(year, month, 1), today


def as_range(start: date, end: date) -> Dict[str, str]:
    return {"from": start.isoformat(), "to": end.isoformat()}


# ── 입력값 고르기 ───────────────────────────────────────────────────────────


def busiest_customers(issues: List[Mapping[str, Any]], today: date) -> List[str]:
    counts = recent_customer_issue_counts(issues, today=today)
    return [name for name, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]


def pick_issue(
    issues: List[Mapping[str, Any]],
    rng: random.Random,
    *,
    today: date,
    bucket: str,
    customer: str = "",
    within_days: int = 120,
) -> Optional[Mapping[str, Any]]:
    since = today - timedelta(days=within_days)
    candidates = [
        issue for issue in issues
        if (created := created_date(issue)) is not None and created >= since
        and issue_bucket(issue) == bucket
        and (not customer or issue_customer(issue) == customer)
        and len(str(issue.get("summary") or "")) >= 8
        and not ROUTINE_SUMMARY.search(str(issue.get("summary") or ""))
    ]
    return rng.choice(candidates) if candidates else None


def symptom_from_summary(summary: str) -> str:
    text = re.sub(r"^\s*(\[[^\]]*\]\s*)+", "", str(summary or "")).strip()
    return text[:60]


def handler_name(issue: Mapping[str, Any]) -> str:
    handler = issue.get("handler") or {}
    return str(handler.get("real_name") or handler.get("name") or "") if isinstance(handler, Mapping) else ""


def build_cases(
    templates: List[Mapping[str, Any]],
    issues: List[Mapping[str, Any]],
    *,
    today: date,
    rng: random.Random,
) -> List[TemplateCase]:
    by_id = {template["id"]: template for template in templates}
    customers = busiest_customers(issues, today)
    if not customers:
        return []
    # 매일 같은 고객사만 보지 않도록 최근 이슈 상위 5곳 중에서 돌려 고른다.
    customer = rng.choice(customers[:5])
    open_issue = pick_issue(issues, rng, today=today, bucket="open", customer=customer) or pick_issue(
        issues, rng, today=today, bucket="open")
    done_issue = pick_issue(issues, rng, today=today, bucket="done", customer=customer) or pick_issue(
        issues, rng, today=today, bucket="done")
    cases: List[TemplateCase] = []

    def add(template_id: str, overrides: Dict[str, str], **expect: Any) -> None:
        template = by_id.get(template_id)
        if template is None:
            return
        values = {**default_values(template), **overrides}
        cases.append(TemplateCase(
            id=f"template-{template_id}",
            template_id=template_id,
            title=str(template.get("title") or template_id),
            question=fill_question(template, values),
            values=values,
            **expect,
        ))

    this_week = week_range(today, last=False)
    add("daily-operations", {"period": "이번 주", "status": "미종료"},
        expected_statuses=["open"], expected_range=as_range(*this_week),
        expected_count=count_issues(issues, start=this_week[0], end=this_week[1], bucket="open"))

    last_month = last_month_range(today)
    add("customer-analysis", {"customer": customer, "period": "지난달"},
        customer=customer, expected_range=as_range(*last_month),
        expected_count=count_issues(issues, start=last_month[0], end=last_month[1], customer=customer))

    recent = recent_months_range(today, 3)
    add("customer-profile", {"customer": customer, "period": "최근 3개월"},
        customer=customer, expected_range=as_range(*recent),
        expected_count=count_issues(issues, start=recent[0], end=recent[1], customer=customer))

    aging = aging_issue_ids(issues, customer=customer, days=30, today=today)
    add("aging-issues", {"customer": customer, "days": "30"},
        customer=customer, expected_statuses=["open"], expected_count=len(aging), expected_issue_ids=aging[:3])

    add("monthly-customer-report", {"customer": customer, "month": f"{last_month[0]:%Y-%m}"},
        customer=customer, expected_range=as_range(*last_month),
        expected_count=count_issues(issues, start=last_month[0], end=last_month[1], customer=customer))

    add("effort-sla", {"customer": customer, "period": "지난달"},
        customer=customer, expected_range=as_range(*last_month))

    last_week = week_range(today, last=True)
    add("weekly-report", {"customer": customer, "period": "지난주"},
        customer=customer,
        expected_count=count_issues(issues, start=last_week[0], end=last_week[1], customer=customer))

    if open_issue:
        add("incident-triage", {"symptom": symptom_from_summary(open_issue.get("summary")), "urgency": "높음"})
        open_id = int(open_issue["id"])
        add("issue-handoff", {"issueId": f"#{open_id}"}, base_issue_id=open_id)
    if done_issue:
        done_id = int(done_issue["id"])
        add("similar-issues", {"issueId": f"#{done_id}"}, base_issue_id=done_id)
        add("incident-review", {"issueId": f"#{done_id}"}, base_issue_id=done_id)
        handler = handler_name(done_issue)
        add("customer-notice", {"issueId": f"#{done_id}"}, base_issue_id=done_id,
            forbidden_terms=[f"#{done_id}", str(done_id), *([handler] if handler else [])])
    return cases


# ── 채점 ───────────────────────────────────────────────────────────────────


def normalize_text(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def customer_aliases(display: str) -> set[str]:
    for canonical, aliases in CUSTOMER_ALIASES.items():
        names = [canonical, *aliases]
        if display in names or any(normalize_text(display) == normalize_text(name) for name in names):
            return {normalize_text(name) for name in names}
    return {normalize_text(display)}


def response_constraints(response: Mapping[str, Any]) -> Dict[str, Any]:
    retrieval = response.get("retrieval") or {}
    return dict(retrieval.get("constraints") or {})


def response_issue_ids(response: Mapping[str, Any]) -> set[int]:
    return {int(source["issue_id"]) for source in response.get("sources") or [] if source.get("issue_id")}


def failed_stages(response: Mapping[str, Any]) -> List[str]:
    stages = (response.get("retrieval") or {}).get("stages") or []
    return [
        f"{stage.get('source')}: {str(stage.get('reason') or stage.get('status'))[:120]}"
        for stage in stages
        if stage.get("status") in {"error", "partial_error"}
    ]


def count_mentioned(content: str, count: int) -> bool:
    if count == 0 and re.search(r"(?:없습니다|없어요|0\s*건)", content):
        return True
    return bool(re.search(rf"(?<![\d,.]){count:,}\s*건|(?<![\d,.]){count}\s*건", content))


def check_filter(case: TemplateCase, constraints: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    if not constraints:
        return None
    problems: List[str] = []
    if case.customer:
        actual = {normalize_text(alias) for alias in constraints.get("customer_aliases") or []}
        actual.add(normalize_text(constraints.get("customer_key")))
        if not customer_aliases(case.customer) & actual:
            problems.append(f"고객사 {case.customer} → {constraints.get('customer_key') or '없음'}")
    missing_statuses = set(case.expected_statuses) - set(constraints.get("statuses") or [])
    if missing_statuses:
        problems.append(f"상태 {sorted(missing_statuses)} 누락")
    if case.expected_range:
        actual_range = constraints.get("created_at") or {}
        if (actual_range.get("from"), actual_range.get("to")) != (case.expected_range["from"], case.expected_range["to"]):
            problems.append(
                f"기간 {case.expected_range['from']}~{case.expected_range['to']} → "
                f"{actual_range.get('from') or '?'}~{actual_range.get('to') or '?'}"
            )
    return {"passed": not problems, "problems": problems}


def grade(case: TemplateCase, response: Mapping[str, Any], known_issue_ids: set[int]) -> Dict[str, Any]:
    content = str(response.get("content") or "")
    checks: Dict[str, Dict[str, Any]] = {}

    stage_errors = failed_stages(response)
    failure_phrase = next((phrase for phrase in FAILURE_PHRASES if phrase in content), "")
    failure_context = ""
    if failure_phrase:
        at = content.index(failure_phrase)
        failure_context = content[max(0, at - 40): at + len(failure_phrase)].replace("\n", " ")
    checks["completed"] = {
        "passed": not failure_phrase and not stage_errors and len(content.strip()) >= 40,
        "problems": [problem for problem in (
            f"실패 안내: '…{failure_context}'" if failure_phrase else "",
            *stage_errors,
            "답변이 너무 짧음" if len(content.strip()) < 40 else "",
        ) if problem],
    }

    filter_check = check_filter(case, response_constraints(response))
    if filter_check is not None:
        checks["filter"] = filter_check

    oracle_problems: List[str] = []
    oracle_applies = False
    if case.expected_count is not None:
        oracle_applies = True
        if not count_mentioned(content, case.expected_count):
            oracle_problems.append(f"저장본 기준 {case.expected_count}건이 답변에 없음")
    if case.expected_issue_ids:
        oracle_applies = True
        mentioned = response_issue_ids(response) | {int(value) for value in ISSUE_REFERENCE.findall(content)}
        missing = [issue_id for issue_id in case.expected_issue_ids if issue_id not in mentioned]
        if missing:
            oracle_problems.append(f"가장 오래된 이슈 {missing} 누락")
    if case.base_issue_id is not None:
        oracle_applies = True
        base_mentioned = re.search(rf"(?<!\d){case.base_issue_id}(?!\d)", content)
        if case.base_issue_id not in response_issue_ids(response) and not base_mentioned:
            oracle_problems.append(f"기준 이슈 #{case.base_issue_id}를 근거로 쓰지 않음")
        if case.template_id == "similar-issues":
            others = response_issue_ids(response) - {case.base_issue_id}
            if not others:
                oracle_problems.append("비교할 다른 이슈가 없음")
    if oracle_applies:
        checks["oracle"] = {"passed": not oracle_problems, "problems": oracle_problems}

    missing_sections = [
        "/".join(group) for group in REQUIRED_SECTIONS.get(case.template_id, [])
        if not any(term in content for term in group)
    ]
    checks["sections"] = {"passed": not missing_sections, "problems": [f"'{name}' 항목 없음" for name in missing_sections]}

    mentioned_ids = {int(value) for value in ISSUE_REFERENCE.findall(content)}
    unknown_ids = sorted(mentioned_ids - known_issue_ids)
    checks["grounding"] = {"passed": not unknown_ids, "problems": [f"없는 이슈 번호 #{value}" for value in unknown_ids]}

    if case.forbidden_terms:
        found = [term for term in case.forbidden_terms if term and term in content]
        checks["forbidden"] = {"passed": not found, "problems": [f"내부 정보 노출: {term}" for term in found]}

    return {
        **asdict(case),
        "passed": all(check["passed"] for check in checks.values()),
        "checks": checks,
        "answer": content[:6000],
        "source_count": len(response.get("sources") or []),
    }


# ── 실행 ───────────────────────────────────────────────────────────────────


def prepare_store(progress: bool, *, allow_stale: bool = False) -> List[Dict[str, Any]]:
    from backend.sources.mantis_store import read_complete_store, read_store_metadata, refresh_in_progress, sync_complete_store

    if allow_stale:
        # Mantis가 느려 전체 동기화가 안 될 때 이슈별 문서 템플릿만 빠르게 확인하는 용도.
        # 건수형 리포트는 최신 저장본을 요구하므로 '답변 완료'에서 실패할 수 있다.
        stale = read_complete_store(allow_stale=True, summary_only=True)
        if stale is None:
            raise RuntimeError("저장된 Mantis 원본이 없습니다.")
        emit(progress, f"저장본을 갱신 없이 사용합니다(기준 {stale.meta.get('fetchedAt')})")
        return [dict(row) for row in stale]
    if read_store_metadata() is None:
        emit(progress, "Mantis 저장본을 최신으로 맞추고 있습니다")
        result = sync_complete_store()
        deadline = time.monotonic() + 900
        if result.get("status") == "running":
            while refresh_in_progress() and read_store_metadata() is None and time.monotonic() < deadline:
                time.sleep(1)
    rows = read_complete_store(summary_only=True)
    if rows is None:
        raise RuntimeError("Mantis 저장본이 최신이 아니어서 템플릿 점검을 시작할 수 없습니다.")
    return [dict(row) for row in rows]


def run_case(case: TemplateCase, model_id: str, known_issue_ids: set[int]) -> Dict[str, Any]:
    from backend.schemas import ChatMessage, ChatRequest
    from backend.services.chat_service import chat_service

    started = time.perf_counter()
    try:
        response = chat_service.build_response(ChatRequest(
            modelId=model_id,
            messages=[ChatMessage(role="user", content=case.question)],
            temperature=0.0,
            seed=42,
            maxTokens=1600,
            searchLimit=5,
            showSources=True,
            abTest=False,
            stream=False,
            # 화면(runtime.js)이 보내는 도구 설정과 같게 둬야 실제 팀원이 받는 답변을 채점한다.
            tools={"mantis": True, "notion": True, "vectorDb": True},
        ), user={"uid": ACCURACY_USER_UID})
        result = grade(case, response, known_issue_ids)
    except Exception as exc:  # 한 템플릿이 실패해도 나머지 점검은 계속한다.
        result = {
            **asdict(case),
            "passed": False,
            "checks": {"completed": {"passed": False, "problems": [f"{type(exc).__name__}: {str(exc)[:300]}"]}},
            "answer": "",
            "source_count": 0,
        }
    return {**result, "latency_ms": round((time.perf_counter() - started) * 1000, 1)}


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(results)
    passed = sum(1 for result in results if result["passed"])
    dimensions: Dict[str, Dict[str, Any]] = {}
    for result in results:
        for name, check in result["checks"].items():
            entry = dimensions.setdefault(name, {"passed": 0, "total": 0})
            entry["total"] += 1
            entry["passed"] += int(bool(check["passed"]))
    for entry in dimensions.values():
        entry["score"] = round(entry["passed"] * 100 / entry["total"]) if entry["total"] else None
    latencies = sorted(result["latency_ms"] for result in results)
    return {
        "status": "passed" if total and passed == total else "failed",
        "score": round(passed * 100 / total) if total else None,
        "passed": passed,
        "total": total,
        "dimensions": dimensions,
        "latency": {
            "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
            "max_ms": latencies[-1] if latencies else None,
        },
    }


DIMENSION_LABELS = {
    "completed": "답변 완료",
    "filter": "조건 해석",
    "oracle": "건수·근거 대조",
    "sections": "요구 항목",
    "grounding": "이슈 번호 실재",
    "forbidden": "내부 정보 제외",
}


def write_report(report: Dict[str, Any]) -> tuple[Path, Path]:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    json_path = REPORT_DIR / f"template-quality-{stamp}.json"
    markdown_path = REPORT_DIR / f"template-quality-{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = report["summary"]
    lines = [
        "# 템플릿 답변 품질 점검",
        "",
        f"- 실행: {report['run_at']}",
        f"- 결과: {summary['status']} · {summary['score']}% ({summary['passed']}/{summary['total']})",
        f"- 응답 시간: 중앙값 {summary['latency']['p50_ms']} ms / 최대 {summary['latency']['max_ms']} ms",
        f"- 저장본: {report['store_count']}건 · 기준 고객사 {report['customer'] or '-'}",
        "",
        "| 항목 | 점수 |",
        "| --- | --- |",
        *[
            f"| {DIMENSION_LABELS.get(name, name)} | {entry['score']}% ({entry['passed']}/{entry['total']}) |"
            for name, entry in summary["dimensions"].items()
        ],
        "",
        "| 결과 | 템플릿 | 질문 | 문제 | 시간 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for result in report["results"]:
        problems = "; ".join(
            problem for check in result["checks"].values() for problem in check["problems"]
        ) or "-"
        question = result["question"].replace("|", "\\|")
        lines.append(
            f"| {'PASS' if result['passed'] else 'FAIL'} | {result['title']} | {question} | "
            f"{problems.replace('|', '/')} | {round(result['latency_ms'] / 1000, 1)}s |"
        )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def emit(enabled: bool, message: str) -> None:
    if enabled:
        print(f"PROGRESS {message}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", default=datetime.now().astimezone().strftime("%Y-%m-%d"))
    parser.add_argument("--model-id", default="")
    parser.add_argument("--only", default="", help="쉼표로 구분한 템플릿 ID만 실행")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--allow-stale", action="store_true", help="저장본을 갱신하지 않고 그대로 사용")
    args = parser.parse_args()

    from backend.llm import get_model_registry

    issues = prepare_store(args.progress, allow_stale=args.allow_stale)
    today = datetime.now().astimezone().date()
    templates = load_team_templates()["templates"]
    cases = build_cases(templates, issues, today=today, rng=random.Random(args.seed))
    if args.only:
        wanted = {value.strip() for value in args.only.split(",") if value.strip()}
        cases = [case for case in cases if case.template_id in wanted]
    models = [model for model in get_model_registry() if model.get("enabled", True)]
    model_id = args.model_id or (str(models[0]["id"]) if models else "")
    if not model_id:
        print("사용할 수 있는 모델이 없습니다.", file=sys.stderr)
        return 2

    known_issue_ids = {int(issue["id"]) for issue in issues if issue.get("id")}
    results = []
    for index, case in enumerate(cases, start=1):
        emit(args.progress, f"[{index}/{len(cases)}] {case.title}: {case.question}")
        results.append(run_case(case, model_id, known_issue_ids))
        emit(args.progress, f"  → {'PASS' if results[-1]['passed'] else 'FAIL'} ({results[-1]['latency_ms']} ms)")
    report = {
        "run_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "seed": args.seed,
        "model_id": model_id,
        "store_count": len(issues),
        "customer": next((case.customer for case in cases if case.customer), ""),
        "summary": summarize(results),
        "results": results,
    }
    json_path, markdown_path = write_report(report)
    print(markdown_path.read_text(encoding="utf-8"))
    print(f"JSON: {json_path}")
    return 0 if report["summary"]["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
