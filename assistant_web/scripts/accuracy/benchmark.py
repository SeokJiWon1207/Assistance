#!/usr/bin/env python3
"""고정 문항으로 답변 정확도를 측정하는 벤치마크.

문항과 정답은 사람이 원본에서 확정해 JSONL로 고정한다. ``run``은 실제 서비스로
답변을 받아 저장만 하고, ``score``는 저장된 답변과 정답을 비교만 한다. 채점은
모델·데이터 접근 없이 다시 실행할 수 있으며 제품의 조건 해석기나 매처를 쓰지 않는다.
수정에 사용한 문항은 ``dev``, 최종 점수에 쓰는 미사용 문항은 ``final``로 나눈다.

실제 이슈 원문이 담긴 문항 파일과 실행 결과는 ``runtime/`` 아래에 두고 Git에 추가하지 않는다.

사용법(assistant_web 디렉터리):
    .venv/bin/python scripts/accuracy/benchmark.py validate runtime/accuracy_benchmark/cases.jsonl
    .venv/bin/python scripts/accuracy/benchmark.py run runtime/accuracy_benchmark/cases.jsonl --model-id <모델 ID>
    .venv/bin/python scripts/accuracy/benchmark.py score runtime/accuracy_benchmark/cases.jsonl <실행 폴더>
"""

from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import math
import os
import re
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from contextlib import nullcontext
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Literal, Optional, cast

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from scripts.accuracy._runtime import reexec_in_project_venv
from scripts.accuracy import benchmark_outcomes as outcomes
from scripts.accuracy.benchmark_observation import summarize_measurements

if __name__ == "__main__":
    reexec_in_project_venv(APP_DIR)

REPORT_DIR = APP_DIR / "runtime" / "accuracy_reports"
SPLITS = {"dev", "final"}
CATEGORIES = {"count", "followup", "list", "aggregate", "explain", "notion", "date", "similar", "report", "clarify", "general"}
ID_MATCHES = {"exact", "includes", "excludes"}
RELATIVE_PERIODS = {"today", "yesterday", "this_week", "last_week", "this_month", "last_month", "recent_days", "recent_months"}
PLAN_KEYS = {"source", "operation", "report_kind", "statuses", "excluded_statuses", "people",
             "customer", "platform", "issue_ids", "created_at"}
CLARIFY_MODES = outcomes.CLARIFY_MODES
BLOCKED_MODE = "verification_blocked"
# 실제 모델의 도구 선택·호출량을 기존 경로와 같은 문항으로 비교한다(--agent on/off).
AGENT_KEYS = {"entered", "tools_include", "tools_exclude", "max_tool_calls", "finalization"}


class CaseError(ValueError):
    """문항 파일 형식 오류."""


# ---------------------------------------------------------------- 문항 형식

def _require(ok: bool, message: str) -> None:
    if not ok:
        raise CaseError(message)


def _is_int_list(value: Any) -> bool:
    return isinstance(value, list) and all(type(item) is int for item in value)


def _is_text_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)


def validate_expectation(expect: Any, where: str) -> None:
    _require(isinstance(expect, dict), f"{where}: 기대값은 객체여야 합니다")
    kind = expect.get("type")
    if kind in outcomes.EXPECTATIONS:
        try:
            outcomes.validate_expectation(expect)
        except (ValueError, TypeError, KeyError) as exc:
            raise CaseError(f'{where}: {exc}') from exc
    elif kind == "count":
        _require(type(expect.get("value")) is int and expect["value"] >= 0, f"{where}: count.value는 0 이상의 정수입니다")
    elif kind == "issue_ids":
        _require(_is_int_list(expect.get("ids")), f"{where}: issue_ids.ids는 정수 목록입니다")
        _require(expect.get("match", "exact") in ID_MATCHES, f"{where}: issue_ids.match는 {sorted(ID_MATCHES)} 중 하나입니다")
        _require(isinstance(expect.get("ordered", False), bool) and
                 (not expect.get("ordered") or expect.get("match", "exact") == "exact"),
                 f"{where}: ordered는 정확한 전체 ID 목록에만 사용할 수 있습니다")
    elif kind == "notion_titles":
        _require(_is_text_list(expect.get("titles")), f"{where}: notion_titles.titles는 문자열 목록입니다")
        _require(expect.get("match", "includes") in ID_MATCHES, f"{where}: notion_titles.match는 {sorted(ID_MATCHES)} 중 하나입니다")
    elif kind == "contains":
        groups = expect.get("all")
        _require(isinstance(groups, list) and bool(groups) and all(
            (isinstance(group, str) and group) or _is_text_list(group) and group for group in groups),
            f"{where}: contains.all은 문자열 또는 동의어 목록의 목록입니다")
    elif kind == "not_contains":
        _require(_is_text_list(expect.get("any")) and expect["any"], f"{where}: not_contains.any는 문자열 목록입니다")
    elif kind == "mode":
        _require(_is_text_list(expect.get("in")) and expect["in"], f"{where}: mode.in은 문자열 목록입니다")
    elif kind in {"clarify", "delivered"}:
        pass
    elif kind == "agent":
        keys = set(expect) - {"type"}
        _require(bool(keys) and keys <= AGENT_KEYS, f"{where}: agent 항목은 {sorted(AGENT_KEYS)} 중에서 지정합니다")
        _require("entered" not in expect or isinstance(expect["entered"], bool), f"{where}: agent.entered는 true/false입니다")
        for key in ("tools_include", "tools_exclude", "finalization"):
            _require(key not in expect or _is_text_list(expect[key]), f"{where}: agent.{key}는 문자열 목록입니다")
        _require("max_tool_calls" not in expect or (type(expect["max_tool_calls"]) is int and expect["max_tool_calls"] >= 0),
                 f"{where}: agent.max_tool_calls는 0 이상의 정수입니다")
    elif kind == "plan":
        keys = set(expect) - {"type"}
        _require(bool(keys) and keys <= PLAN_KEYS, f"{where}: plan 항목은 {sorted(PLAN_KEYS)} 중에서 지정합니다")
        for key in ("statuses", "excluded_statuses", "people"):
            _require(key not in expect or _is_text_list(expect[key]) or expect[key] == [], f"{where}: plan.{key}는 문자열 목록입니다")
        _require("issue_ids" not in expect or _is_int_list(expect["issue_ids"]), f"{where}: plan.issue_ids는 정수 목록입니다")
        if "created_at" in expect:
            period = expect["created_at"]
            _require(period is None or isinstance(period, dict), f"{where}: plan.created_at은 객체 또는 null입니다")
            if isinstance(period, dict) and "relative" in period:
                _require(period["relative"] in RELATIVE_PERIODS, f"{where}: created_at.relative는 {sorted(RELATIVE_PERIODS)} 중 하나입니다")
                _require(period["relative"] not in {"recent_days", "recent_months"} or (type(period.get("n")) is int and period["n"] > 0),
                         f"{where}: recent_days/recent_months에는 양의 정수 n이 필요합니다")
            elif isinstance(period, dict):
                for key in ("from", "to"):
                    _require(isinstance(period.get(key), str), f"{where}: created_at.{key}는 YYYY-MM-DD입니다")
                    try:
                        date.fromisoformat(period[key])
                    except ValueError as exc:
                        raise CaseError(f"{where}: created_at.{key} 날짜 형식 오류") from exc
    else:
        raise CaseError(f"{where}: 알 수 없는 기대값 종류 {kind!r}")


def validate_case(case: Any, line: int) -> Dict[str, Any]:
    _require(isinstance(case, dict), f"{line}행: 문항은 객체여야 합니다")
    case_id = case.get("id")
    _require(isinstance(case_id, str) and bool(case_id.strip()), f"{line}행: id가 필요합니다")
    _require(case.get("split") in SPLITS, f"{case_id}: split은 dev 또는 final입니다")
    _require(case.get("category") in CATEGORIES, f"{case_id}: category는 {sorted(CATEGORIES)} 중 하나입니다")
    turns = case.get("turns")
    _require(isinstance(turns, list) and bool(turns), f"{case_id}: turns가 비어 있습니다")
    for index, turn in enumerate(turns, 1):
        where = f"{case_id} {index}번째 질문"
        _require(isinstance(turn, dict) and isinstance(turn.get("question"), str) and bool(turn["question"].strip()),
                 f"{where}: question이 필요합니다")
        expects = turn.get("expect")
        _require(isinstance(expects, list) and bool(expects), f"{where}: expect가 비어 있습니다")
        for expect in expects:
            validate_expectation(expect, where)
        requirements = turn.get('requirements', [])
        _require(isinstance(requirements, list), f'{where}: requirements는 항목 목록입니다')
        seen_requirements = set()
        for item in requirements:
            _require(isinstance(item, dict) and isinstance(item.get('id'), str) and bool(item['id'])
                     and item['id'] not in seen_requirements and isinstance(item.get('label'), str)
                     and bool(item['label']), f'{where}: 요청 항목의 고유 id와 label이 필요합니다')
            seen_requirements.add(item['id'])
            _require(isinstance(item.get('expect'), list) and bool(item['expect']), f'{where}: 항목의 expect가 필요합니다')
            for expectation in item['expect']:
                validate_expectation(expectation, where + '/' + item['id'])
    _require(case.get("tools") is None or (isinstance(case["tools"], dict) and all(
        isinstance(value, bool) for value in case["tools"].values())), f"{case_id}: tools는 도구별 true/false입니다")
    return case


def load_cases(path: Path) -> List[Dict[str, Any]]:
    cases: List[Dict[str, Any]] = []
    seen = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("//"):
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CaseError(f"{line_number}행: JSON 형식 오류 ({exc.msg})") from exc
        case = validate_case(raw, line_number)
        _require(case["id"] not in seen, f"{case['id']}: 중복된 id입니다")
        seen.add(case["id"])
        cases.append(case)
    _require(bool(cases), "문항이 없습니다")
    return cases


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------- 실행

def compact_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """채점과 재검토에 필요한 필드만 남긴다. 원문 상세(detail)는 저장하지 않는다."""
    retrieval = response.get("retrieval") or {}
    verification = response.get("verification") or {}
    return {
        "modelId": response.get("modelId"),
        "auditId": response.get("auditId"),
        "content": str(response.get("content") or ""),
        "mode": str((response.get("raw") or {}).get("mode") or "model"),
        "sources": [{key: source.get(key) for key in ("type", "issue_id", "page_id", "title", "project", "status")}
                    for source in response.get("sources") or []],
        "queryPlan": retrieval.get("query_plan") or retrieval.get("intent_plan"),
        "stages": [{"source": stage.get("source"), "status": stage.get("status"), "meta": {
            key: (stage.get("meta") or {}).get(key)
            for key in ("operation", "total_count", "found_count", "search_complete", "fetchedAt", "tool")
        }} for stage in retrieval.get("stages") or []],
        "verification": {"status": verification.get("status"), "delivery": verification.get("delivery"),
                         'checks': [{'id': c.get('id'), 'status': c.get('status')} for c in verification.get('checks') or []]},
        "timings": response.get("timings"),
        "agent": compact_agent(response.get("agentReview")),
        "followupIssueIds": ([int(s['issue_id']) for s in response['sourcesForFollowUps'] if s.get('issue_id') is not None]
                             if 'sourcesForFollowUps' in response else None),
        "evidenceReads": response.get('evidenceReads') or [],
    }


def compact_agent(review: Any) -> Optional[Dict[str, Any]]:
    """에이전트 실행 요약에서 도구 이름·호출량·토큰·종료 사유만 남긴다. 검색어·원문은 저장하지 않는다."""
    if not isinstance(review, dict):
        return None
    trace = review.get("trace") or []
    return {"stopReason": review.get("stopReason"), "finalization": review.get("finalization"),
            "retrievalOutcome": review.get("retrievalOutcome"),
            "queryResults": [{k: fact.get(k) for k in ("operation", "total_count", "observed_count", "ordered_ids",
                                                       "search_complete", "snapshot_as_of")}
                             for fact in review.get("queryResults") or []],
            "modelRequests": review.get("modelRequests"), "toolCalls": review.get("toolCalls"),
            "parallelGroups": len({entry['executionGroup'] for entry in trace if entry.get('concurrentTools', 1) > 1}),
            "maxConcurrentTools": max((entry.get('concurrentTools', 1) for entry in trace if entry.get('step') == 'tool'), default=0),
            "tools": [entry.get("tool") for entry in trace if entry.get("step") == "tool" and entry.get("tool")],
            "toolErrors": [entry.get("errorCode") for entry in trace if entry.get("step") == "tool" and entry.get("errorCode")],
            "usage": review.get("usage"), "modelIds": review.get("modelIds"),
            "aggregationResults": [{k: r.get(k) for k in ('groups', 'total_count')}
                                   for r in review.get('aggregationResults') or []]}


def build_request(case: Dict[str, Any], model_id: str, messages: List[Dict[str, str]], audit_id: Optional[str]):
    from backend.schemas import ChatMessage, ChatRequest

    tools = {"mantis": True, "notion": True, "vectorDb": True, **(case.get("tools") or {})}
    return ChatRequest(
        modelId=model_id,
        messages=[ChatMessage(role=cast(Literal['system', 'user', 'assistant'], message["role"]), content=message["content"])
                  for message in messages],
        contextAuditId=audit_id,
        temperature=0.0,
        seed=42,
        searchLimit=5,
        showSources=True,
        abTest=False,
        stream=False,
        tools=tools,
    )


def run_cases(
    cases: Iterable[Dict[str, Any]],
    chat: Callable[[Any], Dict[str, Any]],
    model_id: str,
    request_factory: Callable[..., Any] = build_request,
    progress: Callable[[str], None] = lambda message: None,
    *, observation_factory: Callable = nullcontext,
    audit_reader: Callable | None = None,
    preflight: Callable | None = None,
) -> List[Dict[str, Any]]:
    """문항마다 웹 대화처럼 이전 질문·답변과 감사 ID를 이어서 보낸다."""
    records: List[Dict[str, Any]] = []
    for case in cases:
        messages: List[Dict[str, str]] = []
        audit_id: Optional[str] = None
        for index, turn in enumerate(case["turns"]):
            messages.append({"role": "user", "content": turn["question"]})
            started = time.perf_counter()
            record: Dict[str, Any] = {"case_id": case["id"], "turn": index, "question": turn["question"]}
            observation = None
            try:
                if preflight:
                    record['freshness'] = preflight()
                started = time.perf_counter()
                with observation_factory() as observation:
                    response = chat(request_factory(case, model_id, list(messages), audit_id))
                record['latency_ms'] = round((time.perf_counter() - started) * 1000, 1)
                if audit_reader and response.get('auditId'):
                    response = {**response, 'sourcesForFollowUps': audit_reader(response['auditId'])}
                record["response"] = compact_response(response)
                if preflight:
                    record['freshness_end'] = preflight()
            except Exception as exc:  # noqa: BLE001 - 한 문항의 실패가 나머지 측정을 막지 않게 기록한다.
                record["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
                record['error_kind'] = 'cancelled' if getattr(exc, 'status_code', None) == 499 else 'error'
            record.setdefault('latency_ms', round((time.perf_counter() - started) * 1000, 1))
            if observation is not None:
                record['observation'] = observation.report()
                if record.get('response'):
                    record['response']['evidenceReads'] = record['observation']['evidence_reads']
            if preflight and 'freshness_end' not in record:
                try:
                    record['freshness_end'] = preflight()
                except Exception as exc:  # noqa: BLE001 - 환경 불량과 답변 실패를 구분해 남긴다.
                    record['environment_error'] = type(exc).__name__
            records.append(record)
            progress(f"{case['id']} {index + 1}/{len(case['turns'])} {'오류' if 'error' in record else '완료'}")
            if "error" in record:
                # 후속 질문은 앞 답변의 감사 기록에 의존하므로 같은 문항의 나머지는 실행하지 않는다.
                break
            messages.append({"role": "assistant", "content": record["response"]["content"]})
            audit_id = record["response"].get("auditId") or None
    return records


def git_commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=APP_DIR, capture_output=True, text=True,
                              timeout=10, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def command_run(args: argparse.Namespace) -> int:
    from backend.environment import load_dotenv
    load_dotenv(APP_DIR / ".env")
    from backend.settings.catalogs import initialize_customer_catalogs
    initialize_customer_catalogs()
    # 같은 문항·저장본으로 기존 경로와 에이전트 경로를 비교한다. 진입 조건은 서버 정책을 그대로 따른다.
    os.environ["AGENT_MODE_ENABLED"] = "false" if args.agent == "off" else "true"
    from backend.services.chat_service import chat_service
    from backend.sources.mantis_store import read_store_metadata

    cases_path = Path(args.cases)
    cases = [case for case in load_cases(cases_path) if not args.split or case["split"] == args.split]
    out_dir = Path(args.out) if args.out else REPORT_DIR / f"benchmark-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if (out_dir / 'manifest.json').exists() or (out_dir / 'responses.jsonl').exists():
        raise CaseError('기존 평가 결과를 덮어쓸 수 없습니다. 새 --out 폴더를 지정하세요.')
    user = {"uid": "benchmark-" + uuid.uuid4().hex}
    store = read_store_metadata(allow_stale=True) or {}
    manifest = {
        "cases_path": str(cases_path), "cases_sha256": file_sha256(cases_path), "split": args.split or "all",
        "case_count": len(cases), "model_id": args.model_id, "agent_mode": args.agent, "git_commit": git_commit(),
        "run_date": date.today().isoformat(), "started_at": datetime.now().astimezone().isoformat(),
        "mantis_store_fetched_at": store.get("fetchedAt"),
    }
    from functools import partial
    from backend import llm
    from backend.audit import load_audit_sources
    from backend.config import RUNTIME_DIR
    from backend.services.agent.policy import send_url
    from scripts.accuracy.benchmark_observation import observe_requests, validate_prices
    from scripts.accuracy.benchmark_provenance import code_version, snapshot_version, require_fresh, public_settings
    prices = validate_prices(json.loads(Path(args.prices).read_text()) if args.prices else {})
    endpoints: dict = {}
    for model in llm.get_model_registry():
        endpoints.setdefault(send_url(model), {})[model.get('model', model['id'])] = model['id']
    needed = {source for case in cases for source in ('mantis', 'notion') if (case.get('tools') or {}).get(source, True)}
    manifest.update(code_version=code_version(APP_DIR), snapshot_version=snapshot_version(RUNTIME_DIR),
                    settings=public_settings(), price_book=prices, freshness=require_fresh(needed),
                    evaluator_version=2, validity='running')
    (out_dir / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    from backend.services.agent.query_shadow import query_shadow_scope
    with query_shadow_scope() if args.agent == "query-shadow" else nullcontext():
        records = run_cases(cases, lambda request: chat_service.build_response(request, user=user), args.model_id,
                            progress=lambda message: print(message, flush=True),
                            observation_factory=partial(observe_requests, endpoints, prices),
                            audit_reader=partial(load_audit_sources, user_uid=user['uid']),
                            preflight=partial(require_fresh, needed))
    manifest['code_unchanged'] = code_version(APP_DIR) == manifest['code_version']
    manifest['mantis_unchanged'] = snapshot_version(RUNTIME_DIR).get('mantis_complete.sqlite3') == manifest['snapshot_version'].get('mantis_complete.sqlite3')
    manifest['validity'] = 'valid' if manifest['code_unchanged'] and manifest['mantis_unchanged'] and all(
        'freshness' in r and 'freshness_end' in r and 'environment_error' not in r for r in records) else 'invalid'
    manifest["finished_at"] = datetime.now().astimezone().isoformat()
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    with (out_dir / "responses.jsonl").open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"실행 결과: {out_dir}")
    return 0


# ---------------------------------------------------------------- 채점

def relative_period(spec: Dict[str, Any], run_date: date) -> Dict[str, str]:
    """제품 코드와 별개로 달력 규칙을 직접 계산한다. 주는 월요일~일요일이다."""
    kind = spec["relative"]
    if kind == "today":
        start = end = run_date
    elif kind == "yesterday":
        start = end = run_date - timedelta(days=1)
    elif kind in {"this_week", "last_week"}:
        monday = run_date - timedelta(days=run_date.weekday()) - timedelta(days=7 if kind == "last_week" else 0)
        start, end = monday, monday + timedelta(days=6)
    elif kind == "this_month":
        start = run_date.replace(day=1)
        end = run_date.replace(day=calendar.monthrange(run_date.year, run_date.month)[1])
    elif kind == "last_month":
        end = run_date.replace(day=1) - timedelta(days=1)
        start = end.replace(day=1)
    elif kind == "recent_days":
        start, end = run_date - timedelta(days=spec["n"] - 1), run_date
    else:  # recent_months: 이번 달을 포함한 n개 달력월의 첫날부터 오늘까지
        month_index = run_date.year * 12 + run_date.month - 1 - (spec["n"] - 1)
        start, end = date(month_index // 12, month_index % 12 + 1, 1), run_date
    return {"from": start.isoformat(), "to": end.isoformat()}


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _compare_set(actual: List[Any], expected: List[Any], match: str) -> Optional[str]:
    actual_set, expected_set = set(actual), set(expected)
    if match == "exact" and actual_set != expected_set:
        return f"누락 {sorted(expected_set - actual_set)} · 초과 {sorted(actual_set - expected_set)}"
    if match == "includes" and not expected_set <= actual_set:
        return f"누락 {sorted(expected_set - actual_set)}"
    if match == "excludes" and actual_set & expected_set:
        return f"포함되면 안 되는 항목 {sorted(actual_set & expected_set)}"
    return None


def _plan_constraints(plan: Dict[str, Any]) -> Dict[str, Any]:
    return plan.get("resolved_constraints") or {}


def check_plan(expect: Dict[str, Any], response: Dict[str, Any], run_date: date) -> Optional[str]:
    plan = response.get("queryPlan") or {}
    if not plan:
        return "실행 계획이 없습니다"
    constraints = _plan_constraints(plan)
    problems = []
    for key in ("source", "operation", "report_kind"):
        if key in expect and plan.get(key) != expect[key]:
            problems.append(f"{key}={plan.get(key)!r} (기대 {expect[key]!r})")
    for key in ("statuses", "excluded_statuses", "issue_ids"):
        if key in expect and sorted(constraints.get(key) or []) != sorted(expect[key]):
            problems.append(f"{key}={constraints.get(key) or []} (기대 {expect[key]})")
    if "people" in expect:
        names = (constraints.get("people") or {}).get("names") or []
        if sorted(names) != sorted(expect["people"]):
            problems.append(f"people={names} (기대 {expect['people']})")
    for key in ("customer", "platform"):
        if key in expect and constraints.get(f"{key}_key") != expect[key]:
            problems.append(f"{key}={constraints.get(f'{key}_key')!r} (기대 {expect[key]!r})")
    if "created_at" in expect:
        wanted = expect["created_at"]
        if isinstance(wanted, dict) and "relative" in wanted:
            wanted = relative_period(wanted, run_date)
        actual = constraints.get("created_at")
        actual = {"from": actual.get("from"), "to": actual.get("to")} if isinstance(actual, dict) else None
        if actual != wanted:
            problems.append(f"created_at={actual} (기대 {wanted})")
    return " · ".join(problems) or None


def check_expectation(expect: Dict[str, Any], response: Dict[str, Any], run_date: date) -> Optional[str]:
    """통과면 None, 실패면 사유를 돌려준다."""
    kind = expect["type"]
    content = response.get("content") or ""
    mode = response.get("mode") or ""
    if mode == "agent_query_shadow_skipped":
        return "shadow 비교가 실행되지 않았습니다"
    if kind in outcomes.EXPECTATIONS:
        return outcomes.check(expect, response)
    if kind == "count":
        stages = response.get("stages") or []
        totals = [stage["meta"].get("total_count") for stage in stages
                  if stage.get("status") == "ok" and (stage.get("meta") or {}).get("operation") == "count"
                  and stage["meta"].get("search_complete") is not False]
        if not totals and len(stages) == 1:
            stage, meta = stages[0], stages[0].get("meta") or {}
            if (stage.get("source") == "mantis_snapshot" and stage.get("status") == "ok"
                    and meta.get("search_complete") is True):
                if meta.get("operation") == "list":
                    totals = [meta.get("total_count")]
                elif meta.get("tool") == "mantis_complete_snapshot":
                    totals = [meta.get("found_count")]
        if expect["value"] not in totals:
            return f"집계 결과 {totals or '없음'} (기대 {expect['value']})"
        numeric_text = content.replace(",", "")
        explicit = re.findall(r"(?:총|전체)\s*(\d+)\s*건", numeric_text)
        if explicit:
            displayed = {int(n) for n in explicit} == {expect["value"]}
        else:
            displayed = bool(re.search(rf'(?<!\d){expect["value"]}\s*건', numeric_text))
            if expect["value"] == 0 and not re.search(r"\d+\s*건", numeric_text):
                displayed = "지금 조건으로는 찾지 못했어요" in content or "일치하는 이슈가 없습니다" in content
        if not displayed:
            return f"답변 본문에 {expect['value']}건이 표시되지 않았습니다"
        return None
    if kind == "issue_ids":
        ids = [int(source["issue_id"]) for source in response.get("sources") or []
               if source.get("issue_id") not in (None, "")]
        if expect.get("ordered") and ids != expect["ids"]:
            return f"표시한 이슈 순서 {ids} (기대 {expect['ids']})"
        reason = _compare_set(ids, expect["ids"], expect.get("match", "exact"))
        return f"출처 이슈 {reason}" if reason else None
    if kind == "notion_titles":
        titles = [_normalize_text(source.get("title")) for source in response.get("sources") or []
                  if source.get("type") == "notion"]
        reason = _compare_set(titles, [_normalize_text(title) for title in expect["titles"]], expect.get("match", "includes"))
        return f"Notion 문서 {reason}" if reason else None
    if kind == "contains":
        text = _normalize_text(content)
        missing = [group if isinstance(group, str) else group[0] for group in expect["all"]
                   if not any(_normalize_text(option) in text for option in ([group] if isinstance(group, str) else group))]
        return f"필수 내용 누락: {missing}" if missing else None
    if kind == "not_contains":
        text = _normalize_text(content)
        found = [phrase for phrase in expect["any"] if _normalize_text(phrase) in text]
        return f"금지된 서술 포함: {found}" if found else None
    if kind == "mode":
        return None if mode in expect["in"] else f"답변 방식 {mode!r} (기대 {expect['in']})"
    if kind == "clarify":
        return None if outcomes.response_outcome(response) == 'clarify' else f"확인 질문 대신 {mode!r}로 답했습니다"
    if kind == "delivered":
        outcome = outcomes.response_outcome(response)
        if outcome == 'blocked':
            return "검증 단계에서 답변이 차단됐습니다"
        if outcome == 'clarify':
            return '확인 질문으로 끝났습니다'
        return None if outcome in {'answered', 'partial'} else f'답변을 완료하지 못했습니다: {outcome}'
    if kind == "agent":
        return check_agent(expect, response.get("agent"))
    return check_plan(expect, response, run_date)


def check_agent(expect: Dict[str, Any], agent: Optional[Dict[str, Any]]) -> Optional[str]:
    if "entered" in expect and bool(agent) != expect["entered"]:
        return "에이전트 경로에 진입하지 않았습니다" if expect["entered"] else "에이전트 경로에 진입했습니다"
    if not agent:
        return None if expect.get("entered") is False else "에이전트 실행 기록이 없습니다"
    tools = set(agent.get("tools") or [])
    missing = [name for name in expect.get("tools_include", []) if name not in tools]
    if missing:
        return f"필요한 도구를 호출하지 않았습니다: {missing}"
    extra = [name for name in expect.get("tools_exclude", []) if name in tools]
    if extra:
        return f"불필요한 도구를 호출했습니다: {extra}"
    if "max_tool_calls" in expect and (agent.get("toolCalls") or 0) > expect["max_tool_calls"]:
        return f"도구 호출 {agent.get('toolCalls')}회 (기대 최대 {expect['max_tool_calls']}회)"
    if "finalization" in expect and agent.get("finalization") not in expect["finalization"]:
        return f"종료 방식 {agent.get('finalization')!r} (기대 {expect['finalization']})"
    return None


def score_cases(cases: List[Dict[str, Any]], records: List[Dict[str, Any]], run_date: date) -> Dict[str, Any]:
    by_turn = {(record["case_id"], record["turn"]): record for record in records}
    results = []
    for case in cases:
        turns = []
        for index, turn in enumerate(case["turns"]):
            record = by_turn.get((case["id"], index))
            answer = (record or {}).get('response') or {}
            outcome = outcomes.response_outcome(answer)
            if record is None:
                failures = ["응답이 없습니다(실행되지 않음)"]
                outcome = 'not_run'
            elif "error" in record:
                failures = [f"실행 오류: {record['error']}"]
                outcome = record.get('error_kind', 'error')
            else:
                failures = [reason for expect in turn["expect"]
                            if (reason := check_expectation(expect, record["response"], run_date))]
            requirements = []
            for item in turn.get('requirements', []):
                reasons = ([f'요청 항목을 실행하지 못했습니다: {outcome}'] if outcome in {'error', 'not_run'} else
                           [reason for e in item['expect'] if (reason := check_expectation(e, answer, run_date))])
                if outcome in {'blocked', 'cancelled', 'clarify', 'incomplete'}:
                    reasons.append(f'요청 항목 미완료: {outcome}')
                status = 'pass' if not reasons else 'unassessed' if all(r.startswith('관련성 미확인:') for r in reasons) else 'fail'
                diagnostics = [outcomes.assess_relevance(e, answer) for e in item['expect'] if e['type'] == 'evidence_relevance']
                requirements.append({'id': item['id'], 'label': item['label'], 'status': status, 'failures': reasons,
                                     **({'evidence_diagnostics': diagnostics} if diagnostics else {})})
                failures.extend(f'{item["label"]}: {r}' for r in reasons)
            if requirements and outcome in {'answered', 'partial'}:
                outcome = 'complete' if not failures else 'partial'
            turns.append({"question": turn["question"], "passed": not failures, "failures": failures,
                          'requirements': requirements, 'outcome': outcome,
                          'category': 'followup' if index else case['category'],
                          'observation': (record or {}).get('observation'),
                          "latency_ms": (record or {}).get("latency_ms"),
                          "mode": ((record or {}).get("response") or {}).get("mode"),
                          "agent": ((record or {}).get("response") or {}).get("agent")})
        results.append({"id": case["id"], "split": case["split"], "category": case["category"],
                        "passed": all(turn["passed"] for turn in turns), "turns": turns})
    return {"run_date": run_date.isoformat(), "results": results, "summary": summarize(results)}


def nearest_rank(values: List[float], percentile: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile / 100 * len(ordered)) - 1)]


def summarize(results: List[Dict[str, Any]]) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for split in sorted({result["split"] for result in results}):
        selected = [result for result in results if result["split"] == split]
        latencies = [turn["latency_ms"] for result in selected for turn in result["turns"] if turn["latency_ms"] is not None]
        turn_outcomes = Counter(turn['outcome'] for result in selected for turn in result['turns'])
        turn_categories = {turn['category'] for result in selected for turn in result['turns']}
        categories = {}
        for category in sorted({result["category"] for result in selected}):
            items = [result for result in selected if result["category"] == category]
            categories[category] = {"passed": sum(result["passed"] for result in items), "total": len(items)}
        passed = sum(result["passed"] for result in selected)
        turn_count = sum(len(result["turns"]) for result in selected)
        summary[split] = {
            "passed": passed, "total": len(selected),
            "pass_rate": round(passed / len(selected), 4) if selected else None,
            "categories": categories,
            "latency_ms": {"mean": round(statistics.mean(latencies), 1) if latencies else None,
                           "median": round(statistics.median(latencies), 1) if latencies else None,
                           "p95": nearest_rank(latencies, 95)},
            # 되묻기·차단 비율은 정확도와 별개로 사용자가 체감하는 품질 지표다.
            "clarify_rate": round(turn_outcomes['clarify'] / turn_count, 4) if turn_count else None,
            "blocked_rate": round(turn_outcomes['blocked'] / turn_count, 4) if turn_count else None,
            "agent": summarize_agent([turn for result in selected for turn in result["turns"]]),
            'outcomes': dict(Counter(t['outcome'] for r in selected for t in r['turns'])),
            'requirements': outcomes.summarize_requirements([t for r in selected for t in r['turns']]),
            'measurement': summarize_measurements([t for r in selected for t in r['turns']]),
            'latency_by_category': {category: latency_summary([t['latency_ms'] for r in selected
                for t in r['turns'] if t['category'] == category and t['latency_ms'] is not None]) for category in turn_categories},
            'measurement_by_category': {category: summarize_measurements([t for r in selected
                for t in r['turns'] if t['category'] == category]) for category in turn_categories},
        }
    return summary


def latency_summary(values: list[float]) -> dict:
    return {'samples': len(values), 'mean': round(statistics.mean(values), 1) if values else None,
            'median': statistics.median(values) if values else None, 'p95': nearest_rank(values, 95)}


def summarize_agent(turns: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """에이전트 진입 턴의 호출량·토큰·종료 사유. 제공되지 않은 토큰은 미확인 호출 수로 따로 센다."""
    runs = [turn["agent"] for turn in turns if turn.get("agent")]
    if not runs:
        return None
    usage = [run.get("usage") or {} for run in runs]
    return {
        "entered_turns": len(runs), "entered_rate": round(len(runs) / len(turns), 4),
        "tool_calls_mean": round(statistics.mean(run.get("toolCalls") or 0 for run in runs), 2),
        "model_requests_mean": round(statistics.mean(run.get("modelRequests") or 0 for run in runs), 2),
        "prompt_tokens": sum(item.get("promptTokens") or 0 for item in usage),
        "completion_tokens": sum(item.get("completionTokens") or 0 for item in usage),
        "unreported_token_calls": sum(item.get("unreportedCalls") or 0 for item in usage),
        "stop_reasons": dict(Counter(run.get("stopReason") for run in runs)),
        "finalizations": dict(Counter(run.get("finalization") for run in runs)),
        "tool_counts": dict(Counter(tool for run in runs for tool in run.get("tools") or [])),
    }


def render_markdown(report: Dict[str, Any], manifest: Dict[str, Any]) -> str:
    lines = ["# 고정 문항 정확도 평가", "",
             f"- 문항 파일 SHA-256: `{manifest.get('cases_sha256', '')}`",
             f"- 모델: `{manifest.get('model_id', '')}` · 코드: `{manifest.get('git_commit', '')[:12]}`"
             f" · 에이전트 모드: {manifest.get('agent_mode', 'off')}",
             f"- 실행일: {report['run_date']} · Mantis 저장본 수집 시각: {manifest.get('mantis_store_fetched_at') or '미확인'}",
             "- 최종 점수는 수정에 사용하지 않은 `final` 문항만으로 계산한다.", ""]
    for split, item in report["summary"].items():
        latency = item["latency_ms"]
        lines += [f"## {split} · {item['passed']}/{item['total']}", "",
                  "| 분류 | 통과 |", "|---|---:|",
                  *[f"| {name} | {value['passed']}/{value['total']} |" for name, value in item["categories"].items()],
                  "", f"응답시간 평균 {latency['mean']}ms · 중앙값 {latency['median']}ms · p95 {latency['p95']}ms · "
                      f"확인 질문 비율 {item['clarify_rate']} · 차단 비율 {item['blocked_rate']}", ""]
        agent = item.get("agent")
        if agent:
            lines += [f"에이전트 진입 {agent['entered_turns']}턴({agent['entered_rate']}) · 평균 도구 호출 {agent['tool_calls_mean']}회 · "
                      f"평균 모델 호출 {agent['model_requests_mean']}회 · 토큰 입력 {agent['prompt_tokens']}/출력 "
                      f"{agent['completion_tokens']}(미보고 호출 {agent['unreported_token_calls']}) · 종료 사유 {agent['stop_reasons']}", ""]
        requirements = item.get('requirements') or {}
        if requirements.get('total'):
            lines += [f"요청 항목 통과 {requirements['passed']}/{requirements['total']} · 관련성 미확인 {requirements['unassessed']} · "
                      f"요청 완수 {requirements['complete_turns']}/{requirements['evaluated_turns']}턴", '',
                      '| 요청 항목 | 통과 | 미확인 |', '|---|---:|---:|',
                      *[f"| {v['label']} | {v['passed']}/{v['total']} | {v['unassessed']} |"
                        for v in requirements['by_id'].values()], '']
        measured = item.get('measurement') or {}
        if measured.get('observed_turns'):
            price = (f"{measured['currency']} {measured['cost_total']:.6f}" if measured['cost_total'] is not None else '미산정')
            lines += [f"실제 모델 HTTP 시도 {measured['model_attempts']}회 · 원천 HTTP {measured['source_http_requests']}회 · "
                      f"사용량 미보고 {measured['unreported_usage_calls']}회 · 단가 미등록 {measured['unpriced_calls']}회 · 금액 {price}",
                      '재시도·대체 모델·최종 검토를 포함하며, 미보고 사용량이나 단가 누락은 비용 0으로 간주하지 않습니다.', '']
            if measured.get('cost_per_request') is not None:
                lines += [f"요청당 비용 평균 {measured['currency']} {measured['cost_per_request']:.6f} · "
                          f"p95 {measured['currency']} {measured['cost_p95']:.6f}", '']
            if measured.get('cost_unresolved_calls'):
                lines += [f"비용 미확인 사유별 호출: {measured['cost_unresolved_calls']}", '']
        lines += ['| 질문 분류 | 지연 표본 | 평균(ms) | p95(ms) |', '|---|---:|---:|---:|',
                  *[f"| {k} | {v['samples']} | {v['mean']} | {v['p95']} |" for k, v in item.get('latency_by_category', {}).items()],
                  '소수 표본의 p95는 관찰값이며 운영 성능 보증으로 사용하지 않습니다.', '']
    failed = [result for result in report["results"] if not result["passed"]]
    if failed:
        lines += ["## 실패 문항", ""]
        for result in failed:
            for index, turn in enumerate(result["turns"], 1):
                if not turn["passed"]:
                    lines.append(f"- `{result['id']}` ({result['split']}·{result['category']}) {index}번째: "
                                 + " / ".join(turn["failures"]))
    return "\n".join(lines) + "\n"


def command_score(args: argparse.Namespace) -> int:
    cases_path, run_dir = Path(args.cases), Path(args.run_dir)
    cases = load_cases(cases_path)
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("cases_sha256") != file_sha256(cases_path):
        raise CaseError('실행 당시와 문항 파일이 다릅니다. 고정한 문항 파일로 채점하세요.')
    if manifest.get('validity') in {'invalid', 'running'}:
        raise CaseError('코드·설정·저장본 검사 또는 실행 완료를 확인하지 못한 결과는 정상 기준선으로 채점할 수 없습니다.')
    if manifest.get("split") in SPLITS:
        cases = [case for case in cases if case["split"] == manifest["split"]]
    records = [json.loads(line) for line in (run_dir / "responses.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    report = score_cases(cases, records, date.fromisoformat(manifest["run_date"]))
    (run_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown = render_markdown(report, manifest)
    (run_dir / "report.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    final = report["summary"].get("final")
    if args.fail_under is not None and (final is None or (final["pass_rate"] or 0) < args.fail_under):
        return 1
    return 0


def command_validate(args: argparse.Namespace) -> int:
    cases = load_cases(Path(args.cases))
    counts = Counter((case["split"], case["category"]) for case in cases)
    print(f"문항 {len(cases)}개 형식 확인 완료")
    for (split, category), count in sorted(counts.items()):
        print(f"  {split} · {category}: {count}")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="고정 문항 정확도 평가")
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="문항 파일 형식만 확인한다")
    validate.add_argument("cases")
    run = commands.add_parser("run", help="실제 서비스로 답변을 받아 저장한다(.env 필요)")
    run.add_argument("cases")
    run.add_argument("--model-id", required=True)
    run.add_argument("--agent", choices=["off", "on", "query-shadow"], default="off",
                     help="에이전트 경로 기능 플래그. 진입은 허용 모델·검증 기록 정책을 그대로 따른다")
    run.add_argument("--split", choices=sorted(SPLITS))
    run.add_argument("--out")
    run.add_argument('--prices', help='통화·기준일·출처를 포함한 모델별 100만 토큰 단가 JSON. 없으면 금액은 미산정')
    score = commands.add_parser("score", help="저장된 답변을 채점한다(모델·데이터 접근 불필요)")
    score.add_argument("cases")
    score.add_argument("run_dir")
    score.add_argument("--fail-under", type=float, help="final 통과율이 이 값보다 낮으면 종료 코드 1")
    args = parser.parse_args(argv)
    try:
        return {"validate": command_validate, "run": command_run, "score": command_score}[args.command](args)
    except CaseError as exc:
        print(f"문항 파일 오류: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"파일을 찾을 수 없습니다: {exc.filename}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
