"""Firebase 이메일 로그인, 세션 쿠키, 관리자 초대 API."""

from __future__ import annotations

import re
import secrets
from typing import Any, Dict, Literal, Optional

import requests
from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from backend.auth import (
    VALID_ROLES,
    approve_login_claims,
    auth_cookie_secure,
    csrf_cookie_name,
    current_request_user,
    firebase_auth_enabled,
    firebase_invite_user,
    firebase_web_api_key,
    get_user_by_uid,
    list_users,
    revoke_firebase_sessions,
    session_cookie_name,
    session_ttl_seconds,
    update_user_access,
    upsert_invited_user,
    create_firebase_session,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class SessionLoginRequest(BaseModel):
    idToken: str = Field(min_length=20, max_length=10000)


class PasswordResetRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class UserInviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    displayName: str = Field(default="", max_length=100)
    role: Literal["member", "manager", "admin"] = "member"
    mantisUsername: str = Field(default="", max_length=100)
    team: str = Field(default="", max_length=100)


class UserAccessUpdateRequest(BaseModel):
    displayName: Optional[str] = Field(default=None, max_length=100)
    role: Optional[Literal["member", "manager", "admin"]] = None
    mantisUsername: Optional[str] = Field(default=None, max_length=100)
    team: Optional[str] = Field(default=None, max_length=100)
    approved: Optional[bool] = None


def _validate_email(email: str) -> str:
    normalized = email.strip().casefold()
    if not EMAIL_PATTERN.match(normalized):
        raise HTTPException(status_code=422, detail="올바른 이메일 주소를 입력해 주세요.")
    return normalized


def _set_csrf_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        csrf_cookie_name(),
        token,
        max_age=session_ttl_seconds(),
        secure=auth_cookie_secure(),
        httponly=False,
        samesite="lax",
        path="/",
    )


def _require_csrf(request: Request, token: str) -> None:
    cookie_token = request.cookies.get(csrf_cookie_name(), "")
    if not cookie_token or not token or not secrets.compare_digest(cookie_token, token):
        raise HTTPException(status_code=403, detail="요청 보안 토큰이 올바르지 않습니다.")


@router.get("/config")
def auth_config(request: Request, response: Response) -> Dict[str, Any]:
    csrf_token = request.cookies.get(csrf_cookie_name()) or secrets.token_urlsafe(32)
    _set_csrf_cookie(response, csrf_token)
    enabled = firebase_auth_enabled()
    return {
        "enabled": enabled,
        "inviteOnly": True,
        "provider": "firebase-password" if enabled else "disabled",
        "firebaseApiKey": firebase_web_api_key() if enabled else "",
        "csrfCookie": csrf_cookie_name(),
        "sessionHours": session_ttl_seconds() // 3600,
    }


@router.post("/session")
def create_session(
    payload: SessionLoginRequest,
    request: Request,
    response: Response,
    x_csrf_token: str = Header(default=""),
) -> Dict[str, Any]:
    if not firebase_auth_enabled():
        raise HTTPException(status_code=404, detail="로그인 기능이 비활성화되어 있습니다.")
    _require_csrf(request, x_csrf_token)
    try:
        session_cookie, claims = create_firebase_session(payload.idToken)
    except PermissionError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=401, detail="이메일 또는 비밀번호를 확인해 주세요.") from exc

    user = approve_login_claims(claims)
    if not user:
        raise HTTPException(
            status_code=403,
            detail="관리자가 초대한 계정이 아닙니다. 관리자에게 사용 승인을 요청해 주세요.",
        )
    response.set_cookie(
        session_cookie_name(),
        session_cookie,
        max_age=session_ttl_seconds(),
        secure=auth_cookie_secure(),
        httponly=True,
        samesite="lax",
        path="/",
    )
    _set_csrf_cookie(response, secrets.token_urlsafe(32))
    return {"authenticated": True, "user": user}


@router.get("/me")
def read_current_user(request: Request) -> Dict[str, Any]:
    user = current_request_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return {"authenticated": True, "user": user}


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    x_csrf_token: str = Header(default=""),
) -> Dict[str, bool]:
    _require_csrf(request, x_csrf_token)
    response.delete_cookie(session_cookie_name(), path="/", secure=auth_cookie_secure())
    response.delete_cookie(csrf_cookie_name(), path="/", secure=auth_cookie_secure())
    return {"authenticated": False}


@router.post("/password-reset")
def send_password_reset(
    payload: PasswordResetRequest,
    request: Request,
    x_csrf_token: str = Header(default=""),
) -> Dict[str, bool]:
    """계정 존재 여부를 노출하지 않고 Firebase 기본 재설정 메일을 요청한다."""
    _require_csrf(request, x_csrf_token)
    email = _validate_email(payload.email)
    api_key = firebase_web_api_key()
    if not firebase_auth_enabled() or not api_key:
        raise HTTPException(status_code=503, detail="Firebase 이메일 로그인이 설정되지 않았습니다.")
    try:
        requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key={api_key}",
            json={"requestType": "PASSWORD_RESET", "email": email},
            headers={"X-Firebase-Locale": "ko"},
            timeout=12,
        )
    except requests.RequestException:
        # 계정 존재 여부와 외부 오류 세부를 로그인 화면에 노출하지 않는다.
        pass
    return {"accepted": True}


@router.get("/users")
def read_users() -> Dict[str, Any]:
    return {"users": list_users(), "roles": sorted(VALID_ROLES)}


@router.post("/users/invite")
def invite_user(payload: UserInviteRequest, request: Request) -> Dict[str, Any]:
    email = _validate_email(payload.email)
    admin = current_request_user(request)
    if not admin or admin["role"] != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    try:
        uid, setup_url, firebase_created = firebase_invite_user(email, payload.displayName)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Firebase 초대 링크를 만들지 못했습니다.") from exc
    user = upsert_invited_user(
        uid,
        email,
        payload.displayName or email.split("@", 1)[0],
        payload.role,
        payload.mantisUsername,
        payload.team,
        admin["email"],
    )
    return {
        "user": user,
        "setupUrl": setup_url,
        "firebaseAccountCreated": firebase_created,
        "message": "초대 링크를 사용자에게 안전하게 전달해 주세요.",
    }


@router.patch("/users/{uid}")
def patch_user(uid: str, payload: UserAccessUpdateRequest, request: Request) -> Dict[str, Any]:
    admin = current_request_user(request)
    if not admin or admin["role"] != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    current = get_user_by_uid(uid)
    if not current:
        raise HTTPException(status_code=404, detail="사용자를 찾지 못했습니다.")
    if uid == admin["uid"] and (
        payload.approved is False
        or (payload.role is not None and payload.role != admin["role"])
    ):
        raise HTTPException(
            status_code=400,
            detail="현재 로그인한 관리자 계정의 권한이나 승인을 변경할 수 없습니다.",
        )
    user = update_user_access(
        uid,
        role=payload.role,
        approved=payload.approved,
        display_name=payload.displayName,
        mantis_username=payload.mantisUsername,
        team=payload.team,
    )
    if payload.approved is False:
        try:
            revoke_firebase_sessions(uid)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="권한은 해제했지만 Firebase 세션 폐기에 실패했습니다.") from exc
    return {"user": user}
