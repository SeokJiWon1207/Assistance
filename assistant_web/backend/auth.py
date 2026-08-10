"""Firebase Authentication과 앱 사용자 권한을 연결하는 인증 계층.

브라우저는 Firebase 이메일/비밀번호 인증으로 얻은 ID 토큰을 한 번만 서버에
전달한다. 서버는 이를 Firebase 세션 쿠키로 교환하고, 이후 요청은 HttpOnly
쿠키와 로컬 승인 사용자 DB를 함께 검증한다.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from backend.config import RUNTIME_DIR
from backend.db_migrations import run_sqlite_migrations


VALID_ROLES = {"member", "manager", "admin"}
PUBLIC_API_PATHS = {
    "/api/auth/config",
    "/api/auth/session",
    "/api/auth/logout",
    "/api/auth/password-reset",
}
ADMIN_API_PREFIXES = (
    "/api/validation/",
    "/api/evaluation/",
    "/api/accuracy/",
    "/api/auth/users",
)
ADMIN_API_EXACT_PATHS = {
    "/api/feedback/summary",
    "/api/validation/logs",
    "/api/validation/summary",
    "/api/evaluation/cases",
    "/api/evaluation/history",
}


class AuthConfigurationError(RuntimeError):
    """Firebase 서버 설정이 빠졌거나 잘못된 경우."""


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def firebase_auth_enabled() -> bool:
    return _env_bool("FIREBASE_AUTH_ENABLED", False)


def auth_cookie_secure() -> bool:
    return _env_bool("AUTH_COOKIE_SECURE", True)


def session_cookie_name() -> str:
    # __Host- 접두사는 Secure, Path=/, Domain 미지정을 브라우저가 강제한다.
    return "__Host-ai_session" if auth_cookie_secure() else "ai_session"


def csrf_cookie_name() -> str:
    return "ai_csrf"


def session_ttl_seconds() -> int:
    try:
        hours = int(os.getenv("AUTH_SESSION_HOURS", "8"))
    except ValueError:
        hours = 8
    return max(1, min(hours, 24 * 14)) * 60 * 60


def firebase_web_api_key() -> str:
    return os.getenv("FIREBASE_WEB_API_KEY", "").strip()


def bootstrap_admin_emails() -> set[str]:
    return {
        item.strip().casefold()
        for item in os.getenv("FIREBASE_BOOTSTRAP_ADMIN_EMAILS", "").split(",")
        if item.strip()
    }


def users_db_path() -> Path:
    configured = os.getenv("AUTH_USERS_DB_PATH", "").strip()
    return Path(configured).expanduser() if configured else RUNTIME_DIR / "users.sqlite3"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _normalize_email(value: str) -> str:
    return str(value or "").strip().casefold()


def _create_users_schema(connection: sqlite3.Connection) -> None:
    connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                firebase_uid TEXT PRIMARY KEY,
                email TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT 'member',
                mantis_username TEXT NOT NULL DEFAULT '',
                team TEXT NOT NULL DEFAULT '',
                approved INTEGER NOT NULL DEFAULT 1,
                invited_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_login_at TEXT
            )
            """
        )
    connection.execute("CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)")


@lru_cache(maxsize=8)
def _init_users_db_at(database_path: str) -> None:
    run_sqlite_migrations(
        Path(database_path),
        [_create_users_schema],
        database_name="users",
    )


def init_users_db() -> None:
    _init_users_db_at(str(users_db_path().resolve()))


def _row_to_user(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "uid": row["firebase_uid"],
        "email": row["email"],
        "displayName": row["display_name"],
        "role": row["role"],
        "mantisUsername": row["mantis_username"],
        "team": row["team"],
        "approved": bool(row["approved"]),
        "invitedBy": row["invited_by"],
        "createdAt": row["created_at"],
        "updatedAt": row["updated_at"],
        "lastLoginAt": row["last_login_at"],
    }


def get_user_by_uid(uid: str) -> Optional[Dict[str, Any]]:
    init_users_db()
    with closing(sqlite3.connect(users_db_path())) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM users WHERE firebase_uid = ?",
            (str(uid or "").strip(),),
        ).fetchone()
    return _row_to_user(row) if row else None


def get_user_by_email(email: str) -> Optional[Dict[str, Any]]:
    init_users_db()
    with closing(sqlite3.connect(users_db_path())) as connection, connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT * FROM users WHERE email = ?",
            (_normalize_email(email),),
        ).fetchone()
    return _row_to_user(row) if row else None


def list_users() -> List[Dict[str, Any]]:
    init_users_db()
    with closing(sqlite3.connect(users_db_path())) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT * FROM users ORDER BY approved DESC, display_name, email"
        ).fetchall()
    return [_row_to_user(row) for row in rows]


def upsert_invited_user(
    uid: str,
    email: str,
    display_name: str,
    role: str,
    mantis_username: str,
    team: str,
    invited_by: str,
) -> Dict[str, Any]:
    normalized_email = _normalize_email(email)
    normalized_role = role if role in VALID_ROLES else "member"
    now = _now()
    init_users_db()
    with closing(sqlite3.connect(users_db_path())) as connection, connection:
        # 같은 이메일의 Firebase UID가 바뀐 경우에도 관리자의 새 초대로 명시적으로
        # 연결을 갱신한다. 이메일 UNIQUE 충돌을 피하기 위해 이전 행을 먼저 제거한다.
        connection.execute(
            "DELETE FROM users WHERE email = ? AND firebase_uid <> ?",
            (normalized_email, uid),
        )
        connection.execute(
            """
            INSERT INTO users (
                firebase_uid, email, display_name, role, mantis_username, team,
                approved, invited_by, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(firebase_uid) DO UPDATE SET
                email = excluded.email,
                display_name = excluded.display_name,
                role = excluded.role,
                mantis_username = excluded.mantis_username,
                team = excluded.team,
                approved = 1,
                invited_by = excluded.invited_by,
                updated_at = excluded.updated_at
            """,
            (
                uid,
                normalized_email,
                display_name.strip(),
                normalized_role,
                mantis_username.strip(),
                team.strip(),
                invited_by,
                now,
                now,
            ),
        )
    user = get_user_by_uid(uid)
    assert user is not None
    return user


def update_user_access(
    uid: str,
    *,
    role: Optional[str] = None,
    approved: Optional[bool] = None,
    display_name: Optional[str] = None,
    mantis_username: Optional[str] = None,
    team: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    current = get_user_by_uid(uid)
    if not current:
        return None
    next_role = role if role in VALID_ROLES else current["role"]
    next_approved = current["approved"] if approved is None else bool(approved)
    with closing(sqlite3.connect(users_db_path())) as connection, connection:
        connection.execute(
            """
            UPDATE users SET role = ?, approved = ?, display_name = ?,
                mantis_username = ?, team = ?, updated_at = ?
            WHERE firebase_uid = ?
            """,
            (
                next_role,
                int(next_approved),
                current["displayName"] if display_name is None else display_name.strip(),
                current["mantisUsername"] if mantis_username is None else mantis_username.strip(),
                current["team"] if team is None else team.strip(),
                _now(),
                uid,
            ),
        )
    return get_user_by_uid(uid)


def approve_login_claims(claims: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    uid = str(claims.get("uid") or claims.get("sub") or "").strip()
    email = _normalize_email(claims.get("email", ""))
    if not uid or not email:
        return None

    user = get_user_by_uid(uid)
    if not user and email in bootstrap_admin_emails():
        user = upsert_invited_user(
            uid,
            email,
            str(claims.get("name") or email.split("@", 1)[0]),
            "admin",
            "",
            "",
            "bootstrap",
        )
    if not user or not user["approved"] or user["email"] != email:
        return None

    with closing(sqlite3.connect(users_db_path())) as connection, connection:
        connection.execute(
            "UPDATE users SET last_login_at = ?, updated_at = ? WHERE firebase_uid = ?",
            (_now(), _now(), uid),
        )
    return get_user_by_uid(uid)


def _firebase_auth_module():
    try:
        import firebase_admin
        from firebase_admin import auth as firebase_auth
        from firebase_admin import credentials
    except ImportError as exc:
        raise AuthConfigurationError(
            "firebase-admin 패키지가 없습니다. requirements.txt를 다시 설치해 주세요."
        ) from exc

    try:
        firebase_admin.get_app()
    except ValueError:
        credential_path = os.getenv("FIREBASE_SERVICE_ACCOUNT_PATH", "").strip()
        credential_json = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON", "").strip()
        project_id = os.getenv("FIREBASE_PROJECT_ID", "").strip()
        options = {"projectId": project_id} if project_id else None
        if credential_json:
            try:
                credential = credentials.Certificate(json.loads(credential_json))
            except (ValueError, TypeError) as exc:
                raise AuthConfigurationError("FIREBASE_SERVICE_ACCOUNT_JSON 형식이 잘못되었습니다.") from exc
            firebase_admin.initialize_app(credential, options=options)
        elif credential_path:
            firebase_admin.initialize_app(credentials.Certificate(credential_path), options=options)
        else:
            # EC2 IAM/Google ADC를 사용하는 환경을 위해 기본 자격 증명 경로도 지원한다.
            try:
                firebase_admin.initialize_app(options=options)
            except Exception as exc:
                raise AuthConfigurationError(
                    "Firebase 서비스 계정 경로 또는 JSON을 설정해 주세요."
                ) from exc
    return firebase_auth


def create_firebase_session(id_token: str) -> Tuple[str, Dict[str, Any]]:
    firebase_auth = _firebase_auth_module()
    claims = firebase_auth.verify_id_token(id_token, check_revoked=True)
    auth_time = int(claims.get("auth_time") or 0)
    now = int(datetime.now(timezone.utc).timestamp())
    if not auth_time or now - auth_time > 5 * 60:
        raise PermissionError("다시 로그인해 주세요.")
    cookie = firebase_auth.create_session_cookie(
        id_token,
        expires_in=timedelta(seconds=session_ttl_seconds()),
    )
    return cookie, claims


def verify_firebase_session(session_cookie: str) -> Dict[str, Any]:
    return _firebase_auth_module().verify_session_cookie(session_cookie, check_revoked=True)


def firebase_invite_user(email: str, display_name: str) -> Tuple[str, str, bool]:
    firebase_auth = _firebase_auth_module()
    normalized_email = _normalize_email(email)
    created = False
    try:
        firebase_user = firebase_auth.get_user_by_email(normalized_email)
        if getattr(firebase_user, "disabled", False):
            firebase_user = firebase_auth.update_user(firebase_user.uid, disabled=False)
    except firebase_auth.UserNotFoundError:
        firebase_user = firebase_auth.create_user(
            email=normalized_email,
            display_name=display_name.strip() or None,
            password=secrets.token_urlsafe(32),
            disabled=False,
        )
        created = True
    reset_link = firebase_auth.generate_password_reset_link(normalized_email)
    return str(firebase_user.uid), reset_link, created


def revoke_firebase_sessions(uid: str) -> None:
    _firebase_auth_module().revoke_refresh_tokens(uid)


def _requires_admin(path: str) -> bool:
    return path in ADMIN_API_EXACT_PATHS or any(path.startswith(prefix) for prefix in ADMIN_API_PREFIXES)


def _csrf_valid(request: Request) -> bool:
    cookie_token = request.cookies.get(csrf_cookie_name(), "")
    header_token = request.headers.get("x-csrf-token", "")
    return bool(cookie_token and header_token and secrets.compare_digest(cookie_token, header_token))


async def authentication_middleware(request: Request, call_next):
    """인증 활성화 시 API를 세션·승인 사용자·CSRF 기준으로 보호한다."""
    if not firebase_auth_enabled():
        return await call_next(request)

    path = request.url.path
    if not path.startswith("/api/") or path in PUBLIC_API_PATHS:
        return await call_next(request)

    session_cookie = request.cookies.get(session_cookie_name(), "")
    if not session_cookie:
        return JSONResponse({"detail": "로그인이 필요합니다."}, status_code=401)
    try:
        claims = await run_in_threadpool(verify_firebase_session, session_cookie)
    except Exception:
        response = JSONResponse({"detail": "로그인 세션이 만료되었습니다."}, status_code=401)
        response.delete_cookie(session_cookie_name(), path="/", secure=auth_cookie_secure())
        return response

    user = get_user_by_uid(str(claims.get("uid") or claims.get("sub") or ""))
    claims_email = _normalize_email(claims.get("email", ""))
    if not user or not user["approved"] or (claims_email and claims_email != user["email"]):
        return JSONResponse({"detail": "승인되지 않은 사용자입니다."}, status_code=403)
    request.state.user = user

    if _requires_admin(path) and user["role"] != "admin":
        return JSONResponse({"detail": "관리자 권한이 필요합니다."}, status_code=403)

    if request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"} and not _csrf_valid(request):
        return JSONResponse({"detail": "요청 보안 토큰이 올바르지 않습니다."}, status_code=403)
    return await call_next(request)


def current_request_user(request: Request) -> Optional[Dict[str, Any]]:
    return getattr(request.state, "user", None)
