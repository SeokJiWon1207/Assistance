"""감사 로그를 기록하고 최종 채팅 응답을 조립한다."""

import json
import os
import sqlite3
import sys
import uuid
from time import perf_counter
from contextlib import closing
from typing import Any, Callable, Dict, TypeVar

from backend.answers import generate_follow_up_questions
from backend.audit import CHAT_STREAM_CONTEXT, emit_chat_event, emit_chat_status, init_audit_db
from backend.config import AUDIT_DB_PATH
from backend.evaluation.comparison import (
    evaluate_ab_comparison,
    generate_candidate_model_answer,
    should_attach_ab_test,
)
from backend.evaluation.data_scope import build_answer_data_scope
from backend.evaluation.delivery import BLOCKED_ANSWER, decide_delivery
from backend.execution import checkpoint
from backend.evaluation.verification import (
    build_verification_report,
    generate_basic_grounded_baseline,
)
from backend.schemas import ChatRequest


Dependency = TypeVar("Dependency", bound=Callable[..., Any])


def _compat_dependency(name: str, default: Dependency) -> Dependency:
    """호환 파사드에 적용된 테스트 패치를 최종 조립에도 전달한다."""
    core = sys.modules.get("backend.evaluation.core")
    return getattr(core, name, default) if core is not None else default


def finalize_chat_response(
    request: ChatRequest,
    question: str,
    response: Dict[str, Any],
    user_uid: str = "",
) -> Dict[str, Any]:
    checkpoint()
    finalization_started = perf_counter()
    generate_followups = _compat_dependency(
        "generate_follow_up_questions", generate_follow_up_questions
    )
    build_verification = _compat_dependency(
        "build_verification_report", build_verification_report
    )
    should_attach = _compat_dependency("should_attach_ab_test", should_attach_ab_test)
    generate_candidate = _compat_dependency(
        "generate_candidate_model_answer", generate_candidate_model_answer
    )
    generate_baseline = _compat_dependency(
        "generate_basic_grounded_baseline", generate_basic_grounded_baseline
    )
    evaluate_comparison = _compat_dependency(
        "evaluate_ab_comparison", evaluate_ab_comparison
    )
    sources = response.get("sources") or []
    # sourcesForFollowUps는 화면 출처 표시 여부(showSources)와 무관하게 항상 채워지는
    # 전체 근거 목록이다. 사용자가 "출처 표시"를 꺼서 sources가 []가 되더라도
    # 후속 질문 제안과 검증 리포트는 실제로 찾은 근거를 기준으로 계속 동작해야 한다.
    evidence_sources = response.pop("sourcesForFollowUps", None)
    if evidence_sources is None:
        evidence_sources = sources
    retrieval_result = response.get("retrieval")
    mode = (response.get("raw") or {}).get("mode", "model")
    emit_chat_status("근거와 답변을 검증하고 있습니다", "verification")
    verification = build_verification(
        question,
        response.get("content", ""),
        evidence_sources,
        retrieval_result,
        mode,
    )
    checkpoint()
    verification["dataScope"] = build_answer_data_scope(retrieval_result)
    delivery = decide_delivery(verification)
    verification["delivery"] = "allowed" if delivery.allowed else "blocked"
    blocked_answer = None
    followups: list[str]
    if not delivery.allowed:
        blocked_answer = response.get("content", "")
        response["content"] = BLOCKED_ANSWER
        response["sources"] = sources = []
        response["retrieval"] = None
        response.pop("abTest", None)
        response["raw"] = {"mode": "verification_blocked", "originalMode": mode,
                           "blockedChecks": list(delivery.blocked_checks)}
        mode = "verification_blocked"
        followups = []
    else:
        followups = response.get("followUps") or generate_followups(
            question, evidence_sources, retrieval_result, mode,
        )
    if delivery.allowed and should_attach(request, retrieval_result, mode):
        checkpoint()
        candidate_model_id = str(
            request.abModelId or os.getenv("AB_COMPARISON_MODEL_ID", "")
        ).strip()
        comparing_models = bool(
            candidate_model_id
            and candidate_model_id != request.modelId
            and mode == "model"
        )
        emit_chat_status(
            "후보 모델로 동일한 검색 근거를 비교하고 있습니다"
            if comparing_models
            else "기본 Mantis·Notion 연동 답변을 생성하고 있습니다",
            "comparison",
        )
        baseline = (
            generate_candidate(
                request,
                question,
                retrieval_result or {},
                candidate_model_id,
            )
            if comparing_models
            else generate_baseline(request, question, retrieval_result or {})
        )
        variant_a_evaluation_input = {
            "status": "ok",
            "content": response.get("content", ""),
            "sources": evidence_sources,
        }
        response["abTest"] = {
            "enabled": True,
            "variantA": {
                "label": f"현재 모델 · {request.modelId}" if comparing_models else "전처리 적용",
                "description": (
                    "현재 모델이 검증된 동일 검색 근거로 생성한 답변"
                    if comparing_models
                    else "Mantis·Notion 검색, 조건 필터링, 근거 검증을 적용한 답변"
                ),
            },
            "variantB": {
                "label": f"후보 모델 · {candidate_model_id}" if comparing_models else "기본 검색 연동",
                "description": (
                    "후보 모델이 현재 모델과 같은 검증·검색 결과로 생성한 답변"
                    if comparing_models
                    else "Mantis·Notion 기본 검색 결과만 전달하고 별칭 보정·재정렬·검증은 적용하지 않은 답변"
                ),
                **baseline,
            },
        }
        response["abTest"]["evaluation"] = evaluate_comparison(
            question,
            variant_a_evaluation_input,
            baseline,
        )
    log_id = uuid.uuid4().hex
    # 모델 fallback이 일어난 경우 response["modelId"]는 실제로 답변한 모델이다.
    # 감사 로그는 사용자가 고른 모델이 아니라 실제로 응답을 생성한 모델을 기록해야 한다.
    answered_model_id = response.get("modelId") or request.modelId
    timings = response.setdefault("timings", {})
    timings["verificationAndFormattingMs"] = round((perf_counter() - finalization_started) * 1000, 2)
    payload = {
        "question": question,
        "userUid": str(user_uid or "").strip(),
        "answer": response.get("content", ""),
        "modelId": answered_model_id,
        "mode": mode,
        "timings": timings,
        "generation": {
            "temperature": request.temperature,
            "answerStyle": request.answerStyle,
            "responseFormat": request.responseFormat,
        },
        "sources": sources,
        # 차단된 실행의 조건은 다음 질문에 상속하지 않는다.
        "queryPlan": (retrieval_result or {}).get("query_plan") if delivery.allowed else None,
        "filterAudit": (retrieval_result or {}).get("filter_audit") if retrieval_result else None,
        "retrievalStages": [
            {
                "source": stage.get("source"),
                "status": stage.get("status"),
                "reason": stage.get("reason"),
                "meta": stage.get("meta", {}),
            }
            for stage in (retrieval_result or {}).get("stages", [])
        ],
        "followUps": followups,
        "verification": verification,
        "abTest": response.get("abTest"),
    }
    if blocked_answer is not None:
        payload["blockedAnswer"] = blocked_answer
    checkpoint()
    init_audit_db()
    with closing(sqlite3.connect(AUDIT_DB_PATH)) as connection, connection:
        connection.execute(
            """
            INSERT INTO validation_logs (
                id, created_at, question, model_id, mode, score, status, payload,
                user_uid, failed_checks_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                verification["verifiedAt"],
                question,
                answered_model_id,
                mode,
                verification["score"],
                verification["status"],
                json.dumps(payload, ensure_ascii=False),
                str(user_uid or "").strip(),
                json.dumps(
                    [
                        {
                            "id": str(check.get("id") or "unknown")[:128],
                            "label": str(check.get("label") or check.get("id") or "unknown")[:256],
                        }
                        for check in verification.get("checks", [])
                        if check.get("status") == "fail"
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
    response["verification"] = verification
    response["auditId"] = log_id
    response["followUps"] = followups
    checkpoint()
    if getattr(CHAT_STREAM_CONTEXT, "emitter", None):
        content = str(response.get("content", ""))
        for start in range(0, len(content), 120):
            emit_chat_event("delta", content=content[start : start + 120])
        CHAT_STREAM_CONTEXT.content_streamed = True
    return response


__all__ = ["finalize_chat_response"]
