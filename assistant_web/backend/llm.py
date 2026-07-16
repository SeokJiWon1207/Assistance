"""모델 레지스트리와 채팅 모델 호출(Layer 2).

models.json 기반 모델 목록 로딩, OpenAI 호환/Ollama 네이티브 엔드포인트 호출,
요청 파라미터 → 생성 옵션 변환을 담당한다.

의존: config(CONFIG_DIR), common(parse_date, clamp_*), schemas(ChatRequest).
app.py는 `from backend.llm import *`로 이 이름들을 그대로 재노출한다.
"""

import json
import os
from datetime import date
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException

from backend.common import clamp_float, clamp_int, parse_date
from backend.config import CONFIG_DIR
from backend.schemas import ChatRequest


def get_model_registry() -> List[Dict[str, Any]]:
    registry_path = CONFIG_DIR / "models.json"
    if registry_path.exists():
        raw_models = json.loads(registry_path.read_text(encoding="utf-8"))
    else:
        raw_models = [
            {
                "id": "company-qwen3.6-35b-a3b",
                "label": "회사 Qwen 3.6 35B",
                "model": "qwen3.6-35b-a3b",
                "endpoint": os.getenv(
                    "COMPANY_MODEL_46_ENDPOINT",
                    "http://59.6.135.38:13025/v1/chat/completions",
                ),
                "apiKeyEnv": "COMPANY_MODEL_46_API_KEY",
                "think": False,
                "expiresAt": os.getenv("COMPANY_MODEL_46_EXPIRES_AT", ""),
                "enabled": True,
            }
        ]

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
        expires_at = parse_date(str(model.get("expiresAt", "")))
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
    content = (
        data.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
    )
    if not content:
        content = data.get("message", {}).get("content", "")
    return str(content or "")


def post_model_chat(
    model: Dict[str, Any],
    api_key: str,
    payload: Dict[str, Any],
    timeout: int,
    stream: bool = False,
    think: Optional[bool] = None,
) -> requests.Response:
    """Call an OpenAI-compatible model or Ollama's native chat endpoint."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if not is_ollama_model(model):
        remote_payload = {**payload}
        effective_think = model.get("think") if think is None else think
        if effective_think is not None:
            chat_template_kwargs = dict(remote_payload.get("chat_template_kwargs") or {})
            chat_template_kwargs["enable_thinking"] = bool(effective_think)
            remote_payload["chat_template_kwargs"] = chat_template_kwargs
        return requests.post(
            model["endpoint"],
            headers=headers,
            json=remote_payload,
            timeout=timeout,
            stream=stream,
        )

    option_names = {
        "temperature": "temperature",
        "top_p": "top_p",
        "top_k": "top_k",
        "min_p": "min_p",
        "max_tokens": "num_predict",
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
        timeout=timeout,
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
