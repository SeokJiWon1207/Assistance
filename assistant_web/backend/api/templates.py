"""팀 공용 질문 템플릿 API."""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from backend.team_templates import TeamTemplateConfigError, load_team_templates


router = APIRouter()


@router.get("/api/templates")
def list_team_templates() -> Dict[str, Any]:
    try:
        return load_team_templates()
    except TeamTemplateConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
