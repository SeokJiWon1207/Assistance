"""에이전트 경로의 기능 플래그, 허용 모델 전용 전송 정책, 도구 호출 검증 기록.

전송 허용 여부는 provider 문자열이나 모델 이름으로 추정하지 않는다. 서버 환경변수의
허용 모델 ID와 실제 전송 주소의 origin이 모두 일치해야 하며, 도구 호출 지원은
레지스트리 선언(supportsTools)과 같은 구성으로 실제 호출을 확인한 검증 기록이 함께 필요하다.
"""

import hashlib
import json
import os
import tempfile
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date
from functools import partial
from pathlib import Path
from typing import Any, Dict, Iterator, Optional
from urllib.parse import urlsplit

from fastapi import HTTPException

from backend import llm
from backend.settings.paths import RUNTIME_DIR


VERIFICATION_FILENAME = "agent_tool_verification.json"
# 검증 기록에서 진입 판정에 반드시 필요한 확인 항목.
REQUIRED_CHECKS = ("tools_request_accepted", "tool_calls_returned", "arguments_json", "tool_roundtrip")


def _flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _number(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        return default
    return max(minimum, min(value, maximum))


def _identifiers(name: str) -> frozenset[str]:
    return frozenset(value.strip() for value in os.getenv(name, "").split(",") if value.strip())


@dataclass(frozen=True)
class AgentSettings:
    enabled: bool = False
    allowed_model_ids: frozenset[str] = frozenset()
    allowed_origins: frozenset[str] = frozenset()
    max_model_requests: int = 16
    max_tool_calls: int = 16
    max_calls_per_turn: int = 3
    max_concurrent_tools: int = 1
    model_timeout_seconds: float = 45.0
    tool_timeout_seconds: float = 20.0
    final_reserve_seconds: float = 45.0
    final_reserve_calls: int = 2
    min_start_seconds: float = 60.0
    max_tool_result_chars: int = 6000
    max_context_chars: int = 60000
    max_invalid_turns: int = 1
    max_output_tokens: int = 1024
    verification_max_age_days: int = 30

    @classmethod
    def from_env(cls) -> "AgentSettings":
        return cls(
            enabled=_flag("AGENT_MODE_ENABLED"),
            allowed_model_ids=_identifiers("AGENT_ALLOWED_MODEL_IDS"),
            allowed_origins=frozenset(endpoint_origin(value) for value in _identifiers("AGENT_ALLOWED_ENDPOINT_ORIGINS")),
            max_model_requests=int(_number("AGENT_MAX_MODEL_REQUESTS", 16, 3, 20)),
            max_tool_calls=int(_number("AGENT_MAX_TOOL_CALLS", 16, 1, 20)),
            max_calls_per_turn=int(_number("AGENT_MAX_CALLS_PER_TURN", 3, 1, 5)),
            max_concurrent_tools=int(_number("AGENT_MAX_CONCURRENT_TOOLS", 1, 1, 2)),
            model_timeout_seconds=_number("AGENT_MODEL_TIMEOUT_SECONDS", 45, 5, 120),
            tool_timeout_seconds=_number("AGENT_TOOL_TIMEOUT_SECONDS", 20, 3, 60),
            final_reserve_seconds=_number("AGENT_FINAL_RESERVE_SECONDS", 45, 10, 120),
            # 인용 검증 경로를 끝내는 최소 호출(선택·검토)보다 작게 둘 수 없다.
            final_reserve_calls=int(_number("AGENT_FINAL_RESERVE_CALLS", 2, 2, 6)),
            min_start_seconds=_number("AGENT_MIN_START_SECONDS", 60, 20, 300),
            max_tool_result_chars=int(_number("AGENT_MAX_TOOL_RESULT_CHARS", 6000, 1000, 20000)),
            max_context_chars=int(_number("AGENT_MAX_CONTEXT_CHARS", 60000, 8000, 200000)),
            max_invalid_turns=int(_number("AGENT_MAX_INVALID_TURNS", 1, 0, 2)),
            max_output_tokens=int(_number("AGENT_MAX_OUTPUT_TOKENS", 1024, 256, 4096)),
            verification_max_age_days=int(_number("AGENT_TOOL_VERIFICATION_MAX_AGE_DAYS", 30, 1, 365)),
        )


def endpoint_origin(url: str) -> str:
    parts = urlsplit(str(url or "").strip())
    if not parts.scheme or not parts.hostname:
        return ""
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme.lower()}://{parts.hostname.lower()}{port}"


def send_url(model: Dict[str, Any]) -> str:
    """post_model_chat이 실제로 요청을 보내는 주소."""
    if llm.is_ollama_model(model):
        return str(model.get("nativeEndpoint", "http://127.0.0.1:11434/api/chat"))
    return str(model.get("endpoint") or "")


def transfer_allowed(model: Dict[str, Any], settings: AgentSettings) -> bool:
    origin = endpoint_origin(send_url(model))
    return bool(model.get("id") in settings.allowed_model_ids and origin and origin in settings.allowed_origins)


def config_fingerprint(model: Dict[str, Any]) -> str:
    """도구 호출 동작에 영향을 주는 레지스트리 구성. 바뀌면 다시 검증해야 한다."""
    material = {
        "id": model.get("id"), "model": model.get("model", model.get("id")),
        "provider": str(model.get("provider", "")).strip().lower(),
        "url": send_url(model), "think": model.get("think"),
        "reasoningEffort": model.get("reasoningEffort"), "maxOutputTokens": model.get("maxOutputTokens"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]


def verification_path() -> Path:
    return RUNTIME_DIR / VERIFICATION_FILENAME


def load_verification(model_id: str) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(verification_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    record = (payload.get("models") or {}).get(model_id) if isinstance(payload, dict) else None
    return record if isinstance(record, dict) else None


def save_verification(model_id: str, record: Dict[str, Any]) -> Path:
    path = verification_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), dict):
        payload = {"version": 1, "models": {}}
    payload["models"][model_id] = record
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        temporary = Path(handle.name)
    temporary.replace(path)
    return path


def mark_reverification_required(model_id: str, reason: str) -> None:
    """실행 중 서빙 구성 변경을 감지하면 다음 요청부터 검증 전까지 후보에서 제외한다."""
    record = load_verification(model_id)
    if record and not record.get("reverificationRequired"):
        try:
            save_verification(model_id, {**record, "reverificationRequired": True, "reverificationReason": reason})
        except OSError:
            pass


def tool_support(model: Dict[str, Any], settings: AgentSettings, *, today: Optional[date] = None) -> tuple[bool, str]:
    """문서·이름이 아니라 같은 구성에서 실제 호출로 확인한 기록만 지원으로 인정한다."""
    if model.get("supportsTools") is not True:
        return False, "not_declared"
    record = load_verification(str(model.get("id") or ""))
    if not record:
        return False, "not_verified"
    if record.get("result") != "supported" or not all((record.get("checks") or {}).get(name) is True
                                                       for name in REQUIRED_CHECKS):
        return False, "verification_failed"
    if record.get("configFingerprint") != config_fingerprint(model) or record.get("reverificationRequired"):
        return False, "reverification_required"
    try:
        verified = date.fromisoformat(str(record.get("verifiedAt") or "")[:10])
    except ValueError:
        return False, "not_verified"
    if ((today or date.today()) - verified).days > settings.verification_max_age_days:
        return False, "verification_expired"
    return True, "verified"


@dataclass(frozen=True)
class AgentPolicy:
    settings: AgentSettings
    model_id: str

    def allows(self, model: Dict[str, Any]) -> bool:
        return transfer_allowed(model, self.settings)


def agent_candidate(model_id: str, settings: Optional[AgentSettings] = None) -> Optional[AgentPolicy]:
    """기능 플래그·허용 목록·검증 기록을 모두 통과한 모델만 에이전트 후보로 본다. 네트워크를 쓰지 않는다."""
    settings = settings or AgentSettings.from_env()
    if not settings.enabled:
        return None
    try:
        model = llm.get_private_model(model_id)
    except HTTPException:
        return None
    if model.get("status") != "active" or not llm.get_model_api_key(model) or not transfer_allowed(model, settings):
        return None
    supported, _reason = tool_support(model, settings)
    if not supported:
        return None
    return AgentPolicy(settings, model["id"])


_ACTIVE: ContextVar[Optional[AgentPolicy]] = ContextVar("agent_request_policy", default=None)


@contextmanager
def request_transfer_scope(model_id: str) -> Iterator[Optional[AgentPolicy]]:
    """플래너부터 최종 응답까지 허용 목록을 강제한다. 도구 검증은 루프 진입만 결정한다."""
    settings = AgentSettings.from_env()
    if not settings.enabled:
        yield None
        return
    policy = agent_candidate(model_id, settings)
    token = _ACTIVE.set(policy)
    try:
        with llm.model_transfer_scope(partial(transfer_allowed, settings=settings)):
            yield policy
    finally:
        _ACTIVE.reset(token)


def active_agent_policy() -> Optional[AgentPolicy]:
    return _ACTIVE.get()
