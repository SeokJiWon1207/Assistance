"""입력 페이지·블록의 소속 경로를 I/O 없이 판정한다."""
from __future__ import annotations
from typing import Any


def _id(value: Any) -> str:
    return str(value or "").replace("-", "").lower()


def segment_belongs_to_page(segment: dict[str, Any], page_id: str) -> bool:
    """직접 소유 근거 또는 원본 검증을 마친 동기화 복제 근거만 허용한다.

    저장본의 표시는 후보 발견에만 쓰며, 결과에는 매번 API 재검증한 근거를 쓴다.
    """
    target = _id(page_id)
    if _id(segment.get("owner_page_id") or segment.get("page_id")) == target:
        return True
    return bool(target and _id(segment.get("display_page_id")) == target
                and segment.get("owner_page_id") and segment.get("synced_source_id")
                and segment.get("synced_copy_id") and segment.get("source_database_id"))


def _ancestor_page_path(raw: dict[str, Any], database_id: str,
                         parent_pages: dict[str, dict[str, Any]] | None = None) -> list[str] | None:
    parents = parent_pages or {}
    current = raw
    seen: set[str] = set()
    page_ids: list[str] = []
    for _ in range(65):
        identifier = _id(current.get("id"))
        if not identifier or identifier in seen or current.get("archived") or current.get("in_trash"):
            return None
        seen.add(identifier)
        parent = current.get("parent") or {}
        if parent.get("type") == "database_id":
            return page_ids if _id(parent.get("database_id")) == _id(database_id) else None
        kind = parent.get("type")
        if kind not in {"page_id", "block_id"}:
            return None
        ancestor_id = _id(parent.get(kind))
        current = parents.get(ancestor_id, {})
        if _id(current.get("id")) != ancestor_id or current.get("object") != kind.removesuffix("_id"):
            return None
        if kind == "page_id":
            page_ids.append(ancestor_id)
    return None


def page_belongs_to_database(raw: dict[str, Any], database_id: str,
                             parent_pages: dict[str, dict[str, Any]] | None = None) -> bool:
    """페이지·블록의 실제 부모 경로가 지정 DB까지 이어질 때만 인정한다."""
    return _ancestor_page_path(raw, database_id, parent_pages) is not None
