"""Notion 조회·검색·쓰기 API."""

from typing import Any, Dict

import requests
from fastapi import APIRouter, HTTPException, Request

from backend.auth import current_request_user
from backend.config import NOTION_API_BASE, get_notion_headers, get_notion_settings, notion_is_configured
from backend.schemas import ChromaSearchRequest, NotionPageUpdateRequest, NotionPageWriteRequest
from backend.notion.schema import ensure_readable_notion_database
from backend.sources.notion import (
    build_sources_from_single_notion_page,
    create_notion_page,
    execute_notion_search,
    fetch_notion_database_title,
    normalize_notion_page,
    update_notion_page,
    validate_notion_page_id,
)
from backend.understanding import extract_query_constraints


router = APIRouter()


def _require_notion_writer(request: Request) -> None:
    user = current_request_user(request)
    if user and user.get("role") not in {"manager", "admin"}:
        raise HTTPException(status_code=403, detail="Notion 수정은 매니저 이상만 가능합니다.")


@router.get("/api/notion/status")
def notion_status_endpoint() -> Dict[str, Any]:
    settings = get_notion_settings()
    database_ids = settings.get("database_ids", [])
    return {
        "configured": notion_is_configured(),
        "hasToken": bool(settings["token"]),
        "hasDatabaseId": bool(database_ids),
        "databaseCount": len(database_ids),
        "databaseIds": [database_id[-8:] for database_id in database_ids],
        "databaseSource": settings.get("database_source", "configured"),
        "databaseDiscoveryError": bool(settings.get("database_discovery_error")),
        "version": settings["version"],
    }


@router.post("/api/notion/search")
def search_notion(request: ChromaSearchRequest) -> Dict[str, Any]:
    limit = max(1, min(request.limit, 20))
    return execute_notion_search(request.query, limit, extract_query_constraints(request.query))


@router.get("/api/notion/pages/{page_id}")
def get_notion_page_endpoint(page_id: str) -> Dict[str, Any]:
    page_id = validate_notion_page_id(page_id)
    response = requests.get(
        f"{NOTION_API_BASE}/pages/{page_id}",
        headers=get_notion_headers(),
        timeout=20,
    )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail=f"Notion 항목 조회 실패: {response.status_code} {response.text[:300]}",
        )
    page = response.json()
    database_id = page.get("parent", {}).get("database_id", "")
    ensure_readable_notion_database(database_id)
    return {
        "page": normalize_notion_page(
            page,
            include_blocks=True,
            database_id=database_id,
            database_label=fetch_notion_database_title(database_id) if database_id else "Notion",
        )
    }


@router.post("/api/notion/pages")
def create_notion_page_endpoint(payload: NotionPageWriteRequest, request: Request) -> Dict[str, Any]:
    _require_notion_writer(request)
    page = create_notion_page(payload)
    return {"page": page, "sources": build_sources_from_single_notion_page(page)}


@router.patch("/api/notion/pages/{page_id}")
def update_notion_page_endpoint(
    page_id: str,
    payload: NotionPageUpdateRequest,
    request: Request,
) -> Dict[str, Any]:
    _require_notion_writer(request)
    page = update_notion_page(validate_notion_page_id(page_id), payload)
    return {"page": page, "sources": build_sources_from_single_notion_page(page)}
