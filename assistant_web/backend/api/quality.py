"""감사 로그, 회귀 평가, 동적 정확도 API."""

import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.audit import init_audit_db
from backend.config import APP_DIR, AUDIT_DB_PATH, RUNTIME_DIR
from backend.reports.evaluation import (
    EVALUATION_CASES,
    build_accuracy_analytics,
    build_evaluation_summary,
    run_evaluation_case_safely,
    save_evaluation_run,
)
from backend.schemas import AccuracyRunRequest, EvaluationRunRequest
from backend.sources.mantis import get_mantis_snapshot_freshness_status


router = APIRouter()


@router.get("/api/validation/logs")
def list_validation_logs(limit: int = 20) -> Dict[str, Any]:
    init_audit_db()
    safe_limit = max(1, min(limit, 100))
    with sqlite3.connect(AUDIT_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, created_at, question, model_id, mode, score, status FROM validation_logs ORDER BY created_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
    return {"logs": [dict(row) for row in rows]}


@router.get("/api/validation/summary")
def get_validation_summary(limit: int = 500) -> Dict[str, Any]:
    """최근 감사 로그를 집계해 통과율/모드별 분포/자주 실패하는 검증 항목을 보여준다.

    개별 로그를 하나씩 눈으로 훑는 대신, 실사용 품질을 한눈에 파악하기 위한
    운영 대시보드용 엔드포인트다."""
    init_audit_db()
    safe_limit = max(1, min(limit, 2000))
    with sqlite3.connect(AUDIT_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, created_at, question, model_id, mode, score, status, payload "
            "FROM validation_logs ORDER BY created_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()

    total = len(rows)
    status_counts: Dict[str, int] = {"pass": 0, "warning": 0, "fail": 0}
    mode_counts: Dict[str, int] = {}
    model_counts: Dict[str, int] = {}
    check_fail_counts: Dict[str, int] = {}
    check_labels: Dict[str, str] = {}
    score_sum = 0
    recent_issues: List[Dict[str, Any]] = []

    for row in rows:
        status = row["status"] or "warning"
        status_counts[status] = status_counts.get(status, 0) + 1
        mode = row["mode"] or "model"
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        model_id = row["model_id"] or "unknown"
        model_counts[model_id] = model_counts.get(model_id, 0) + 1
        score_sum += row["score"] or 0

        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            payload = {}
        verification = payload.get("verification") or {}
        for check in verification.get("checks", []):
            if check.get("status") == "fail":
                check_id = str(check.get("id", "unknown"))
                check_fail_counts[check_id] = check_fail_counts.get(check_id, 0) + 1
                check_labels[check_id] = check.get("label", check_id)

        if status in {"fail", "warning"} and len(recent_issues) < 20:
            recent_issues.append(
                {
                    "id": row["id"],
                    "createdAt": row["created_at"],
                    "question": row["question"],
                    "modelId": model_id,
                    "mode": mode,
                    "score": row["score"],
                    "status": status,
                }
            )

    top_failing_checks = sorted(
        (
            {"id": check_id, "label": check_labels.get(check_id, check_id), "count": count}
            for check_id, count in check_fail_counts.items()
        ),
        key=lambda item: item["count"],
        reverse=True,
    )[:10]

    return {
        "windowSize": safe_limit,
        "total": total,
        "averageScore": round(score_sum / total, 1) if total else 0,
        "statusCounts": status_counts,
        "modeCounts": dict(sorted(mode_counts.items(), key=lambda item: item[1], reverse=True)),
        "modelCounts": dict(sorted(model_counts.items(), key=lambda item: item[1], reverse=True)),
        "topFailingChecks": top_failing_checks,
        "recentIssues": recent_issues,
    }


@router.get("/api/validation/logs/{log_id}")
def read_validation_log(log_id: str) -> Dict[str, Any]:
    init_audit_db()
    with sqlite3.connect(AUDIT_DB_PATH) as connection:
        row = connection.execute(
            "SELECT payload FROM validation_logs WHERE id = ?",
            (log_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="검증 로그를 찾지 못했습니다.")
    return json.loads(row[0])


@router.get("/api/evaluation/cases")
def list_evaluation_cases() -> Dict[str, Any]:
    return {"cases": EVALUATION_CASES}


def stream_evaluation_run(selected: List[Dict[str, Any]]):
    # 케이스가 순차 실행되는 사이사이에만 이벤트가 발생하므로, 채팅 스트림처럼
    # 워커 스레드/큐를 쓰지 않고 단순 제너레이터로 케이스마다 프레임을 내보낸다.
    def frame(event: Dict[str, Any]) -> str:
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    run_started = time.perf_counter()
    yield frame({"type": "run_start", "total": len(selected), "caseIds": [case["id"] for case in selected]})
    results: List[Dict[str, Any]] = []
    for index, case in enumerate(selected, start=1):
        yield frame({"type": "case_start", "caseId": case["id"], "index": index, "total": len(selected)})
        result = run_evaluation_case_safely(case)
        results.append(result)
        yield frame({"type": "case_result", "result": result})
    summary = build_evaluation_summary(results, round((time.perf_counter() - run_started) * 1000))
    run_id = save_evaluation_run(summary, results)
    yield frame({"type": "summary", "summary": {**summary, "runId": run_id}})


@router.post("/api/evaluation/run")
def run_evaluation(request: EvaluationRunRequest):
    selected_ids = set(request.caseIds)
    selected = [case for case in EVALUATION_CASES if not selected_ids or case["id"] in selected_ids]
    if request.stream:
        return StreamingResponse(
            stream_evaluation_run(selected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    run_started = time.perf_counter()
    results = [run_evaluation_case_safely(case) for case in selected]
    summary = build_evaluation_summary(results, round((time.perf_counter() - run_started) * 1000))
    run_id = save_evaluation_run(summary, results)
    return {"summary": {**summary, "runId": run_id}, "results": results}


ACCURACY_SCRIPT_PATH = APP_DIR / "scripts" / "accuracy" / "dynamic_accuracy_check.py"
ACCURACY_REPORT_DIR = RUNTIME_DIR / "accuracy_reports"
ACCURACY_REPORT_NAME_PATTERN = re.compile(r"accuracy-\d{8}-\d{6}\.json")
# 정확도 검증은 실시간 스냅샷 수집과 실제 챗 응답을 포함해 수 분이 걸리는 무거운
# 작업이라 동시에 하나만 실행한다.
ACCURACY_RUN_LOCK = threading.Lock()


def stream_accuracy_run(questions: int, follow_up_scenarios: int):
    def frame(event: Dict[str, Any]) -> str:
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    if not ACCURACY_RUN_LOCK.acquire(blocking=False):
        yield frame({"type": "error", "message": "이미 실행 중인 정확도 검증이 있습니다. 잠시 후 다시 시도해 주세요."})
        return
    try:
        # 서브프로세스로 실행해 스크립트의 os.environ 변경과 예외가 서버에 영향을
        # 주지 않게 하고, stdout의 PROGRESS 라인을 SSE로 중계한다.
        process = subprocess.Popen(
            [
                sys.executable,
                str(ACCURACY_SCRIPT_PATH),
                "--questions",
                str(questions),
                "--follow-up-scenarios",
                str(follow_up_scenarios),
                "--progress",
            ],
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert process.stdout is not None
        for line in process.stdout:
            line = line.strip()
            if not line.startswith("PROGRESS "):
                continue
            try:
                yield frame(json.loads(line[len("PROGRESS "):]))
            except ValueError:
                continue
        exit_code = process.wait()
        yield frame({"type": "done", "exitCode": exit_code})
    finally:
        ACCURACY_RUN_LOCK.release()


@router.post("/api/accuracy/run")
def run_accuracy(request: AccuracyRunRequest):
    questions = max(1, min(request.questions, 10))
    follow_up_scenarios = max(0, min(request.followUpScenarios, 4))
    return StreamingResponse(
        stream_accuracy_run(questions, follow_up_scenarios),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/data/freshness")
def read_data_freshness() -> Dict[str, Any]:
    return {"mantis": get_mantis_snapshot_freshness_status()}


@router.get("/api/accuracy/reports")
def list_accuracy_reports(limit: int = 10) -> Dict[str, Any]:
    safe_limit = max(1, min(limit, 50))
    reports: List[Dict[str, Any]] = []
    report_data: List[Dict[str, Any]] = []
    if ACCURACY_REPORT_DIR.exists():
        for path in sorted(ACCURACY_REPORT_DIR.glob("accuracy-*.json"), reverse=True)[:safe_limit]:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            report_data.append(data)
            reports.append(
                {
                    "name": path.name,
                    "runAt": data.get("run_at"),
                    "seed": data.get("seed"),
                    "modelId": data.get("model_id"),
                    "summary": data.get("summary", {}),
                    "errors": data.get("errors", []),
                }
            )
    return {
        "reports": reports,
        "analytics": build_accuracy_analytics(report_data),
        "freshness": {"mantis": get_mantis_snapshot_freshness_status()},
    }


@router.get("/api/accuracy/reports/{name}")
def read_accuracy_report(name: str) -> Dict[str, Any]:
    if not ACCURACY_REPORT_NAME_PATTERN.fullmatch(name):
        raise HTTPException(status_code=400, detail="잘못된 리포트 이름입니다.")
    path = ACCURACY_REPORT_DIR / name
    if not path.exists():
        raise HTTPException(status_code=404, detail="리포트를 찾을 수 없습니다.")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise HTTPException(status_code=500, detail=f"리포트를 읽지 못했습니다: {exc}")


@router.get("/api/evaluation/history")
def list_evaluation_history(limit: int = 10) -> Dict[str, Any]:
    init_audit_db()
    safe_limit = max(1, min(limit, 50))
    with sqlite3.connect(AUDIT_DB_PATH) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT id, run_at, total, passed, failed, score, duration_ms FROM evaluation_runs "
            "ORDER BY run_at DESC LIMIT ?",
            (safe_limit,),
        ).fetchall()
    return {"runs": [dict(row) for row in rows]}
