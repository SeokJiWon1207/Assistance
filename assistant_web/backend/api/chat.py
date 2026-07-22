"""채팅 API와 SSE 전달 계층."""

import json
import queue
import threading
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from backend.audit import CHAT_STREAM_CONTEXT
from backend.schemas import ChatRequest
from backend.services.chat_service import chat_service


router = APIRouter()


def stream_chat_response(request: ChatRequest):
    events: queue.Queue = queue.Queue()
    sentinel = object()

    def emit(event: Dict[str, Any]) -> None:
        events.put(event)

    def worker() -> None:
        CHAT_STREAM_CONTEXT.emitter = emit
        CHAT_STREAM_CONTEXT.content_streamed = False
        try:
            result = chat_service.build_response(request)
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


@router.post("/api/chat")
def chat(request: ChatRequest):
    if not request.stream:
        return chat_service.build_response(request)
    return StreamingResponse(
        stream_chat_response(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
