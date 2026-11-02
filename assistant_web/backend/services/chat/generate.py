"""모델 생성·스트림 읽기·후보 폴백을 처리한다."""

import json
import os
from time import perf_counter
from typing import Any, Dict, List, Optional
import requests
from fastapi import HTTPException

from backend.answers import (
    format_deterministic_combined_answer,
    format_model_timeout_fallback,
    sanitize_customer_notice_answer,
)
from backend.execution import checkpoint
from backend.audit import CHAT_STREAM_CONTEXT, emit_chat_event, emit_chat_status
from backend.utils.numbers import clamp_int
from backend.llm import (
    build_generation_options,
    consume_model_response,
    extract_model_content,
    get_model_registry,
    get_fallback_models,
    is_ollama_model,
    post_model_chat,
)
from backend.notion.formatting import append_notion_completeness_notice, format_deterministic_notion_answer
from backend.evidence.model_context import build_model_evidence_sources
from backend.retrieval import build_sources
from backend.evidence.retrieval_rows import collect_retrieved_issues
from backend.schemas import ChatRequest
from backend.notion.matching import collect_retrieved_notion_pages
from backend import llm


def generate_model_response(
    request: ChatRequest,
    model: Dict[str, Any],
    messages: List[Dict[str, Any]],
    retrieval_result: Optional[Dict[str, Any]],
    query_constraints: Dict[str, Any],
    active_limit: int,
) -> Dict[str, Any]:
    """활성 모델 폴백, 스트림 해석, 검색 타임아웃 폴백을 한곳에서 처리한다."""
    candidate_models = [model, *get_fallback_models(model, get_model_registry())]

    data = None
    final_model = model
    last_error: Optional[Exception] = None
    model_timeout = clamp_int(os.getenv("MODEL_TIMEOUT_SECONDS", "180"), 30, 300) or 180
    stream_read_timeout = min(
        model_timeout,
        clamp_int(os.getenv("MODEL_STREAM_READ_TIMEOUT_SECONDS", "60"), 10, 300) or 60,
    )
    first_content_timeout = min(
        model_timeout,
        clamp_int(os.getenv("MODEL_FIRST_CONTENT_TIMEOUT_SECONDS", "60"), 10, 300) or 60,
    )
    emit_chat_status("답변을 생성하고 있습니다", "generation")

    for candidate in candidate_models:
        checkpoint()
        candidate_api_key = llm.get_model_api_key(candidate)
        if not candidate_api_key:
            continue

        candidate_payload = {
            "model": candidate.get("model", candidate["id"]),
            "messages": messages,
            "temperature": 0 if retrieval_result else request.temperature,
            "stream": bool(request.stream),
        }
        candidate_payload.update(build_generation_options(request))
        final_model = candidate

        try:
            first_content_deadline = perf_counter() + first_content_timeout if request.stream else None
            candidate_response = post_model_chat(
                candidate,
                candidate_api_key,
                candidate_payload,
                timeout=stream_read_timeout if request.stream else model_timeout,
                stream=bool(request.stream),
            )
            # HTTP 200 뒤에 발생하는 본문 읽기 오류도 같은 후보 폴백 범위에서
            # 처리한다. 본문 소비가 끝나야 회로 성공 여부를 확정하고 연결을 닫는다.
            with consume_model_response(candidate_response):
                if candidate_response.status_code >= 400:
                    error_text = candidate_response.text
                    unsupported_hint = ""
                    if any(keyword in error_text.lower() for keyword in (
                        "extra", "unknown", "unsupported", "unexpected", "not permitted",
                    )):
                        unsupported_hint = " 선택한 생성 옵션 중 회사 모델 서버가 지원하지 않는 값이 있을 수 있습니다. 해당 옵션을 비우고 다시 시도해 주세요."
                    raise HTTPException(
                        status_code=candidate_response.status_code,
                        detail=error_text[:1000] + unsupported_hint,
                    )
                data = _read_model_response(
                    candidate_response, candidate, request.stream,
                    first_content_deadline=first_content_deadline,
                )
                if not extract_model_content(data).strip():
                    raise requests.ConnectionError("모델 응답에 답변 본문이 없습니다.")
        except (requests.RequestException, HTTPException) as exc:
            checkpoint()
            last_error = exc
            # 실패한 후보의 부분 답변과 다음 후보/근거 답변을 이어 붙이지 않는다.
            if getattr(CHAT_STREAM_CONTEXT, "content_streamed", False):
                emit_chat_event("reset")
                CHAT_STREAM_CONTEXT.content_streamed = False
            emit_chat_status("응답 연결을 다시 확인하고 있습니다", "generation")
            data = None
            continue
        break

    if data is None:
        if retrieval_result and isinstance(last_error, requests.RequestException):
            fallback_sources = build_sources(retrieval_result, max_sources=active_limit)
            notion_pages = collect_retrieved_notion_pages(retrieval_result, active_limit)
            issues = collect_retrieved_issues(retrieval_result, active_limit)
            if notion_pages and issues:
                fallback_content = format_deterministic_combined_answer(fallback_sources)
            elif not issues and any(stage.get("source") == "notion" for stage in retrieval_result.get("stages", [])):
                fallback_content = format_deterministic_notion_answer(
                    str(retrieval_result.get("query") or ""), retrieval_result, active_limit,
                )
            else:
                fallback_content = format_model_timeout_fallback(query_constraints, retrieval_result, active_limit)
            if notion_pages:
                fallback_content = "모델 분석 응답이 지연되어 확인한 자료를 안내합니다.\n\n" + fallback_content
            return {
                "modelId": final_model["id"],
                "content": append_notion_completeness_notice(fallback_content, retrieval_result),
                "sources": fallback_sources if request.showSources else [],
                "sourcesForFollowUps": fallback_sources,
                "retrieval": retrieval_result,
                "raw": {
                    "mode": "model_timeout_fallback",
                    "error": str(last_error),
                    "errorType": type(last_error).__name__,
                },
            }
        if isinstance(last_error, HTTPException):
            raise last_error
        if last_error is None:
            raise HTTPException(status_code=503, detail="답변을 생성할 모델의 API 키가 설정되지 않았습니다.")
        raise HTTPException(status_code=502, detail=f"Model connection failed: {last_error}")

    final_sources = build_model_evidence_sources(retrieval_result, active_limit)
    content = sanitize_customer_notice_answer(
        messages[-1].get("content", "") if messages else "",
        extract_model_content(data),
    )
    return {
        "modelId": final_model["id"],
        # 모델 스트림도 최종 검증 전까지 버퍼링되므로 저장·검증·전송에
        # 동일한 안내가 포함된다. 결정형 목록은 이 모델 경로를 거치지 않는다.
        "content": append_notion_completeness_notice(content or "응답 본문을 찾지 못했습니다.", retrieval_result),
        "sources": final_sources if request.showSources else [],
        "sourcesForFollowUps": final_sources,
        "retrieval": retrieval_result,
        "raw": data,
        "usedFallbackModel": final_model["id"] if final_model["id"] != model["id"] else None,
    }


def _read_model_response(
    response: requests.Response, model: Dict[str, Any], stream: bool,
    *, first_content_deadline: Optional[float] = None,
) -> Dict[str, Any]:
    has_content = False

    def lines():
        for raw_line in response.iter_lines(decode_unicode=True):
            checkpoint()
            # 역할/추론 청크만 계속 보내는 연결도 무한히 첫 답변을 미루지 않게 한다.
            # 블로킹된 읽기는 별도 stream read timeout으로 끝난다.
            if not has_content and first_content_deadline is not None and perf_counter() >= first_content_deadline:
                raise requests.Timeout("모델의 첫 답변 대기 시간을 초과했습니다.")
            yield (raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes)
                   else str(raw_line or "")).strip()

    if stream and is_ollama_model(model):
        streamed_content = ""
        ollama_stream_chunks: List[Dict[str, Any]] = []
        for line in lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            ollama_stream_chunks.append(chunk)
            message = chunk.get("message")
            delta = message.get("content") if isinstance(message, dict) else None
            if isinstance(delta, str) and delta:
                has_content = has_content or bool(delta.strip())
                streamed_content += delta
                # 본문은 최종 검증 전까지 보내지 않고 후보별로 보관한다.
        data = {
            "choices": [{"message": {"content": streamed_content}}],
            "streamChunks": len(ollama_stream_chunks),
            "provider": "ollama",
        }
    elif stream and "text/event-stream" in response.headers.get("content-type", "").lower():
        streamed_content = ""
        sse_stream_chunks: List[Dict[str, Any]] = []
        for line in lines():
            if not line or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            sse_stream_chunks.append(chunk)
            choices = chunk.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else None
            delta_message = choice.get("delta") if isinstance(choice, dict) else None
            delta = delta_message.get("content") if isinstance(delta_message, dict) else None
            if isinstance(delta, str) and delta:
                has_content = has_content or bool(delta.strip())
                streamed_content += delta
                # 본문은 최종 검증 전까지 보내지 않고 후보별로 보관한다.
        data = {
            "choices": [{"message": {"content": streamed_content}}],
            "streamChunks": len(sse_stream_chunks),
        }
    else:
        data = response.json()

    checkpoint()
    return data
