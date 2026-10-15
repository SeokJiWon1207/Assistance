"""임시 비밀번호 발급과 최초 변경. 비밀번호는 앱 DB에 저장하지 않는다."""

from typing import Any
from contextlib import contextmanager
import fcntl
import hashlib
import os

import requests
from fastapi import HTTPException

from backend import auth


def validate_password(password: str) -> None:
    if not 8 <= len(password) <= 128:
        raise HTTPException(status_code=422, detail="비밀번호는 8~128자로 입력해 주세요.")


def create_temporary_password_user(email: str, display_name: str, password: str) -> str:
    validate_password(password)
    firebase = auth._firebase_auth_module()
    if auth.get_user_by_email(email):
        raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다. 기존 계정을 확인해 주세요.")
    try:
        firebase.get_user_by_email(email)
    except firebase.UserNotFoundError:
        pass
    else:
        raise HTTPException(status_code=409, detail="이미 등록된 이메일입니다. 기존 계정을 확인해 주세요.")
    user = firebase.create_user(email=email, display_name=display_name.strip() or None,
                                password=password, disabled=False)
    return str(user.uid)


def _change_password_in_firebase(user: dict[str, Any], current: str, new: str) -> None:
    validate_password(new)
    if not current or len(current) > 128 or current == new:
        raise HTTPException(status_code=422, detail="현재 비밀번호와 다른 새 비밀번호를 입력해 주세요.")
    api_key = auth.firebase_web_api_key()
    if not api_key:
        raise HTTPException(status_code=503, detail="이메일 로그인이 설정되지 않았습니다.")
    try:
        response = requests.post(
            f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}",
            json={"email": user["email"], "password": current, "returnSecureToken": True},
            timeout=12,
        )
        if not response.ok or response.json().get("localId") != user["uid"]:
            raise HTTPException(status_code=400, detail="현재 비밀번호를 확인해 주세요. 잠시 후 다시 시도해 주세요.")
        firebase = auth._firebase_auth_module()
        firebase.update_user(user["uid"], password=new)
        firebase.revoke_refresh_tokens(user["uid"])
    except HTTPException:
        raise
    except Exception as exc:
        # 예외 본문에는 공급자가 보낸 요청 정보가 포함될 수 있어 반환·로그하지 않는다.
        raise HTTPException(status_code=502, detail="비밀번호 변경을 완료하지 못했습니다. 다시 로그인해 시도해 주세요.") from exc


def discard_temporary_password_user(uid: str) -> None:
    """로컬 저장 실패 시 이번 요청에서 새로 만든 계정만 되돌린다."""
    auth._firebase_auth_module().delete_user(uid)
    auth.remove_provisional_user(uid)


@contextmanager
def password_operation(uid: str):
    """같은 사용자의 관리자 초기화와 사용자 변경을 프로세스 간에도 직렬화한다."""
    directory = auth.users_db_path().parent / '.password-locks'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / (hashlib.sha256(uid.encode()).hexdigest() + '.lock')
    # 잠금 파일은 삭제하지 않는다. 삭제 후 재생성 시 서로 다른 inode에 잠길 수 있다.
    with os.fdopen(os.open(path, os.O_CREAT | os.O_RDWR, 0o600), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise HTTPException(status_code=409, detail="비밀번호 변경을 처리 중입니다. 잠시 후 다시 시도해 주세요.") from exc
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def change_temporary_password(user: dict[str, Any], current: str, new: str) -> None:
    with password_operation(user['uid']):
        fresh = auth.get_user_by_uid(user['uid'])
        if (not fresh or not fresh['approved'] or fresh['passwordResetPending']
                or fresh['passwordResetVersion'] != user.get('passwordResetVersion', 0)
                or not fresh['passwordChangeRequired']):
            raise HTTPException(status_code=409, detail="계정 상태가 변경되었습니다. 다시 로그인해 주세요.")
        _change_password_in_firebase(fresh, current, new)
        auth.complete_password_change(user['uid'])


def reset_existing_password(uid: str, password: str) -> dict[str, Any]:
    validate_password(password)
    with password_operation(uid):
        user = auth.get_user_by_uid(uid)
        if not user:
            raise HTTPException(status_code=404, detail="사용자를 찾지 못했습니다.")
        if not user['approved']:
            raise HTTPException(status_code=409, detail="로그인이 허용된 계정만 초기화할 수 있습니다.")
        try:
            auth.begin_password_reset(uid)
            firebase = auth._firebase_auth_module()
            firebase.update_user(uid, password=password)
            firebase.revoke_refresh_tokens(uid)
            auth.finish_password_reset(uid)
        except Exception as exc:
            raise HTTPException(status_code=502, detail="초기화를 완료하지 못했습니다. 계정의 접근 차단 상태를 확인하고 다시 초기화해 주세요.") from exc
        updated = auth.get_user_by_uid(uid)
        assert updated is not None
        return updated
