"""단계 경계의 호출 순서·문맥·저장 결과를 기록하는 네트워크 없는 고정 사례."""

from contextlib import ExitStack, contextmanager
from copy import deepcopy
from unittest.mock import patch

from fastapi import HTTPException

from backend import execution, timing
from backend.audit import CHAT_STREAM_CONTEXT
from backend.schemas import ChatRequest
from backend.services import chat_service as service
from backend.services.contracts import ChatPipelineContext, StageResult


def _capture_pipeline_case(scenario, admitted):
    calls, events, budgets, timings, contexts, stored = [], [], [], [], [], []
    question = "내가 누구야" if scenario == "identity" else "고정 질문"
    request = ChatRequest(modelId="fixture", messages=[{"role": "user", "content": question}], abTest=False)
    context = ChatPipelineContext(
        latest_user_text=question, model={"id": "fixture"}, api_key="synthetic", messages=[],
        retrieval_text="확정 질문", enabled_tools={}, system_notes=["고정 안내"], query_constraints={},
        active_limit=5, intent_plan={"source": "general", **(
            {"planner": "unified_llm"} if scenario == "unified_generation" else {})},
    )

    def record(name, current=None):
        calls.append(name)
        budgets.append(execution.CURRENT_BUDGET.get())
        timings.append(timing.TIMINGS.get())
        if current is not None:
            contexts.append(current)

    def answer(mode):
        return {"modelId": "fixture", "content": "확정 답변", "sources": [], "raw": {"mode": mode}}

    def prepare(*_args):
        record("prepare", context)
        return context

    def plan(_request, current, _user):
        record("plan", current)
        modes = {"plan_answer": "deterministic_count", "clarification": "status_clarification"}
        return StageResult.from_response(answer(modes[scenario]) if scenario in modes else None)

    def retrieve(_request, current):
        record("retrieve", current)
        if scenario == "cancel_during":
            execution.CURRENT_BUDGET.get().cancel()
            execution.checkpoint()
        modes = {"retrieved_answer": "deterministic_list", "empty_evidence": "empty_evidence"}
        return StageResult.from_response(answer(modes[scenario]) if scenario in modes else None)

    def generate(*_args):
        record("generate")
        if scenario == "model_failure":
            raise HTTPException(502, "고정 모델 오류")
        return answer("model")

    def persist(_request, text, response, **kwargs):
        record("persist")
        copied = deepcopy(response)
        copied.pop("timings", None)
        stored.append({"question": text, "response": copied, "kwargs": kwargs})
        return response

    @contextmanager
    def admission(_key):
        calls.append("admit")
        try:
            yield
        finally:
            calls.append("release")

    with ExitStack() as stack:
        for name, replacement in (("prepare_chat_pipeline", prepare), ("plan_chat_query", plan),
                                  ("retrieve_chat_evidence", retrieve), ("generate_model_response", generate),
                                  ("persist_chat_response", persist)):
            stack.enter_context(patch.object(service, name, side_effect=replacement))
        stack.enter_context(patch.object(service.CHAT_ADMISSION, "wait", side_effect=admission))
        stack.enter_context(patch.object(CHAT_STREAM_CONTEXT, "emitter", events.append, create=True))
        cancelled_token = None
        if scenario == "cancel_before":
            cancelled = execution.ExecutionBudget.create()
            cancelled.cancel()
            cancelled_token = execution.CURRENT_BUDGET.set(cancelled)
        error = None
        try:
            method = service.chat_service.build_admitted_response if admitted else service.chat_service.build_response
            result = method(request, user={"uid": "fixture-user", "displayName": "테스트 사용자"})
            result.pop("timings", None)
        except (HTTPException, execution.RequestStopped) as exc:
            result, error = None, {"status": exc.status_code, "detail": exc.detail}
        finally:
            if cancelled_token is not None:
                execution.CURRENT_BUDGET.reset(cancelled_token)
    return {
        "scenario": scenario, "admitted": admitted, "calls": calls, "result": result, "error": error,
        "events": [{key: value for key, value in event.items() if key != "elapsedMs"} for event in events],
        "stored": stored, "shared_budget": not budgets or (budgets[0] is not None and all(b is budgets[0] for b in budgets)),
        "shared_timing": not timings or (timings[0] is not None and all(t is timings[0] for t in timings)),
        "shared_context": all(c is context for c in contexts),
        "budget_restored": execution.CURRENT_BUDGET.get() is None,
        "timing_restored": timing.TIMINGS.get() is None,
    }


def capture_pipeline_cases():
    scenarios = ("identity", "plan_answer", "clarification", "retrieved_answer", "empty_evidence",
                 "generation", "unified_generation", "model_failure", "cancel_before", "cancel_during")
    return [_capture_pipeline_case(scenario, admitted) for scenario in scenarios for admitted in (False, True)]
