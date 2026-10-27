"""채팅 API와 SSE 전달 계층."""

import asyncio
import json
import queue
import threading
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from backend.admission import CHAT_ADMISSION, ChatAdmissionLease, ChatAdmissionTicket
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


async def queued_chat_events(request, user, ticket: ChatAdmissionTicket, connection=None):
    """대기 중에는 작업 스레드·실행 예산을 만들지 않고 순서만 알린다."""
    lease = None
    handed_to_worker = False
    worker_events = None
    last_position, last_notice = -1, 0.0
    try:
        while lease is None:
            if connection is not None and await connection.is_disconnected():
                return
            lease = ticket.try_start()
            if lease is not None:
                break
            position = ticket.position
            now = asyncio.get_running_loop().time()
            if position != last_position or now - last_notice >= 5:
                yield {"type": "status", "stage": "queue", "queuePosition": position,
                       "message": f"앞선 요청을 처리하고 있습니다. 대기 순서 {position}번째입니다."}
                last_position, last_notice = position, now
            await asyncio.sleep(0.1)
        # 대기 시간이 모델/검색의 실행 시간을 소진하지 않게 한다.
        budget = ExecutionBudget.create()
        worker_events = chat_events(request, user, lease, budget, connection)
        handed_to_worker = True
        async for event in worker_events:
            yield event
    except HTTPException as exc:
        yield {"type": "error", "message": str(exc.detail), "status": exc.status_code}
    finally:
        ticket.cancel()
        if worker_events is not None:
            await worker_events.aclose()
        if lease is not None and not handed_to_worker:
            lease.release()


async def stream_chat_response(request, user, ticket):
    iterator = queued_chat_events(request, user, ticket)
    try:
        async for event in iterator:
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        await iterator.aclose()


@router.post("/api/chat")
async def chat(payload: ChatRequest, http_request: Request):
    user = current_request_user(http_request)
    user_key = str((user or {}).get("uid") or "anonymous").strip()
    ticket = CHAT_ADMISSION.enqueue(user_key)
    if not payload.stream:
        iterator = queued_chat_events(payload, user, ticket, http_request)
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
        event_iterator = stream_chat_response(payload, user, ticket)
        return StreamingResponse(
            event_iterator,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            background=BackgroundTask(ticket.cancel),
        )
    except Exception:
        ticket.cancel()
        raise
