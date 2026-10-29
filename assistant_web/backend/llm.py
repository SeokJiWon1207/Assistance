"""모델 레지스트리와 채팅 모델 호출(Layer 2).

models.json 기반 모델 목록 로딩, OpenAI 호환/Ollama 네이티브 엔드포인트 호출,
요청 파라미터 → 생성 옵션 변환을 담당한다.

의존: config(CONFIG_DIR), common(parse_date, clamp_*), schemas(ChatRequest).
"""

import json
import logging
import math
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from threading import Lock
from typing import Any, Dict, Iterator, List, Optional

import requests
from fastapi import HTTPException

from backend.common import clamp_float, clamp_int, parse_date
from backend.config import CONFIG_DIR
from backend.execution import RequestStopped, bounded_timeout, checkpoint
from backend.schemas import ChatRequest


LOGGER = logging.getLogger(__name__)


def _default_model_registry() -> List[Dict[str, Any]]:
    return [
        {
            "id": "gemini-3.1-flash-lite",
            "label": "Gemini 3.1 Flash-Lite",
            "model": "gemini-3.1-flash-lite",
            "provider": "gemini",
            "endpoint": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
            "apiKeyEnv": "GEMINI_API_KEY",
            "reasoningEffort": "minimal",
            "maxOutputTokens": 2048,
            "expiresAt": "",
            "enabled": True,
        },
        {
            "id": "company-qwen3.6-35b-a3b",
            "label": "회사 Qwen 3.6 35B",
            "model": "qwen3.6-35b-a3b",
            "provider": "openai-compatible",
            "endpointEnv": "COMPANY_MODEL_46_ENDPOINT",
            "apiKeyEnv": "COMPANY_MODEL_46_API_KEY",
            "think": False,
            "expiresAt": os.getenv("COMPANY_MODEL_46_EXPIRES_AT", ""),
            "enabled": True,
        },
        {
            "id": "qwen3.5-9b-local",
            "label": "Qwen 3.5 9B (로컬)",
            "model": "qwen3.5:9b",
            "provider": "ollama",
            "endpoint": "http://127.0.0.1:11434/v1/chat/completions",
            "nativeEndpoint": "http://127.0.0.1:11434/api/chat",
            "think": False,
            "expiresAt": "",
            "enabled": True,
        },
    ]


def get_model_registry() -> List[Dict[str, Any]]:
    registry_path = CONFIG_DIR / "models.json"
    if registry_path.exists():
        try:
            loaded = json.loads(registry_path.read_text(encoding="utf-8"))
            if not isinstance(loaded, list) or not all(isinstance(item, dict) for item in loaded):
                raise ValueError("models.json root must be a list of objects")
            raw_models = loaded
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            LOGGER.warning("models.json을 읽지 못해 기본 모델 목록을 사용합니다: %s", exc)
            raw_models = _default_model_registry()
    else:
        raw_models = _default_model_registry()

    today = date.today()
    models = []
    for model in raw_models:
        endpoint_env = str(model.get("endpointEnv", "")).strip()
        resolved_endpoint = (
            os.getenv(endpoint_env, "").strip()
            if endpoint_env
            else str(model.get("endpoint", "")).strip()
        )
        resolved_model = {**model}
        if resolved_endpoint:
            resolved_model["endpoint"] = resolved_endpoint
        expires_at_env = str(model.get("expiresAtEnv", "")).strip()
        resolved_expires_at = (
            os.getenv(expires_at_env, str(model.get("expiresAt", ""))).strip()
            if expires_at_env
            else str(model.get("expiresAt", "")).strip()
        )
        resolved_model["expiresAt"] = resolved_expires_at
        expires_at = parse_date(resolved_expires_at)
        api_key = os.getenv(str(model.get("apiKeyEnv", "")), "").strip()
        is_local_ollama = str(model.get("provider", "")).strip().lower() == "ollama"
        has_key = is_local_ollama or (bool(api_key) and not api_key.startswith("replace_with_"))
        expired = bool(expires_at and expires_at < today)
        models.append(
            {
                **resolved_model,
                "hasKey": has_key,
                "expired": expired,
                "status": "expired"
                if expired
                else "missing_key"
                if not has_key
                else "active"
                if model.get("enabled", True)
                else "disabled",
            }
        )
    return models


def is_ollama_model(model: Dict[str, Any]) -> bool:
    return str(model.get("provider", "")).strip().lower() == "ollama"


def get_model_api_key(model: Dict[str, Any]) -> str:
    if is_ollama_model(model):
        return "ollama"
    return os.getenv(str(model.get("apiKeyEnv", "")), "").strip()


def extract_model_content(data: Dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content:
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, str) else ""


class ModelCircuitOpenError(requests.ConnectionError):
    """연속 장애로 차단된 모델 엔드포인트 호출을 네트워크 대기 없이 실패시킨다."""


# 게이트웨이가 모델 서버에 닿지 못했다는 응답만 장애로 본다.
_MODEL_UNAVAILABLE_STATUSES = {502, 503, 504}
_model_circuit_lock = Lock()


@dataclass
class _ModelCircuitState:
    failures: int = 0
    open_until: float = 0.0
    probe_in_flight: bool = False
    generation: int = 0


_model_circuits: Dict[str, _ModelCircuitState] = {}
_ModelCircuitCall = tuple[str, _ModelCircuitState, int]


def _model_circuit_setting(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        return clamp_int(os.getenv(name, str(default)), minimum, maximum) or default
    except ValueError:
        return default


def _model_circuit_threshold() -> int:
    return _model_circuit_setting("MODEL_CIRCUIT_FAILURE_THRESHOLD", 2, 1, 10)


def _model_circuit_cooldown() -> int:
    return _model_circuit_setting("MODEL_CIRCUIT_COOLDOWN_SECONDS", 60, 5, 600)


def _model_circuit_key(model: Dict[str, Any]) -> str:
    if is_ollama_model(model):
        return str(model.get("nativeEndpoint", "http://127.0.0.1:11434/api/chat"))
    return str(model.get("endpoint", ""))


def _enter_model_circuit(key: str) -> _ModelCircuitCall:
    now = time.monotonic()
    with _model_circuit_lock:
        state = _model_circuits.setdefault(key, _ModelCircuitState())
        if state.failures < _model_circuit_threshold():
            return key, state, state.generation
        if state.probe_in_flight:
            raise ModelCircuitOpenError("모델 엔드포인트의 복구 확인 요청이 진행 중입니다.")
        if now < state.open_until:
            raise ModelCircuitOpenError(
                f"모델 엔드포인트가 연속 장애로 {math.ceil(state.open_until - now)}초간 차단되었습니다."
            )
        # 반개방: 이 호출 하나만 회복 확인용으로 보내고 그동안 다른 호출은 계속 차단한다.
        state.probe_in_flight = True
        state.generation += 1
        return key, state, state.generation


def _record_model_result(call: _ModelCircuitCall, available: Optional[bool]) -> None:
    key, state, generation = call
    with _model_circuit_lock:
        # 차단 전에 시작한 느린 요청이 새 차단이나 복구 확인 상태를 덮어쓰지 않는다.
        if _model_circuits.get(key) is not state or state.generation != generation:
            return
        was_probe = state.probe_in_flight
        state.probe_in_flight = False
        if available:
            if state.failures >= _model_circuit_threshold():
                LOGGER.info("모델 엔드포인트 차단을 해제합니다.")
            state.failures = 0
            state.open_until = 0.0
            if was_probe:
                state.generation += 1
            return
        # 파싱 오류·취소는 장애 횟수에 넣지 않되 복구 확인 잠금은 반드시 해제한다.
        if available is False:
            state.failures += 1
        if state.failures >= _model_circuit_threshold():
            if state.failures == _model_circuit_threshold() and available is False:
                LOGGER.warning("모델 엔드포인트가 연속 %d회 응답하지 않아 호출을 차단합니다.", state.failures)
            state.open_until = time.monotonic() + _model_circuit_cooldown()
            state.generation += 1


def model_circuit_status_for(model: Dict[str, Any]) -> Dict[str, Any]:
    """엔드포인트 주소를 노출하지 않고 모델의 차단 상태를 돌려준다."""
    with _model_circuit_lock:
        state = _model_circuits.get(_model_circuit_key(model), _ModelCircuitState())
        failures, open_until = state.failures, state.open_until
    is_open = failures >= _model_circuit_threshold()
    return {
        "id": model["id"],
        "circuit": "open" if is_open else "closed",
        "consecutiveFailures": failures,
        "retryAfterSeconds": max(0, math.ceil(open_until - time.monotonic())) if is_open else 0,
    }


def model_circuit_status() -> List[Dict[str, Any]]:
    return [model_circuit_status_for(model) for model in get_model_registry()]


def reset_model_circuits() -> None:
    with _model_circuit_lock:
        _model_circuits.clear()


@contextmanager
def consume_model_response(response: requests.Response) -> Iterator[None]:
    """스트리밍 응답은 헤더가 아니라 본문 소비가 끝날 때 서킷 결과를 확정한다."""
    call = vars(response).pop("_model_circuit_call", None)
    available: Optional[bool] = None
    try:
        yield
        checkpoint()
        available = True
    except (requests.Timeout, requests.ConnectionError, requests.exceptions.ChunkedEncodingError):
        checkpoint()
        available = False
        raise
    finally:
        if call is not None:
            _record_model_result(call, available)
        response.close()


def post_model_chat(
    model: Dict[str, Any],
    api_key: str,
    payload: Dict[str, Any],
    timeout: int,
    stream: bool = False,
    think: Optional[bool] = None,
) -> requests.Response:
    """Call a model endpoint, failing fast while its circuit is open.

    응답 없는 엔드포인트를 매 요청마다 타임아웃까지 기다리지 않도록 연속 장애 시
    일정 시간 호출을 차단한다. 차단 예외는 ``requests.ConnectionError`` 이므로
    호출부의 기존 규칙 기반·대체 모델 폴백이 그대로 적용된다.
    """
    checkpoint()
    call = _enter_model_circuit(_model_circuit_key(model))
    try:
        response = _send_model_chat(model, api_key, payload, timeout, stream, think)
        try:
            checkpoint()
        except RequestStopped:
            response.close()
            raise
    except (requests.Timeout, requests.ConnectionError):
        try:
            checkpoint()
        except RequestStopped:
            _record_model_result(call, available=None)
            raise
        _record_model_result(call, available=False)
        raise
    except BaseException:
        _record_model_result(call, available=None)
        raise
    if stream and response.status_code < 400:
        # 본문을 읽는 호출부는 consume_model_response 안에서 처리해야 한다.
        vars(response)["_model_circuit_call"] = call
    else:
        _record_model_result(call, available=response.status_code not in _MODEL_UNAVAILABLE_STATUSES)
    return response


def _send_model_chat(
    model: Dict[str, Any],
    api_key: str,
    payload: Dict[str, Any],
    timeout: int,
    stream: bool,
    think: Optional[bool],
) -> requests.Response:
    """Call an OpenAI-compatible model or Ollama's native chat endpoint."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if not is_ollama_model(model):
        remote_payload = {**payload}
        effective_think = model.get("think") if think is None else think
        if str(model.get("provider", "")).strip().lower() == "gemini":
            # 회사 모델 전용 옵션은 Gemini의 OpenAI 호환 API에 전달하지 않는다.
            for name in ("seed", "top_k", "min_p", "repetition_penalty", "chat_template_kwargs"):
                remote_payload.pop(name, None)
            remote_payload["reasoning_effort"] = model.get("reasoningEffort", "low")
            output_limit = clamp_int(model.get("maxOutputTokens"), 1, 32768) or 2048
            requested_limit = clamp_int(remote_payload.get("max_tokens"), 1, 32768) or output_limit
            remote_payload["max_tokens"] = min(requested_limit, output_limit)
        elif effective_think is not None:
            chat_template_kwargs = dict(remote_payload.get("chat_template_kwargs") or {})
            chat_template_kwargs["enable_thinking"] = bool(effective_think)
            remote_payload["chat_template_kwargs"] = chat_template_kwargs
        response = requests.post(
            model["endpoint"],
            headers=headers,
            json=remote_payload,
            timeout=bounded_timeout(timeout),
            stream=stream,
        )

        if str(model.get("provider", "")).strip().lower() == "gemini":
            # SSE는 UTF-8이다. charset이 없을 때 requests의 Latin-1 기본값을 쓰지 않는다.
            response.encoding = "utf-8"
        return response

    option_names = {
        "temperature": "temperature",
        "top_p": "top_p",
        "top_k": "top_k",
        "min_p": "min_p",
        "max_tokens": "num_predict",
        "num_ctx": "num_ctx",
        "repetition_penalty": "repeat_penalty",
        "seed": "seed",
        "stop": "stop",
    }
    options = {
        target: payload[source]
        for source, target in option_names.items()
        if payload.get(source) is not None
    }
    native_payload: Dict[str, Any] = {
        "model": payload.get("model", model.get("model", model["id"])),
        "messages": payload.get("messages", []),
        "stream": stream,
        "think": bool(model.get("think", False) if think is None else think),
    }
    if options:
        native_payload["options"] = options
    if payload.get("response_format", {}).get("type") == "json_object":
        native_payload["format"] = "json"

    return requests.post(
        model.get("nativeEndpoint", "http://127.0.0.1:11434/api/chat"),
        headers=headers,
        json=native_payload,
        timeout=bounded_timeout(timeout),
        stream=stream,
    )


def public_model(model: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": model["id"],
        "label": model["label"],
        "model": model.get("model", model["id"]),
        "expiresAt": model.get("expiresAt", ""),
        "enabled": bool(model.get("enabled", True)),
        "status": model["status"],
        "expired": model["expired"],
        "hasKey": model["hasKey"],
    }


def get_private_model(model_id: str) -> Dict[str, Any]:
    for model in get_model_registry():
        if model["id"] == model_id:
            return model
    raise HTTPException(status_code=404, detail="Unknown model")


def build_generation_options(request: ChatRequest) -> Dict[str, Any]:
    options: Dict[str, Any] = {}
    optional_values = {
        "top_p": clamp_float(request.topP, 0, 1),
        "top_k": clamp_int(request.topK, 1, 500),
        "min_p": clamp_float(request.minP, 0, 1),
        "max_tokens": clamp_int(request.maxTokens, 1, 32768),
        "presence_penalty": clamp_float(request.presencePenalty, -2, 2),
        "frequency_penalty": clamp_float(request.frequencyPenalty, -2, 2),
        "repetition_penalty": clamp_float(request.repetitionPenalty, 0.1, 3),
        "seed": request.seed,
    }
    for key, value in optional_values.items():
        if value is not None:
            options[key] = value

    if request.stop:
        stop_values = [value for value in request.stop if value.strip()]
        if stop_values:
            options["stop"] = stop_values[:8]

    if request.responseFormat == "json":
        options["response_format"] = {"type": "json_object"}

    return options
