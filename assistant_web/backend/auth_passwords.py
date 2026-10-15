"""임시 비밀번호 발급과 최초 변경. 비밀번호는 앱 DB에 저장하지 않는다."""

from typing import Any

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


def change_temporary_password(user: dict[str, Any], current: str, new: str) -> None:
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
