"""개인 업무 홈과 로그인 사용자별 워크스페이스 API."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.auth import current_request_user, list_users
from backend.workspace import (
    build_my_workspace,
    build_team_dashboard,
    delete_conversation,
    list_conversations,
    mark_notification_read,
    save_conversation,
)


router = APIRouter(prefix="/api/workspace", tags=["workspace"])
ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ConversationWriteRequest(BaseModel):
    title: str = Field(default="새 대화", max_length=120)
    messages: List[Dict[str, Any]] = Field(default_factory=list, max_length=80)
    favorite: bool = False
    createdAt: str = Field(default="", max_length=40)


def _user(request: Request) -> Dict[str, Any]:
    user = current_request_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


def _valid_id(value: str, label: str) -> str:
    if not ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=422, detail=f"올바르지 않은 {label}입니다.")
    return value


@router.get("/me")
def my_workspace(request: Request) -> Dict[str, Any]:
    return build_my_workspace(_user(request))


@router.get("/conversations")
def my_conversations(request: Request, limit: int = 40) -> Dict[str, Any]:
    return {"conversations": list_conversations(_user(request)["uid"], limit)}


@router.put("/conversations/{conversation_id}")
def put_conversation(
    conversation_id: str,
    payload: ConversationWriteRequest,
    request: Request,
) -> Dict[str, Any]:
    user = _user(request)
    return {
        "conversation": save_conversation(
            user["uid"],
            _valid_id(conversation_id, "대화 ID"),
            payload.title,
            payload.messages,
            payload.favorite,
            payload.createdAt,
        )
    }


@router.delete("/conversations/{conversation_id}")
def remove_conversation(conversation_id: str, request: Request) -> Dict[str, bool]:
    user = _user(request)
    return {
        "deleted": delete_conversation(
            user["uid"], _valid_id(conversation_id, "대화 ID")
        )
    }


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: str, request: Request) -> Dict[str, bool]:
    user = _user(request)
    mark_notification_read(
        user["uid"], _valid_id(notification_id, "알림 ID")
    )
    return {"read": True}


@router.get("/team")
def team_dashboard(request: Request) -> Dict[str, Any]:
    user = _user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return build_team_dashboard(list_users())
