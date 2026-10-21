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
import re
import statistics
import subprocess
import sys
import time
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent.parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from scripts.accuracy._runtime import reexec_in_project_venv

if __name__ == "__main__":
    reexec_in_project_venv(APP_DIR)

REPORT_DIR = APP_DIR / "runtime" / "accuracy_reports"
SPLITS = {"dev", "final"}
CATEGORIES = {"count", "followup", "list", "explain", "notion", "date", "similar", "report", "clarify", "general"}
ID_MATCHES = {"exact", "includes", "excludes"}
RELATIVE_PERIODS = {"today", "yesterday", "this_week", "last_week", "this_month", "last_month", "recent_days", "recent_months"}
PLAN_KEYS = {"source", "operation", "report_kind", "statuses", "excluded_statuses", "people",
             "customer", "platform", "issue_ids", "created_at"}
CLARIFY_MODES = {"unified_plan_clarification"}
BLOCKED_MODE = "verification_blocked"


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
    if kind == "count":
        _require(type(expect.get("value")) is int and expect["value"] >= 0, f"{where}: count.value는 0 이상의 정수입니다")
    elif kind == "issue_ids":
        _require(_is_int_list(expect.get("ids")), f"{where}: issue_ids.ids는 정수 목록입니다")
        _require(expect.get("match", "exact") in ID_MATCHES, f"{where}: issue_ids.match는 {sorted(ID_MATCHES)} 중 하나입니다")
    elif kind == "notion_titles":
        _require(_is_text_list(expect.get("titles")), f"{where}: notion_titles.titles는 문자열 목록입니다")
        _require(expect.get("match", "includes") in ID_MATCHES, f"{where}: notion_titles.match는 {sorted(ID_MATCHES)} 중 하나입니다")
    elif kind == "contains":
        groups = expect.get("all")
        _require(isinstance(groups, list) and groups and all(
            (isinstance(group, str) and group) or _is_text_list(group) and group for group in groups),
            f"{where}: contains.all은 문자열 또는 동의어 목록의 목록입니다")
    elif kind == "not_contains":
        _require(_is_text_list(expect.get("any")) and expect["any"], f"{where}: not_contains.any는 문자열 목록입니다")
    elif kind == "mode":
        _require(_is_text_list(expect.get("in")) and expect["in"], f"{where}: mode.in은 문자열 목록입니다")
    elif kind in {"clarify", "delivered"}:
        pass
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
            for key in ("operation", "total_count", "search_complete", "fetchedAt", "tool")
        }} for stage in retrieval.get("stages") or []],
        "verification": {"status": verification.get("status"), "delivery": verification.get("delivery")},
        "timings": response.get("timings"),
    }


def build_request(case: Dict[str, Any], model_id: str, messages: List[Dict[str, str]], audit_id: Optional[str]):
    from backend.schemas import ChatMessage, ChatRequest

    tools = {"mantis": True, "notion": True, "vectorDb": True, **(case.get("tools") or {})}
    return ChatRequest(
        modelId=model_id,
        messages=[ChatMessage(role=message["role"], content=message["content"]) for message in messages],
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
            try:
                response = chat(request_factory(case, model_id, list(messages), audit_id))
                record["response"] = compact_response(response)
            except Exception as exc:  # noqa: BLE001 - 한 문항의 실패가 나머지 측정을 막지 않게 기록한다.
                record["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
            record["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
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
    from backend.services.chat_service import chat_service
    from backend.sources.mantis_store import read_store_metadata

    cases_path = Path(args.cases)
    cases = [case for case in load_cases(cases_path) if not args.split or case["split"] == args.split]
    out_dir = Path(args.out) if args.out else REPORT_DIR / f"benchmark-{datetime.now():%Y%m%d-%H%M%S}"
    out_dir.mkdir(parents=True, exist_ok=True)
    user = {"uid": "benchmark-" + uuid.uuid4().hex}
    store = read_store_metadata(allow_stale=True) or {}
    manifest = {
        "cases_path": str(cases_path), "cases_sha256": file_sha256(cases_path), "split": args.split or "all",
        "case_count": len(cases), "model_id": args.model_id, "git_commit": git_commit(),
        "run_date": date.today().isoformat(), "started_at": datetime.now().astimezone().isoformat(),
        "mantis_store_fetched_at": store.get("fetchedAt"),
    }
    records = run_cases(cases, lambda request: chat_service.build_response(request, user=user), args.model_id,
                        progress=lambda message: print(message, flush=True))
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
    if kind == "count":
        totals = [stage["meta"].get("total_count") for stage in response.get("stages") or []
                  if (stage.get("meta") or {}).get("operation") == "count"]
        if expect["value"] not in totals:
            return f"집계 결과 {totals or '없음'} (기대 {expect['value']})"
        if f"{expect['value']}건" not in content.replace(",", ""):
            return f"답변 본문에 {expect['value']}건이 표시되지 않았습니다"
        return None
    if kind == "issue_ids":
        ids = [int(source["issue_id"]) for source in response.get("sources") or []
               if source.get("issue_id") not in (None, "")]
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
        return None if mode in CLARIFY_MODES else f"확인 질문 대신 {mode!r}로 답했습니다"
    if kind == "delivered":
        if mode == BLOCKED_MODE:
            return "검증 단계에서 답변이 차단됐습니다"
        return "확인 질문으로 끝났습니다" if mode in CLARIFY_MODES else None
    return check_plan(expect, response, run_date)


def score_cases(cases: List[Dict[str, Any]], records: List[Dict[str, Any]], run_date: date) -> Dict[str, Any]:
    by_turn = {(record["case_id"], record["turn"]): record for record in records}
    results = []
    for case in cases:
        turns = []
        for index, turn in enumerate(case["turns"]):
            record = by_turn.get((case["id"], index))
            if record is None:
                failures = ["응답이 없습니다(실행되지 않음)"]
            elif "error" in record:
                failures = [f"실행 오류: {record['error']}"]
            else:
                failures = [reason for expect in turn["expect"]
                            if (reason := check_expectation(expect, record["response"], run_date))]
            turns.append({"question": turn["question"], "passed": not failures, "failures": failures,
                          "latency_ms": (record or {}).get("latency_ms"),
                          "mode": ((record or {}).get("response") or {}).get("mode")})
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
        modes = Counter(turn["mode"] for result in selected for turn in result["turns"] if turn["mode"])
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
            "clarify_rate": round(sum(modes[mode] for mode in CLARIFY_MODES) / turn_count, 4) if turn_count else None,
            "blocked_rate": round(modes[BLOCKED_MODE] / turn_count, 4) if turn_count else None,
        }
    return summary


def render_markdown(report: Dict[str, Any], manifest: Dict[str, Any]) -> str:
    lines = ["# 고정 문항 정확도 평가", "",
             f"- 문항 파일 SHA-256: `{manifest.get('cases_sha256', '')}`",
             f"- 모델: `{manifest.get('model_id', '')}` · 코드: `{manifest.get('git_commit', '')[:12]}`",
             f"- 실행일: {report['run_date']} · Mantis 저장본 수집 시각: {manifest.get('mantis_store_fetched_at') or '미확인'}",
             "- 최종 점수는 수정에 사용하지 않은 `final` 문항만으로 계산한다.", ""]
    for split, item in report["summary"].items():
        latency = item["latency_ms"]
        lines += [f"## {split} · {item['passed']}/{item['total']}", "",
                  "| 분류 | 통과 |", "|---|---:|",
                  *[f"| {name} | {value['passed']}/{value['total']} |" for name, value in item["categories"].items()],
                  "", f"응답시간 평균 {latency['mean']}ms · 중앙값 {latency['median']}ms · p95 {latency['p95']}ms · "
                      f"확인 질문 비율 {item['clarify_rate']} · 차단 비율 {item['blocked_rate']}", ""]
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
        print("경고: 실행 당시와 문항 파일이 다릅니다. 같은 문항으로 다시 실행해야 점수를 비교할 수 있습니다.", file=sys.stderr)
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
    run.add_argument("--split", choices=sorted(SPLITS))
    run.add_argument("--out")
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
