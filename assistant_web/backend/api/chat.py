"""채팅 API와 SSE 전달 계층."""

import json
import queue
import threading
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from backend.admission import CHAT_ADMISSION, ChatAdmissionLease
from backend.auth import current_request_user
from backend.audit import CHAT_STREAM_CONTEXT
from backend.schemas import ChatRequest
from backend.services.chat_service import chat_service


router = APIRouter()


def stream_chat_response(
    request: ChatRequest,
    user: Dict[str, Any] | None,
    admission_lease: ChatAdmissionLease,
):
    events: queue.Queue = queue.Queue()
    sentinel = object()

    def emit(event: Dict[str, Any]) -> None:
        events.put(event)

    def worker() -> None:
        CHAT_STREAM_CONTEXT.emitter = emit
        CHAT_STREAM_CONTEXT.content_streamed = False
        try:
            result = chat_service.build_admitted_response(request, user=user)
            events.put({"type": "result", "data": result})
        except HTTPException as exc:
            events.put({"type": "error", "message": str(exc.detail), "status": exc.status_code})
        except Exception as exc:
            events.put({"type": "error", "message": f"응답 처리 중 오류가 발생했습니다: {type(exc).__name__}"})
        finally:
            CHAT_STREAM_CONTEXT.emitter = None
            CHAT_STREAM_CONTEXT.content_streamed = False
            events.put(sentinel)
            # 클라이언트가 SSE를 먼저 끊어도 실제 모델/검색 워커가 끝날 때까지
            # 동시성 슬롯을 유지한다.
            admission_lease.release()

    threading.Thread(target=worker, daemon=True).start()

    def iterate_events():
        while True:
            event = events.get()
            if event is sentinel:
                break
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

    return iterate_events()


@router.post("/api/chat")
def chat(payload: ChatRequest, http_request: Request):
    user = current_request_user(http_request)
    user_key = str((user or {}).get("uid") or "anonymous").strip()
    admission_lease = CHAT_ADMISSION.acquire(user_key)
    if not payload.stream:
        with admission_lease:
            return chat_service.build_admitted_response(payload, user=user)
    try:
        event_iterator = stream_chat_response(payload, user, admission_lease)
        return StreamingResponse(
            event_iterator,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    except Exception:
        admission_lease.release()
        raise
