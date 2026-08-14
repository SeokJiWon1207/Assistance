"""해결 플레이북 저장·검색·큐레이션 API."""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator

from backend.auth import current_request_user
from backend.playbooks import create_playbook, get_playbook, list_playbooks, update_playbook


router = APIRouter(prefix="/api/playbooks", tags=["playbooks"])


class PlaybookWriteRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    symptom: str = Field(min_length=1, max_length=4000)
    cause: str = Field(default="", max_length=4000)
    resolution: str = Field(min_length=1, max_length=8000)
    verification: str = Field(default="", max_length=4000)
    tags: List[str] = Field(default_factory=list, max_length=12)
    sourceIssueIds: List[int] = Field(default_factory=list, max_length=20)
    status: Literal["draft", "published"] = "draft"

    @model_validator(mode="after")
    def strip_required_text(self):
        if not self.title.strip() or not self.symptom.strip() or not self.resolution.strip():
            raise ValueError("제목, 증상, 해결 방법은 비워둘 수 없습니다.")
        return self


class PlaybookUpdateRequest(BaseModel):
    title: Optional[str] = Field(default=None, max_length=200)
    symptom: Optional[str] = Field(default=None, max_length=4000)
    cause: Optional[str] = Field(default=None, max_length=4000)
    resolution: Optional[str] = Field(default=None, max_length=8000)
    verification: Optional[str] = Field(default=None, max_length=4000)
    tags: Optional[List[str]] = Field(default=None, max_length=12)
    sourceIssueIds: Optional[List[int]] = Field(default=None, max_length=20)
    status: Optional[Literal["draft", "published", "archived"]] = None


def _user(request: Request) -> Dict[str, Any]:
    user = current_request_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다.")
    return user


def _curator(request: Request) -> Dict[str, Any]:
    user = _user(request)
    if user.get("role") not in {"manager", "admin"}:
        raise HTTPException(status_code=403, detail="매니저 이상만 플레이북을 편집할 수 있습니다.")
    return user


@router.get("")
def search_playbooks(
    request: Request,
    q: str = "",
    status: str = "",
    includeDrafts: bool = False,
    limit: int = 100,
) -> Dict[str, Any]:
    user = _user(request)
    can_curate = user.get("role") in {"manager", "admin"}
    include_unpublished = bool(includeDrafts and can_curate)
    items = list_playbooks(
        q,
        include_unpublished=include_unpublished,
        status=status if include_unpublished else "published",
        limit=limit,
    )
    return {"playbooks": items, "canCurate": can_curate}


@router.post("")
def add_playbook(payload: PlaybookWriteRequest, request: Request) -> Dict[str, Any]:
    user = _curator(request)
    return {"playbook": create_playbook(payload.model_dump(), user)}


@router.patch("/{playbook_id}")
def curate_playbook(
    playbook_id: str,
    payload: PlaybookUpdateRequest,
    request: Request,
) -> Dict[str, Any]:
    _curator(request)
    changes = payload.model_dump(exclude_unset=True)
    current = get_playbook(playbook_id)
    if not current:
        raise HTTPException(status_code=404, detail="플레이북을 찾지 못했습니다.")
    next_title = str(changes.get("title", current["title"])).strip()
    next_symptom = str(changes.get("symptom", current["symptom"])).strip()
    next_resolution = str(changes.get("resolution", current["resolution"])).strip()
    if not next_title or not next_symptom or not next_resolution:
        raise HTTPException(status_code=422, detail="제목, 증상, 해결 방법은 비워둘 수 없습니다.")
    updated = update_playbook(playbook_id, changes)
    return {"playbook": updated}

