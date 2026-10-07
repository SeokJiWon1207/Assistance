"""채팅 API와 SSE 전달 계층."""

import asyncio
import json
import queue
import threading
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.admission import CHAT_ADMISSION, ChatAdmissionLease
from backend.auth import current_request_user
from backend.audit import CHAT_STREAM_CONTEXT
from backend.execution import ExecutionBudget, RequestStopped, checkpoint, execution_scope
from backend.schemas import ChatRequest
from backend.services.chat_service import chat_service


router = APIRouter()


async def chat_events(
    request: ChatRequest,
    user: Dict[str, Any] | None,
    admission_lease: ChatAdmissionLease,
    budget: ExecutionBudget,
    connection: Request | None = None,
):
    events: queue.Queue = queue.Queue()
    sentinel = object()

    def emit(event: Dict[str, Any]) -> None:
        checkpoint()
        events.put(event)

    def worker() -> None:
        CHAT_STREAM_CONTEXT.emitter = emit
        CHAT_STREAM_CONTEXT.content_streamed = False
        try:
            with execution_scope(budget):
                result = chat_service.build_admitted_response(request, user=user)
                checkpoint()
                events.put({"type": "result", "data": result})
        except (HTTPException, RequestStopped) as exc:
            events.put({"type": "error", "message": str(exc.detail), "status": exc.status_code})
        except Exception as exc:
            events.put({"type": "error", "message": f"응답 처리 중 오류가 발생했습니다: {type(exc).__name__}"})
        finally:
            CHAT_STREAM_CONTEXT.emitter = None
            CHAT_STREAM_CONTEXT.content_streamed = False
            events.put(sentinel)
            # 클라이언트가 SSE를 먼저 끊어도 실제 모델/검색 워커가 끝날 때까지
            # 동시성 슬롯을 유지한다.
            try:
                budget.wait_for_workers()
            finally:
                admission_lease.release()

    try:
        threading.Thread(target=worker, daemon=True).start()
    except BaseException:
        admission_lease.release()
        raise
    try:
        while True:
            if connection is not None and await connection.is_disconnected():
                break
            try:
                budget.remaining()
                event = events.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            except RequestStopped as exc:
                yield {"type": "error", "message": exc.detail, "status": exc.status_code}
                break
            if event is sentinel:
                break
            yield event
            if event["type"] in {"result", "error"}:
                break
    finally:
        # SSE 연결 해제와 비스트리밍 요청 취소를 같은 실행 예산으로 전달한다.
        # 실행 중인 동기 I/O는 소켓 타임아웃으로 끝나며 lease는 워커가 해제한다.
        budget.cancel()


async def stream_chat_response(request, user, admission_lease, budget):
    iterator = chat_events(request, user, admission_lease, budget)
    try:
        async for event in iterator:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        await iterator.aclose()


@router.post("/api/chat")
async def chat(payload: ChatRequest, http_request: Request):
    user = current_request_user(http_request)
    user_key = str((user or {}).get("uid") or "anonymous").strip()
    admission_lease = CHAT_ADMISSION.acquire(user_key)
    budget = ExecutionBudget.create()
    if not payload.stream:
        iterator = chat_events(payload, user, admission_lease, budget, http_request)
        try:
            async for event in iterator:
                if await http_request.is_disconnected():
                    raise HTTPException(499, "요청이 취소되었습니다.")
                if event["type"] == "result":
                    return event["data"]
                if event["type"] == "error":
                    raise HTTPException(event.get("status", 500), event["message"])
        finally:
            await iterator.aclose()
        raise HTTPException(499, "요청이 취소되었습니다.")
    try:
        event_iterator = stream_chat_response(payload, user, admission_lease, budget)
        return StreamingResponse(
            event_iterator,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except Exception:
        admission_lease.release()
        raise
