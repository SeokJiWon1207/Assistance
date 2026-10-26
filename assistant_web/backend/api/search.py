"""모델 목록과 Chroma 검색 API."""

from datetime import datetime, timezone
from typing import Annotated, Any, Dict

import requests
from fastapi import APIRouter, HTTPException, Path

from backend.llm import get_model_registry, public_model
from backend.schemas import ChromaSearchRequest
from backend.sources.chroma import execute_chroma_search, get_chroma_document
from backend.sources import mantis
from backend.sources.mantis_store import render_document
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
def read_chroma_document(issue_id: Annotated[int, Path(gt=0)]) -> Dict[str, Any]:
    document = get_chroma_document(issue_id)
    if document:
        return {**document, "source": "chroma"}
    # 과거 답변의 저장 원문은 보존하고, 저장본에 없는 최신 이슈만 원천에서 조회한다.
    try:
        issue = mantis.fetch_mantis_issue_by_id(issue_id)
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail="Mantis 원문에 연결하지 못했습니다. 잠시 후 다시 조회해 주세요.") from exc
    except HTTPException as exc:
        if exc.status_code >= 500:
            raise HTTPException(status_code=502, detail="Mantis 원문을 조회하지 못했습니다. 잠시 후 다시 조회해 주세요.") from exc
        raise
    if not issue:
        raise HTTPException(status_code=404, detail="저장본과 Mantis에서 이슈 원문을 찾지 못했습니다.")
    return {"issue_id": issue_id, "content": render_document(issue), "source": "mantis",
            "fetchedAt": datetime.now(timezone.utc).isoformat()}
