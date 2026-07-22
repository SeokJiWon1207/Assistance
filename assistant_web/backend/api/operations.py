"""헬스체크와 정적 앱 진입점."""

from typing import Dict

from fastapi import APIRouter
from fastapi.responses import FileResponse

from backend.config import STATIC_DIR


router = APIRouter()


@router.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@router.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
