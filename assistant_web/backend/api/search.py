"""모델 목록과 Chroma 검색 API."""

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

from backend.llm import get_model_registry, public_model
from backend.schemas import ChromaSearchRequest
from backend.sources.chroma import execute_chroma_search, get_chroma_document
from backend.understanding import extract_query_constraints


router = APIRouter()


@router.get("/api/models")
def list_models() -> Dict[str, Any]:
    return {"models": [public_model(model) for model in get_model_registry()]}


@router.post("/api/chroma/search")
def search_chroma(request: ChromaSearchRequest) -> Dict[str, Any]:
    limit = max(1, min(request.limit, 20))
    return execute_chroma_search(request.query, limit, extract_query_constraints(request.query))


@router.get("/api/chroma/documents/{issue_id}")
def read_chroma_document(issue_id: int) -> Dict[str, Any]:
    document = get_chroma_document(issue_id)
    if not document:
        raise HTTPException(status_code=404, detail="ChromaDB 문서를 찾지 못했습니다.")
    return document
