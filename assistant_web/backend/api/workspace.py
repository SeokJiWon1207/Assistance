"""개인 업무 홈과 로그인 사용자별 워크스페이스 API."""

from __future__ import annotations

import re
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from backend.auth import current_request_user, list_users
from backend.schemas import ConversationMessage
from backend.workspace import (
    ConversationQuotaError,
    ConversationConflictError,
    build_my_workspace,
    build_team_dashboard,
    delete_conversation,
    list_conversations,
    mark_notification_read,
    save_conversation,
)
from backend.reports.team_brief import get_weekly_team_brief, mark_weekly_team_brief_read
from backend.reports.dashboard import DASHBOARD_PERIOD_OPTIONS, build_operations_dashboard


router = APIRouter(prefix="/api/workspace", tags=["workspace"])
ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


class ConversationWriteRequest(BaseModel):
    expectedUpdatedAt: str = Field(default="", max_length=64)
    title: str = Field(default="새 대화", max_length=120)
    messages: List[ConversationMessage] = Field(default_factory=list, max_length=80)
    favorite: bool = False
    createdAt: str = Field(default="", max_length=40)

    @model_validator(mode="after")
    def cap_serialized_size(self):
        if len(self.model_dump_json().encode("utf-8")) > 1_000_000:
            raise ValueError("대화 저장 요청은 1MB를 넘을 수 없습니다.")
        return self


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
def my_workspace(request: Request, refresh: bool = False) -> Dict[str, Any]:
    return build_my_workspace(_user(request), refresh=refresh)


@router.get("/dashboard")
def operations_dashboard(request: Request, days: int = 30, refresh: bool = False) -> Dict[str, Any]:
    user = _user(request)
    if days not in DASHBOARD_PERIOD_OPTIONS:
        raise HTTPException(
            status_code=422,
            detail="조회 기간은 7일, 14일, 30일, 90일 중 하나여야 합니다.",
        )
    users = list_users()
    return build_operations_dashboard(user, users=users, days=days, refresh=refresh)


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
    try:
        conversation = save_conversation(
            user["uid"],
            _valid_id(conversation_id, "대화 ID"),
            payload.title,
            [message.model_dump(exclude_none=True) for message in payload.messages],
            payload.favorite,
            payload.createdAt,
            payload.expectedUpdatedAt,
        )
    except ConversationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ConversationQuotaError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    return {"conversation": conversation}


@router.delete("/conversations/{conversation_id}")
def remove_conversation(conversation_id: str, request: Request) -> Dict[str, bool]:
    user = _user(request)
    return {"deleted": delete_conversation(user["uid"], _valid_id(conversation_id, "대화 ID"))}


@router.post("/notifications/{notification_id}/read")
def read_notification(notification_id: str, request: Request) -> Dict[str, bool]:
    user = _user(request)
    mark_notification_read(user["uid"], _valid_id(notification_id, "알림 ID"))
    return {"read": True}


@router.get("/team")
def team_dashboard(request: Request) -> Dict[str, Any]:
    user = _user(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="관리자 권한이 필요합니다.")
    return build_team_dashboard(list_users())


@router.get("/team-brief")
def team_brief(request: Request) -> Dict[str, Any]:
    return get_weekly_team_brief(_user(request)["uid"])


@router.post("/team-brief/read")
def read_team_brief(request: Request) -> Dict[str, bool]:
    user = _user(request)
    brief = get_weekly_team_brief(user["uid"])
    mark_weekly_team_brief_read(user["uid"], brief["weekStart"])
    return {"read": True}


@router.post("/team-brief/refresh")
def refresh_team_brief(request: Request) -> Dict[str, Any]:
    user = _user(request)
    if user.get("role") not in {"manager", "admin"}:
        raise HTTPException(status_code=403, detail="매니저 이상만 팀 브리핑을 재생성할 수 있습니다.")
    return get_weekly_team_brief(user["uid"], refresh=True)
