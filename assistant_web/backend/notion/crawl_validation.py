"""수집 페이지와 상위 문서·동기화 원본의 소속을 확인한다."""
from __future__ import annotations
from backend.notion import crawl_transport
from typing import Any
from backend.notion.page_ownership import page_belongs_to_database
from backend.notion.crawl_transport import CrawlBudget as _Budget, NotionReadFailure as _ReadFailure

def _id(value: Any) -> str:
    return str(value or "").replace("-", "").lower()


def _source_signature(raw: dict[str, Any], parents: dict[str, dict[str, Any]]) -> list[Any]:
    return [(key, *(obj.get(field) for field in
             ("id", "object", "parent", "last_edited_time", "archived", "in_trash", "synced_block")))
            for key, obj in sorted({"source": raw, **parents}.items())]


def _synced_source(identifier: str, allowed: set[str], budget: _Budget
                   ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str, str]:
    raw = crawl_transport.request_json(f"blocks/{identifier}", budget)
    if (raw.get("object") != "block" or _id(raw.get("id")) != identifier
            or raw.get("type") != "synced_block" or (raw.get("synced_block") or {}).get("synced_from")
            or not raw.get("last_edited_time") or raw.get("archived") or raw.get("in_trash")):
        raise _ReadFailure("synced_source_invalid")
    parents = _page_ancestors(raw, budget)
    database_id = next((db for db in sorted(allowed) if page_belongs_to_database(raw, db, parents)), "")
    if not database_id:
        raise _ReadFailure("synced_source_outside_scope")
    owner = _source_owner(raw, parents)
    if not owner:
        raise _ReadFailure("synced_source_owner_unknown")
    return raw, parents, owner, database_id


def _source_owner(raw: dict[str, Any], parents: dict[str, dict[str, Any]]) -> str:
    current = raw
    for _ in range(64):
        parent = current.get("parent") or {}
        kind = parent.get("type")
        current = parents.get(_id(parent.get(kind)), {})
        if current.get("object") == "page" and current.get("last_edited_time"):
            return _id(current["id"])
    return ""


def _page_ancestors(raw: dict[str, Any], budget: _Budget,
                    cache: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    parents = cache if cache is not None else {}
    current = raw
    seen = {_id(raw.get("id"))}
    for _ in range(64):
        parent = current.get("parent") or {}
        kind = parent.get("type")
        if kind not in {"page_id", "block_id"}:
            break
        identifier = _id(parent.get(kind))
        if not identifier or identifier in seen:
            break
        seen.add(identifier)
        if identifier not in parents:
            endpoint = "pages" if kind == "page_id" else "blocks"
            parents[identifier] = crawl_transport.request_json(f"{endpoint}/{identifier}", budget)
        current = parents[identifier]
    return parents


def _valid_page(raw: dict[str, Any], page_id: str, database_id: str,
                allowed_database_ids: set[str], parent_pages: dict[str, dict[str, Any]] | None = None) -> bool:
    return bool(
        raw.get("object") == "page" and _id(raw.get("id")) == _id(page_id)
        and not raw.get("archived") and not raw.get("in_trash")
        and page_belongs_to_database(raw, database_id, parent_pages)
        and _id(database_id) in allowed_database_ids
        and raw.get("last_edited_time")
    )
