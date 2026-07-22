import json
import os
import queue
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import calendar
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from backend.query_planning import (
    build_query_plan,
    extract_created_at_range,
    extract_group_by,
    infer_operation,
    plan_created_at_matches,
    validate_query_plan,
)
from backend.tools.mantis_snapshot import scan_mantis_documents

# 경로 상수, 별칭/상태 테이블, Mantis·Notion 연결 설정은 config.py로 분리했다.
# `import *`로 기존 이름을 그대로 재노출해 `from backend.app import ...`(테스트 포함)
# 하위 호환을 유지한다.
from backend.config import *  # noqa: F401,F403
from backend.config import (  # 명시적 재노출(정적 분석/가독성용)
    APP_DIR,
    AUDIT_DB_PATH,
    BASE_DIR,
    CHROMA_DB_PATH,
    CHROMA_PERSIST_DIR,
    CONFIG_DIR,
    NOTION_API_BASE,
    NOTION_DEFAULT_VERSION,
    RUNTIME_DIR,
    STATIC_DIR,
    WORKSPACE_DIR,
)

# 순수 헬퍼(문자열/숫자/날짜 정규화, 인물명 검증 등)는 common.py로 분리했다.
from backend.common import *  # noqa: F401,F403

# 감사 로그 DB + 스트리밍 컨텍스트(CHAT_STREAM_CONTEXT, emit_chat_*)는 audit.py로 분리했다.
from backend.audit import *  # noqa: F401,F403

# FastAPI 요청 pydantic 모델은 schemas.py, 모델 레지스트리/호출은 llm.py로 분리했다.
from backend.schemas import *  # noqa: F401,F403
from backend.llm import *  # noqa: F401,F403

# 질의 이해 계층(제약조건 추출/매칭, 검색 의도 판단)은 understanding.py로 분리했다.
from backend.understanding import *  # noqa: F401,F403

# Mantis 소스 어댑터(API 호출·정규화·포맷·키워드 스코어링)는 sources/mantis.py로 분리했다.
# 검색 오케스트레이션(execute_mantis_*)·집계 리포트는 아직 app.py에 남아 있다.
from backend.sources.mantis import *  # noqa: F401,F403

# Notion 소스 어댑터(조회/파싱/검색/쓰기)는 sources/notion.py로 분리했다. 라우트는 app 잔류.
from backend.sources.notion import *  # noqa: F401,F403

# Chroma 벡터검색 소스 어댑터(의미검색·랭킹·문서 조회)는 sources/chroma.py로 분리했다.
from backend.sources.chroma import *  # noqa: F401,F403

# 공수 집계 리포트(인물별/랭킹)는 reports/effort.py로 분리했다.
from backend.reports.effort import *  # noqa: F401,F403

# 검색 파이프라인은 retrieval.py, 결정론적 답변/집계 리포트는 answers.py로 분리했다.
from backend.retrieval import *  # noqa: F401,F403
from backend.answers import *  # noqa: F401,F403

# 검증·평가 리포트(근거 검증, A/B, 회귀 케이스)는 reports/evaluation.py로 분리했다.
from backend.reports.evaluation import *  # noqa: F401,F403


def load_dotenv() -> None:
    env_path = BASE_DIR.parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()


app = FastAPI(title="컨텐츠서비스 AI 비서", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:8787", "http://127.0.0.1:8787"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)



# 직전 대화를 참조하는 후속 질문을 가리키는 표현. 두 단계로 나눈다.
# - STRONG: 대명사/서수처럼 그 자체로 "이전 턴을 가리킨다"는 의도가 명확한 표현.
#   문장 길이와 무관하게 후속 질문으로 판단한다.
# - WEAK: "담당자", "상태"처럼 단독으로는 새로운 일반 질문에도 흔히 나오는 표현.
#   문장이 짧을 때(=군더더기 없이 바로 되묻는 전형적인 후속 질문 패턴)만 인정해
#   "이 시스템 왜 이렇게 느려?" 같은 새 주제의 일반 질문이 이전 고객사/담당자
#   맥락을 잘못 물려받지 않게 한다.
# 모든 답변에 공통으로 적용하는 대화 톤 지침. 검색 결과가 있든 없든 동일하게 주입해
# 딱딱한 데이터 나열 대신 이어지는 대화처럼 답하게 한다.
# 전체 스캔 단계는 API 호출을 아끼려고 제목/속성만으로 매칭한다(본문은 미조회).
# 제목/속성 매칭만으로 요청 개수를 못 채웠을 때만, 최근 N개 미매칭 후보의 본문을
# 실제로 가져와 재매칭한다. 페이지당 API 호출이 추가로 드는 만큼 DB당 상한을 둔다.
# Notion 생성/수정은 회사 데이터에 실제 변경을 가하는 되돌리기 어려운 동작이라
# 곧바로 실행하지 않는다. 먼저 이 문구로 시작하는 미리보기를 보여주고,
# 사용자가 확인 문구로 답할 때만 실제로 반영한다. 서버는 세션/DB에 별도 상태를
# 저장하지 않고, 프론트가 항상 전체 대화 history를 다시 보내는 구조를 그대로
# 활용해 "바로 이전 assistant 턴이 이 문구로 시작하는가"만으로 대기 상태를 판별한다.
# 확인/취소 판정은 위 문구가 정확히 짧은 대답으로 왔을 때만 인정한다(길이 상한).
# 그래야 "네, 근데 그거 말고 다른 이슈도 찾아줘"처럼 확인처럼 보이지만 실제로는
# 다른 요청인 문장을 오인하지 않는다.
# 검증 질문은 코드 수정 없이 assistant_web/config/evaluation_cases.json 에서 추가/수정한다.
# 파일이 없거나 형식이 잘못됐을 때만 아래 내장 기본값을 사용한다(하위 호환).
# 근거를 찾고도 실질적인 분석 대신 회피로 답을 흐리는 표현들. "찾지 못했다"는
# 근거가 아예 없을 때는 정당한 답이지만, 근거가 있는데도 이런 표현이 나오면
# 저정보 답변일 가능성이 크다(evaluate_ab_variant의 informativeness에서 사용).
@app.get("/api/models")
def list_models() -> Dict[str, Any]:
    return {"models": [public_model(model) for model in get_model_registry()]}


@app.post("/api/chroma/search")
def search_chroma(request: ChromaSearchRequest) -> Dict[str, Any]:
    limit = max(1, min(request.limit, 20))
    return execute_chroma_search(request.query, limit, extract_query_constraints(request.query))


@app.get("/api/chroma/documents/{issue_id}")
def read_chroma_document(issue_id: int) -> Dict[str, Any]:
    document = get_chroma_document(issue_id)
    if not document:
        raise HTTPException(status_code=404, detail="ChromaDB 문서를 찾지 못했습니다.")
    return document


@app.get("/api/notion/status")
def notion_status_endpoint() -> Dict[str, Any]:
    settings = get_notion_settings()
    database_ids = settings.get("database_ids", [])
    return {
        "configured": notion_is_configured(),
        "hasToken": bool(settings["token"]),
        "hasDatabaseId": bool(database_ids),
        "databaseCount": len(database_ids),
        "databaseIds": [database_id[-8:] for database_id in database_ids],
        "version": settings["version"],
    }


@app.post("/api/notion/search")
def search_notion(request: ChromaSearchRequest) -> Dict[str, Any]:
    limit = max(1, min(request.limit, 20))
    return execute_notion_search(request.query, limit, extract_query_constraints(request.query))


@app.get("/api/notion/pages/{page_id}")
def get_notion_page_endpoint(page_id: str) -> Dict[str, Any]:
    response = requests.get(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=get_notion_headers(),
        timeout=20,
    )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Notion 항목 조회 실패: {response.status_code} {response.text[:300]}",
        )
    page = response.json()
    database_id = page.get("parent", {}).get("database_id", "")
    return {
        "page": normalize_notion_page(
            page,
            include_blocks=True,
            database_id=database_id,
            database_label=fetch_notion_database_title(database_id) if database_id else "Notion",
        )
    }


@app.post("/api/notion/pages")
def create_notion_page_endpoint(request: NotionPageWriteRequest) -> Dict[str, Any]:
    page = create_notion_page(request)
    return {"page": page, "sources": build_sources_from_single_notion_page(page)}


@app.patch("/api/notion/pages/{page_id}")
def update_notion_page_endpoint(page_id: str, request: NotionPageUpdateRequest) -> Dict[str, Any]:
    page = update_notion_page(page_id, request)
    return {"page": page, "sources": build_sources_from_single_notion_page(page)}


@app.get("/api/validation/logs")
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


@app.get("/api/validation/summary")
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


@app.get("/api/validation/logs/{log_id}")
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


@app.get("/api/evaluation/cases")
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


@app.post("/api/evaluation/run")
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


def stream_accuracy_run(questions: int):
    def frame(event: Dict[str, Any]) -> str:
        return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    if not ACCURACY_RUN_LOCK.acquire(blocking=False):
        yield frame({"type": "error", "message": "이미 실행 중인 정확도 검증이 있습니다. 잠시 후 다시 시도해 주세요."})
        return
    try:
        # 서브프로세스로 실행해 스크립트의 os.environ 변경과 예외가 서버에 영향을
        # 주지 않게 하고, stdout의 PROGRESS 라인을 SSE로 중계한다.
        process = subprocess.Popen(
            [sys.executable, str(ACCURACY_SCRIPT_PATH), "--questions", str(questions), "--progress"],
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


@app.post("/api/accuracy/run")
def run_accuracy(request: AccuracyRunRequest):
    questions = max(1, min(request.questions, 10))
    return StreamingResponse(
        stream_accuracy_run(questions),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/api/data/freshness")
def read_data_freshness() -> Dict[str, Any]:
    return {"mantis": get_mantis_snapshot_freshness_status()}


@app.get("/api/accuracy/reports")
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


@app.get("/api/accuracy/reports/{name}")
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


@app.get("/api/evaluation/history")
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


def build_chat_response(request: ChatRequest) -> Dict[str, Any]:
    emit_chat_status("질문과 대화 맥락을 분석하고 있습니다", "analysis")
    model = get_private_model(request.modelId)

    if model["status"] == "expired":
        raise HTTPException(status_code=400, detail="Selected model is expired")
    if model["status"] == "disabled":
        raise HTTPException(status_code=400, detail="Selected model is disabled")

    api_key = get_model_api_key(model)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=f"Missing API key env: {model.get('apiKeyEnv')}",
        )

    messages = [message.model_dump() for message in request.messages]
    # 대화가 길어져도 모델에 보내는 히스토리는 최근 N개로 제한한다(속도·컨텍스트 한도 보호).
    # 검색 라우팅은 request.messages 전체(최근 5턴)를 별도로 참조하므로 영향받지 않는다.
    history_limit = clamp_int(os.getenv("MODEL_HISTORY_MAX_MESSAGES", "20"), 2, 200) or 20
    if len(messages) > history_limit:
        messages = messages[-history_limit:]
    latest_user_text = get_latest_user_text(request.messages)
    # 후속 질문이면 직전 대화의 조건을 물려받은 라우팅용 텍스트. 검색/조건 추출에만 쓰고
    # 감사 로그·중복 판정·Notion 쓰기는 사용자가 실제 입력한 latest_user_text를 그대로 쓴다.
    retrieval_text, needs_follow_up_rewrite = build_contextual_retrieval_text(request.messages)
    # "그중 3번째" 같은 서수 참조는 직전 답변이 실제로 보여준 결과 목록(감사 로그)에서
    # 해당 항목을 직접 찾는다. 재검색 결과가 달라져 다른 항목을 가리키는 문제가 없고
    # LLM 재작성보다 빠르고 정확하므로 최우선으로 시도한다.
    resolved_reference = resolve_reference_from_context(latest_user_text, request.contextAuditId)
    if resolved_reference:
        retrieval_text = resolved_reference
        needs_follow_up_rewrite = False
    if not resolved_reference:
        # Mantis 조건 상속은 Notion 상태·DB를 물려주지 않아, 노션 후속 질문이
        # 소스와 상태를 모두 잃고 모델 재작성으로 엉뚱한 상태가 채워졌다.
        notion_prefix = inherit_notion_context(get_recent_user_texts(request.messages, limit=5))
        if notion_prefix and not extract_notion_constraints(retrieval_text).get("status"):
            retrieval_text = f"{notion_prefix} {retrieval_text or latest_user_text}".strip()
            needs_follow_up_rewrite = False
    if needs_follow_up_rewrite:
        # 규칙 기반 마커/상속으로 대상을 못 찾은 후속 질문만 모델에게 재작성시킨다.
        # 실패하면 원문 그대로 쓰던 기존 동작으로 조용히 폴백한다.
        rewritten = rewrite_follow_up_query_with_model(
            get_recent_user_texts(request.messages, limit=5),
            model,
            api_key,
        )
        if rewritten:
            retrieval_text = rewritten
    retrieval_text = retrieval_text or latest_user_text
    system_notes = []
    retrieval_result = None
    intent_plan = None
    query_constraints = extract_query_constraints(retrieval_text) if retrieval_text else {}
    enabled_tools = {"mantis": True, "vectorDb": True, "notion": True, **request.tools}
    system_notes.append(answer_style_instruction(request.answerStyle))
    system_notes.append(CONVERSATION_TONE_NOTE)
    system_notes.append(build_current_date_note())
    active_limit = clamp_int(request.searchLimit, 1, 20) or 5

    # 공수 질문 빠른 경로: 규칙(정규식+known_people)이 확신하는 경우 LLM 없이 바로
    # 결정형 계산으로 응답한다. 규칙이 인물/의도를 못 잡으면(pending) 바로 에러를
    # 돌려주지 않고, 아래 LLM 플래너에게 분류를 한 번 더 맡긴 뒤 그래도 안 되면
    # 보류해둔 안내 문구를 반환한다.
    # 등록되지 않은 고객사를 물으면 조건 없는 전체 검색으로 흘러가 엉뚱한
    # 고객사 이슈가 답이 된다. 검색 전에 그 사실을 그대로 알린다.
    if latest_user_text and enabled_tools.get("mantis"):
        unknown_customer_report = build_unknown_customer_report(retrieval_text or latest_user_text)
        if unknown_customer_report:
            return finalize_chat_response(
                request,
                latest_user_text,
                {"modelId": request.modelId, **unknown_customer_report},
            )

    pending_effort_report = None
    if latest_user_text and enabled_tools.get("mantis"):
        ranking_report = build_effort_ranking_report(latest_user_text)
        if ranking_report:
            return finalize_chat_response(
                request,
                latest_user_text,
                {"modelId": request.modelId, **ranking_report},
            )
        effort_report = build_effort_report(latest_user_text)
        if effort_report:
            if effort_report.pop("needsPerson", False):
                pending_effort_report = effort_report
            else:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **effort_report},
                )

    if latest_user_text and enabled_tools.get("notion"):
        notion_write_result = run_notion_write_command(latest_user_text, request.messages)
        if notion_write_result:
            return finalize_chat_response(
                request,
                latest_user_text,
                {"modelId": request.modelId, **notion_write_result},
            )

    if retrieval_text:
        rule_plan = rule_based_search_intent(retrieval_text, query_constraints)
        model_plan: Dict[str, Any] = {}
        planner_enabled = os.getenv("QUERY_PLANNER_ENABLED", "true").strip().lower() not in {"0", "false", "off"}
        should_plan = (
            planner_enabled
            and any(enabled_tools.get(key) for key in ("mantis", "notion", "vectorDb"))
            and (
                rule_plan.get("source") in {"mantis", "notion", "both"}
                or should_run_retrieval(retrieval_text, query_constraints)
            )
        )
        if should_plan:
            emit_chat_status("질문을 도구 실행 계획으로 변환하고 있습니다", "routing")
            model_plan = plan_search_intent_with_model(retrieval_text, model, api_key)
            if rule_plan.get("confidence", 0) >= 0.9 and rule_plan.get("source") != "general":
                model_plan["source"] = rule_plan["source"]
        query_plan = build_query_plan(
            retrieval_text,
            rule_plan,
            query_constraints,
            model_plan if model_plan.get("planned") else None,
        )
        query_constraints = merge_query_plan_constraints(query_constraints, query_plan)
        intent_plan = {
            **query_plan,
            "planned": True,
            "ruleFallback": rule_plan,
        }

        if pending_effort_report is not None and enabled_tools.get("mantis"):
            effort_fallback = build_effort_report_from_plan(retrieval_text, model_plan)
            if effort_fallback:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **effort_fallback},
                )
            # 플래너도 공수 의도를 특정하지 못했다. 규칙 경로가 보류해둔
            # "인물 이름을 입력해 달라"는 안내를 그대로 반환한다.
            return finalize_chat_response(
                request,
                latest_user_text,
                {"modelId": request.modelId, **pending_effort_report},
            )

        if enabled_tools.get("mantis"):
            count_report = build_mantis_count_report(retrieval_text, query_plan)
            if count_report:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **count_report},
                )

    if (
        retrieval_text
        and (enabled_tools.get("mantis") or enabled_tools.get("vectorDb") or enabled_tools.get("notion"))
        and (
            (intent_plan or {}).get("source") in {"notion", "mantis", "both"}
            or should_run_retrieval(retrieval_text, query_constraints)
        )
    ):
        active_source_labels = [
            label
            for key, label in (("mantis", "Mantis"), ("notion", "Notion"), ("vectorDb", "Vector DB"))
            if enabled_tools.get(key)
        ]
        emit_chat_status(f"{' · '.join(active_source_labels)}에서 자료를 찾고 있습니다", "retrieval")
        requested_limit = (intent_plan or {}).get("limit")
        sidebar_limit = clamp_int(request.searchLimit, 1, 20)
        group_by = (intent_plan or {}).get("group_by") or extract_group_by(retrieval_text)
        # 고객사별 최신 1건은 "하나씩"을 전체 1건 제한으로 해석하지 않고,
        # 충분한 최근 이슈를 조회한 뒤 고객사마다 한 건씩 선택한다.
        if group_by == "customer":
            limit = 800
        elif group_by:
            limit = requested_limit or 200
        else:
            limit = requested_limit or resolve_result_limit(latest_user_text, sidebar_limit)
        active_limit = limit
        retrieval_result = run_retrieval_pipeline(
            retrieval_text,
            limit,
            enabled_tools,
            intent_plan=intent_plan,
        )
        emit_chat_status("검색 결과를 질문 조건에 맞게 정리하고 있습니다", "filtering")
        if (intent_plan or {}).get("source") == "both":
            combined_sources = build_sources(retrieval_result, max_sources=limit)
            if combined_sources:
                return finalize_chat_response(request, latest_user_text, {
                    "modelId": request.modelId,
                    "content": format_deterministic_combined_answer(combined_sources),
                    "sources": combined_sources if request.showSources else [],
                    "sourcesForFollowUps": combined_sources,
                    "retrieval": retrieval_result,
                    "raw": {"mode": "deterministic_combined_search"},
                })
        if should_return_deterministic_notion_list(retrieval_text, retrieval_result):
            notion_pages = collect_retrieved_notion_pages(retrieval_result, limit)
            notion_sources = build_sources_from_notion_pages(notion_pages)
            return finalize_chat_response(request, latest_user_text, {
                "modelId": request.modelId,
                "content": format_deterministic_notion_answer(
                    retrieval_text,
                    retrieval_result,
                    limit,
                ),
                "sources": notion_sources if request.showSources else [],
                "sourcesForFollowUps": notion_sources,
                "retrieval": retrieval_result,
                "raw": {"mode": "deterministic_notion_list"},
            })
        if should_return_deterministic_issue_list(retrieval_text, query_constraints):
            deterministic_issues = select_grouped_issue_rows(
                collect_retrieved_issues(retrieval_result, limit),
                group_by,
            )
            issue_sources = build_sources_from_issue_rows(deterministic_issues)
            return finalize_chat_response(request, latest_user_text, {
                "modelId": request.modelId,
                "content": format_deterministic_issue_answer(
                    retrieval_text,
                    query_constraints,
                    retrieval_result,
                    limit,
                    request.answerStyle,
                ),
                "sources": issue_sources if request.showSources else [],
                "sourcesForFollowUps": issue_sources,
                "retrieval": retrieval_result,
                "raw": {"mode": "deterministic_issue_list"},
            })
        model_retrieval_context = build_model_retrieval_context(retrieval_result, limit)
        system_notes.append(
            "너는 컨텐츠서비스 AI 비서다. 아래 [검색 파이프라인 결과] JSON에 있는 데이터만 사실로 사용해라. "
            "검색 파이프라인은 Notion DB 조회, Mantis 저장 필터, Mantis 페이지 스캔, ChromaDB 검색으로 구성된다. "
            "Notion 결과는 팀 주요 이슈/공지/일정/결재 상태 조회에 사용하고, Mantis 결과는 원본 이슈 목록 조회에 우선 사용하며, ChromaDB 결과는 유사 사례나 보조 맥락으로 사용해라. "
            "ChromaDB 문서에 latest_issue가 있으면 ChromaDB 스냅샷 대신 latest_issue 값을 최신 Mantis 원본으로 사용해라. "
            "다만 Mantis 결과가 요청 개수보다 부족하고 ChromaDB 결과가 질문의 고객사/담당자/키워드 조건과 일치하면, ChromaDB 결과도 실제 이슈 목록으로 포함해라. "
            "질문에 고객사, 담당자, HTS, MTS 같은 조건이 있으면 그 조건과 일치하는 이슈만 답변에 포함해라. "
            "예를 들어 HTS 질문에는 제목에 MTS가 있더라도 본문이나 제목에 HTS가 함께 있을 때만 포함하고, MTS 전용 이슈는 제외해라. "
            "실시간 조회를 할 수 없다고 말하지 마라. "
            "사용자가 검색 과정이나 기준을 묻지 않는 한 mantis_filter, mantis_page_scan, chroma_db 같은 내부 단계명이나 스킵 여부는 말하지 마라. "
            "대신 필요한 경우 '검색 기준: 고객사/담당자/상태/키워드, 정렬: 최신 이슈 ID 기준'처럼 사용자에게 의미 있는 기준만 짧게 말해라. "
            "이슈가 있으면 번호 목록으로 ID, 프로젝트, 제목, 상태, 담당자, 등록일을 포함해 요약해라.\n\n"
            f"[검색 파이프라인 결과]\n{json.dumps(model_retrieval_context, ensure_ascii=False, indent=2)}"
        )
    else:
        system_notes.append(
            "너는 컨텐츠서비스 AI 비서다. 사용자가 일반 대화나 모델/사용법을 묻는 경우에는 Mantis나 ChromaDB 이슈를 언급하지 말고 직접 답하라. "
            "사용자가 고객사 이슈, 담당자, 화면번호, HTS/MTS, Mantis 조회처럼 사내 데이터 검색을 명확히 요청할 때만 이슈 조회가 필요하다고 이해하라."
        )

    if system_notes:
        messages.insert(0, {"role": "system", "content": "\n\n".join(system_notes)})

    # 1차 모델이 타임아웃/오류일 때 시도할 대체 모델 목록. models.json에 활성 모델이
    # 하나뿐이면(현재 기본 상태) 이 목록은 [model] 하나뿐이라 동작은 기존과 동일하다.
    candidate_models = [model] + [
        other
        for other in get_model_registry()
        if other["id"] != model["id"] and other.get("status") == "active"
    ]

    response = None
    final_model = model
    timeout_exc: Optional[requests.ReadTimeout] = None
    request_exc: Optional[requests.RequestException] = None
    emit_chat_status("답변을 생성하고 있습니다", "generation")

    for index, candidate in enumerate(candidate_models):
        is_last_candidate = index == len(candidate_models) - 1
        candidate_api_key = get_model_api_key(candidate)
        if not candidate_api_key:
            continue

        candidate_payload = {
            "model": candidate.get("model", candidate["id"]),
            "messages": messages,
            "temperature": request.temperature,
            "stream": bool(request.stream),
        }
        candidate_payload.update(build_generation_options(request))

        try:
            model_timeout = clamp_int(os.getenv("MODEL_TIMEOUT_SECONDS", "180"), 30, 300) or 180
            candidate_response = post_model_chat(
                candidate,
                candidate_api_key,
                candidate_payload,
                timeout=model_timeout,
                stream=bool(request.stream),
            )
        except requests.ReadTimeout as exc:
            timeout_exc = exc
            if is_last_candidate:
                break
            continue
        except requests.RequestException as exc:
            request_exc = exc
            if is_last_candidate:
                break
            continue

        if candidate_response.status_code >= 400:
            if is_last_candidate:
                response = candidate_response
                final_model = candidate
                break
            continue

        response = candidate_response
        final_model = candidate
        timeout_exc = None
        request_exc = None
        break

    if response is None:
        if timeout_exc is not None:
            if retrieval_result:
                fallback_sources = build_sources(retrieval_result, max_sources=active_limit)
                return finalize_chat_response(request, latest_user_text, {
                    "modelId": final_model["id"],
                    "content": format_model_timeout_fallback(
                        query_constraints,
                        retrieval_result,
                        active_limit,
                    ),
                    "sources": fallback_sources if request.showSources else [],
                    "sourcesForFollowUps": fallback_sources,
                    "retrieval": retrieval_result,
                    "raw": {"mode": "model_timeout_fallback", "error": str(timeout_exc)},
                })
            raise HTTPException(status_code=502, detail=f"Model connection failed: {timeout_exc}")
        raise HTTPException(status_code=502, detail=f"Model connection failed: {request_exc}")

    if response.status_code >= 400:
        unsupported_hint = ""
        if any(
            keyword in response.text.lower()
            for keyword in ("extra", "unknown", "unsupported", "unexpected", "not permitted")
        ):
            unsupported_hint = " 선택한 생성 옵션 중 회사 모델 서버가 지원하지 않는 값이 있을 수 있습니다. 해당 옵션을 비우고 다시 시도해 주세요."
        raise HTTPException(
            status_code=response.status_code,
            detail=(response.text[:1000] + unsupported_hint),
        )

    if request.stream and is_ollama_model(final_model):
        streamed_content = ""
        stream_chunks: List[Dict[str, Any]] = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="replace").strip()
            else:
                line = str(raw_line or "").strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            stream_chunks.append(chunk)
            delta = chunk.get("message", {}).get("content", "")
            if delta:
                streamed_content += delta
                CHAT_STREAM_CONTEXT.content_streamed = True
                emit_chat_event("delta", content=delta)
        data = {
            "choices": [{"message": {"content": streamed_content}}],
            "streamChunks": len(stream_chunks),
            "provider": "ollama",
        }
    elif request.stream and "text/event-stream" in response.headers.get("content-type", "").lower():
        streamed_content = ""
        stream_chunks: List[Dict[str, Any]] = []
        for raw_line in response.iter_lines(decode_unicode=True):
            line = str(raw_line or "").strip()
            if not line or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
            stream_chunks.append(chunk)
            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
            if delta:
                streamed_content += delta
                CHAT_STREAM_CONTEXT.content_streamed = True
                emit_chat_event("delta", content=delta)
        data = {
            "choices": [{"message": {"content": streamed_content}}],
            "streamChunks": len(stream_chunks),
        }
    else:
        data = response.json()
    content = extract_model_content(data)

    final_sources = build_sources(retrieval_result, max_sources=active_limit)
    return finalize_chat_response(request, latest_user_text, {
        "modelId": final_model["id"],
        "content": content or "응답 본문을 찾지 못했습니다.",
        "sources": final_sources if request.showSources else [],
        "sourcesForFollowUps": final_sources,
        "retrieval": retrieval_result,
        "raw": data,
        "usedFallbackModel": final_model["id"] if final_model["id"] != model["id"] else None,
    })


def stream_chat_response(request: ChatRequest):
    events: queue.Queue = queue.Queue()
    sentinel = object()

    def emit(event: Dict[str, Any]) -> None:
        events.put(event)

    def worker() -> None:
        CHAT_STREAM_CONTEXT.emitter = emit
        CHAT_STREAM_CONTEXT.content_streamed = False
        try:
            result = build_chat_response(request)
            events.put({"type": "result", "data": result})
        except HTTPException as exc:
            events.put({"type": "error", "message": str(exc.detail), "status": exc.status_code})
        except Exception as exc:
            events.put({"type": "error", "message": f"응답 처리 중 오류가 발생했습니다: {type(exc).__name__}"})
        finally:
            CHAT_STREAM_CONTEXT.emitter = None
            CHAT_STREAM_CONTEXT.content_streamed = False
            events.put(sentinel)

    threading.Thread(target=worker, daemon=True).start()
    while True:
        event = events.get()
        if event is sentinel:
            break
        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post("/api/chat")
def chat(request: ChatRequest):
    if not request.stream:
        return build_chat_response(request)
    return StreamingResponse(
        stream_chat_response(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
