"""운영 기동 전에 인증·전송 보안 설정을 검증한다."""

from __future__ import annotations

import ipaddress
import logging
import os
from datetime import date
from typing import Any, Dict
from urllib.parse import urlsplit

from backend.auth import auth_cookie_secure, firebase_auth_enabled
from backend.llm import get_model_registry


LOGGER = logging.getLogger(__name__)
LOCAL_DEVELOPMENT_ORIGINS = (
    "http://localhost:8787",
    "http://127.0.0.1:8787",
)


def is_production() -> bool:
    return os.getenv("APP_ENV", "development").strip().lower() in {"prod", "production"}


def _origin(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"올바르지 않은 URL입니다: {value!r}")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = 80 if parsed.scheme == "http" else 443
    port = parsed.port or default_port
    suffix = "" if port == default_port else f":{port}"
    return f"{parsed.scheme}://{host}{suffix}"


def _is_loopback(hostname: str) -> bool:
    normalized = str(hostname or "").strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def get_cors_allowed_origins() -> list[str]:
    """환경에 맞는 credential CORS origin만 반환한다.

    운영에서는 검증된 공개 주소만 허용하고, 로컬 주소는 개발 환경에서만
    유지한다. PUBLIC_BASE_URL의 경로·기본 포트는 origin 형태로 정규화한다.
    """
    origins = [] if is_production() else list(LOCAL_DEVELOPMENT_ORIGINS)
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    if public_base_url:
        public_origin = _origin(public_base_url)
        if not is_production() or public_origin.startswith("https://"):
            origins.append(public_origin)
    return list(dict.fromkeys(origins))


def validate_auth_transport_security() -> None:
    """운영 인증은 HTTPS와 Secure 쿠키 없이는 시작하지 않는다."""
    if not is_production():
        return
    if not firebase_auth_enabled():
        raise RuntimeError("운영 환경에서는 FIREBASE_AUTH_ENABLED=true가 필요합니다.")
    if not auth_cookie_secure():
        raise RuntimeError("운영 환경에서는 AUTH_COOKIE_SECURE=true가 필요합니다.")
    public_base_url = os.getenv("PUBLIC_BASE_URL", "").strip()
    if not public_base_url or urlsplit(public_base_url).scheme.lower() != "https":
        raise RuntimeError("운영 환경에서는 https:// PUBLIC_BASE_URL이 필요합니다.")


def validate_model_endpoint_security() -> None:
    """원격 HTTP 모델은 정확한 origin과 만료일이 있는 임시 예외만 허용한다."""
    if not is_production():
        return

    raw_allowlist = os.getenv("MODEL_ENDPOINT_INSECURE_ALLOWLIST", "")
    if "*" in raw_allowlist:
        raise RuntimeError("MODEL_ENDPOINT_INSECURE_ALLOWLIST에는 wildcard를 사용할 수 없습니다.")
    allowlist = {_origin(item) for item in raw_allowlist.split(",") if item.strip()}

    expires_raw = os.getenv("MODEL_ENDPOINT_INSECURE_ALLOWLIST_EXPIRES_AT", "").strip()
    expires_at = None
    if allowlist:
        if not expires_raw:
            raise RuntimeError("HTTP 모델 allowlist에는 만료일이 필요합니다.")
        try:
            expires_at = date.fromisoformat(expires_raw)
        except ValueError as exc:
            raise RuntimeError("HTTP 모델 allowlist 만료일은 YYYY-MM-DD 형식이어야 합니다.") from exc
        if expires_at < date.today():
            raise RuntimeError("HTTP 모델 allowlist가 만료되었습니다.")
        days_remaining = (expires_at - date.today()).days
        try:
            warning_days = max(
                1,
                min(
                    int(os.getenv("MODEL_ENDPOINT_INSECURE_ALLOWLIST_WARNING_DAYS", "30")),
                    365,
                ),
            )
        except ValueError:
            warning_days = 30
        if days_remaining <= warning_days:
            LOGGER.warning(
                "HTTP 모델 예외 만료 임박: expires_at=%s days_remaining=%s; 회사 모델 TLS 전환을 확인하세요.",
                expires_at,
                days_remaining,
            )

    for model in get_model_registry():
        if not model.get("enabled", True):
            continue
        endpoint = str(model.get("endpoint") or "").strip()
        parsed = urlsplit(endpoint)
        if parsed.scheme.lower() != "http" or _is_loopback(parsed.hostname or ""):
            continue
        origin = _origin(endpoint)
        if origin not in allowlist:
            raise RuntimeError(
                f"원격 HTTP 모델 endpoint는 허용되지 않습니다: {model.get('id')} ({origin})"
            )
        LOGGER.warning(
            "임시 HTTP 모델 예외 사용: model=%s origin=%s expires_at=%s",
            model.get("id"),
            origin,
            expires_at,
        )


def get_model_transport_security_status(today: date | None = None) -> Dict[str, Any]:
    """Readiness/모니터링용 HTTP 모델 예외 상태를 비밀값 없이 반환한다."""
    current_day = today or date.today()
    remote_http_models = []
    for model in get_model_registry():
        if not model.get("enabled", True):
            continue
        endpoint = str(model.get("endpoint") or "").strip()
        parsed = urlsplit(endpoint)
        if parsed.scheme.lower() == "http" and not _is_loopback(parsed.hostname or ""):
            remote_http_models.append((str(model.get("id") or "unknown"), endpoint))

    base: Dict[str, Any] = {
        "enforced": is_production(),
        "exceptionModelIds": [model_id for model_id, _endpoint in remote_http_models],
        "expiresAt": None,
        "daysRemaining": None,
    }
    if not remote_http_models:
        return {**base, "status": "not_applicable", "label": "HTTP 원격 모델 없음"}

    raw_allowlist = os.getenv("MODEL_ENDPOINT_INSECURE_ALLOWLIST", "")
    if "*" in raw_allowlist:
        return {**base, "status": "error", "label": "HTTP 모델 예외 wildcard 금지"}
    try:
        allowlist = {_origin(item) for item in raw_allowlist.split(",") if item.strip()}
        unapproved = [
            model_id
            for model_id, endpoint in remote_http_models
            if _origin(endpoint) not in allowlist
        ]
    except (RuntimeError, ValueError):
        return {**base, "status": "error", "label": "HTTP 모델 예외 URL 오류"}
    if unapproved:
        return {
            **base,
            "status": "error",
            "label": "허용되지 않은 HTTP 모델 endpoint",
            "unapprovedModelIds": unapproved,
        }

    expires_raw = os.getenv("MODEL_ENDPOINT_INSECURE_ALLOWLIST_EXPIRES_AT", "").strip()
    try:
        expires_at = date.fromisoformat(expires_raw)
    except ValueError:
        return {**base, "status": "error", "label": "HTTP 모델 예외 만료일 오류"}
    days_remaining = (expires_at - current_day).days
    try:
        warning_days = max(
            1,
            min(
                int(os.getenv("MODEL_ENDPOINT_INSECURE_ALLOWLIST_WARNING_DAYS", "30")),
                365,
            ),
        )
    except ValueError:
        warning_days = 30
    status = "expired" if days_remaining < 0 else "warning" if days_remaining <= warning_days else "ok"
    label = (
        "HTTP 모델 예외 만료"
        if status == "expired"
        else "HTTP 모델 예외 만료 임박"
        if status == "warning"
        else "임시 HTTP 모델 예외 사용 중"
    )
    return {
        **base,
        "status": status,
        "label": label,
        "expiresAt": expires_at.isoformat(),
        "daysRemaining": days_remaining,
        "warningDays": warning_days,
    }


def validate_runtime_security() -> None:
    """서로 독립된 인증 경계와 모델 전송 경계를 순서대로 검증한다."""
    validate_auth_transport_security()
    validate_model_endpoint_security()
