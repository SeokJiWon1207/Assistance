"""허용한 DB의 본문 색인을 재개 가능한 단위로 채우고 원본을 재검증한다."""

from __future__ import annotations

import os
import math
import time
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from backend.config import (
    NOTION_API_BASE, get_notion_headers, get_notion_settings, notion_request_slot, parse_notion_database_ids,
)
from backend.execution import bounded_timeout, interruptible_sleep
from backend.notion.body_index import BodyIndex, open_body_index, page_belongs_to_database
from backend.notion.mapping import notion_block_text
from backend.notion.read_scope import resolve_notion_read_scope


def _id(value: Any) -> str:
    return str(value or "").replace("-", "").lower()


def _configured_int(name: str, default: int, ceiling: int) -> int:
    try:
        return max(1, min(int(os.getenv(name, str(default))), ceiling))
    except (TypeError, ValueError):
        return default


def notion_body_index_status(database_ids: list[str] | None = None, *,
                            index: BodyIndex | None = None) -> dict[str, Any]:
    """네트워크나 파일 생성 없이 명시적으로 설정한 읽기 범위의 저장 상태를 읽는다."""
    unavailable = {"available": False, "page_count": 0, "indexed_page_count": 0,
                   "pending_page_count": 0, "failed_page_count": 0, "catalog_complete": False,
                   "content_complete": False, "complete": False, "database_count": 0,
                   "completed_database_count": 0}
    # 일반 설정 함수는 루트가 없으면 원격 자동 발견을 하므로 그 전에 종료한다.
    roots = parse_notion_database_ids()
    if not roots:
        return {**unavailable, "scope_complete": False,
                "incomplete_reasons": ["configured_scope_unavailable"]}
    settings = get_notion_settings()
    scope, scope_complete = resolve_notion_read_scope(roots, settings["token"])
    allowed = {_id(identifier) for identifier in scope}
    selected = list(dict.fromkeys(_id(identifier) for identifier in (database_ids if database_ids is not None else scope)))
    if not selected or any(identifier not in allowed for identifier in selected):
        raise ValueError("Requested database is outside the configured Notion read scope")
    index = index if index is not None else open_body_index()
    reasons = [] if scope_complete else ["child_database_discovery"]
    if index is None:
        return {**unavailable, "database_count": len(selected), "scope_complete": scope_complete,
                "incomplete_reasons": [*reasons, "body_index_unavailable"]}
    status = index.status(selected)
    return {**status, "complete": bool(status.get("complete") and scope_complete),
            "scope_complete": scope_complete, "incomplete_reasons": reasons}


@dataclass(frozen=True)
class CrawlLimits:
    max_blocks: int = 10000
    max_depth: int = 64
    max_requests: int = 1000
    max_seconds: float = 180

    @classmethod
    def configured(cls) -> CrawlLimits:
        return cls(
            max_blocks=_configured_int("NOTION_BODY_INDEX_MAX_BLOCKS", 10000, 100000),
            max_depth=_configured_int("NOTION_BODY_INDEX_MAX_DEPTH", 64, 128),
            max_requests=_configured_int("NOTION_BODY_INDEX_MAX_REQUESTS", 1000, 10000),
            max_seconds=_configured_int("NOTION_BODY_INDEX_PAGE_SECONDS", 180, 3600),
        )


class _ReadFailure(Exception):
    pass


@dataclass
class _Budget:
    deadline: float
    requests_left: int


_retry_after_until = 0.0
_retry_after_lock = threading.Lock()


def _wait_for_retry_after(budget: _Budget) -> None:
    # 페이지 예산이 끝나도 같은 프로세스의 다음 요청은 서버 대기를 지킨다.
    while True:
        with _retry_after_lock:
            until = _retry_after_until
        delay = until - time.monotonic()
        if delay <= 0:
            return
        if until >= budget.deadline:
            raise _ReadFailure("http_429")
        interruptible_sleep(delay)


def _request(path: str, budget: _Budget, *, payload: dict[str, Any] | None = None,
             post: bool = False) -> dict[str, Any]:
    """모든 시도는 공유 요청 간격과 현재 실행의 남은 예산을 지킨다."""
    global _retry_after_until
    for attempt in range(3):
        remaining = budget.deadline - time.monotonic()
        if remaining <= 0:
            raise _ReadFailure("time_limit")
        if budget.requests_left <= 0:
            raise _ReadFailure("request_limit")
        _wait_for_retry_after(budget)
        budget.requests_left -= 1
        notion_request_slot()
        remaining = budget.deadline - time.monotonic()
        if remaining <= 0:
            raise _ReadFailure("time_limit")
        try:
            kwargs: dict[str, Any] = {"headers": get_notion_headers(), "timeout": bounded_timeout(min(15, remaining))}
            response = (
                requests.post(f"{NOTION_API_BASE}/{path}", json=payload or {}, **kwargs)
                if post else requests.get(f"{NOTION_API_BASE}/{path}", params=payload or {}, **kwargs)
            )
            status = response.status_code
            if status == 200:
                data = response.json()
                if not isinstance(data, dict):
                    raise _ReadFailure("invalid_response")
                return data
            if status != 429 and status < 500:
                raise _ReadFailure(f"http_{status}")
            try:
                retry_after = float(response.headers.get("Retry-After", "0"))
            except (AttributeError, TypeError, ValueError):
                retry_after = 0
            if not math.isfinite(retry_after):
                retry_after = 0
            if status == 429:
                with _retry_after_lock:
                    _retry_after_until = max(
                        _retry_after_until, time.monotonic() + max(retry_after, 0.5 * 2**attempt))
            if attempt == 2:
                raise _ReadFailure(f"http_{status}")
        except requests.RequestException as exc:
            if attempt == 2:
                raise _ReadFailure("request_failed") from exc
            retry_after = 0
        except (TypeError, ValueError) as exc:
            raise _ReadFailure("invalid_response") from exc
        # 서버가 지정한 대기 시간을 줄이면 같은 제한을 연속 소진한다.
        if not math.isfinite(retry_after):
            retry_after = 0
        delay = max(retry_after, 0.5 * 2**attempt)
        if time.monotonic() + delay >= budget.deadline:
            raise _ReadFailure(f"http_{status}" if retry_after > 0 else "time_limit")
        interruptible_sleep(delay)
    raise _ReadFailure("request_failed")


def crawl_notion_page_content(page_id: str, *, limits: CrawlLimits | None = None,
                             deadline: float | None = None,
                             allowed_database_ids: set[str] | None = None) -> dict[str, Any]:
    """페이지가 직접 소유한 모든 일반·표·코드 블록을 페이지네이션한다.

    명시된 허용 DB 안의 검증된 동기화 원본만 출처와 함께 읽는다.
    연결된 DB, 관계형 속성 및 범위 밖 원본은 따라가지 않는다. 독립 하위 페이지가 있으면 전체 색인 범위의 한계도 남긴다.
    """
    limits = limits or CrawlLimits.configured()
    end = time.monotonic() + max(0, limits.max_seconds)
    budget = _Budget(min(end, deadline) if deadline is not None else end, max(0, limits.max_requests))
    segments: list[dict[str, str]] = []
    reasons: set[str] = set()
    unsupported_children: set[str] = set()
    child_databases: set[str] = set()
    seen_containers: set[str] = set()
    seen_blocks: set[str] = set()
    owned_synced_originals: set[str] = set()
    synced_references: set[str] = set()
    block_count = 0
    unsupported_elements: list[dict[str, str]] = []
    reference_copies: dict[str, dict[str, Any]] = {}
    walk_failures: set[str] = set()
    source_exclusions: set[str] = set()
    verified_synced_references: list[dict[str, str]] = []

    def walk(identifier: str, depth: int, projection: dict[str, str] | None = None) -> None:
        nonlocal block_count
        if depth > limits.max_depth:
            reasons.add("depth_limit")
            return
        normalized = _id(identifier)
        if normalized in seen_containers:
            reasons.add("block_cycle")
            return
        seen_containers.add(normalized)
        cursor = ""
        cursors: set[str] = set()
        while True:
            if block_count >= limits.max_blocks:
                reasons.add("block_limit")
                return
            payload: dict[str, Any] = {"page_size": min(100, limits.max_blocks - block_count)}
            if cursor:
                payload["start_cursor"] = cursor
            try:
                data = _request(f"blocks/{identifier}/children", budget, payload=payload)
            except _ReadFailure as exc:
                reasons.add(str(exc))
                walk_failures.add(str(exc))
                return
            rows = data.get("results")
            if not isinstance(rows, list):
                reasons.add("invalid_response")
                return
            for block in rows:
                if block_count >= limits.max_blocks:
                    reasons.add("block_limit")
                    return
                if not isinstance(block, dict) or not block.get("id"):
                    reasons.add("invalid_block")
                    continue
                block_count += 1
                block_id = str(block["id"])
                if _id(block_id) in seen_blocks:
                    reasons.add("duplicate_block")
                    continue
                seen_blocks.add(_id(block_id))
                if block.get("archived") or block.get("in_trash"):
                    continue
                kind = str(block.get("type") or "")
                # API가 원문을 노출하지 않은 블록은 빈 본문이라는 증거가 아니다.
                if kind == "unsupported":
                    reasons.add("unsupported_block")
                    if block.get("has_children"):
                        reasons.add("unsupported_child_content")
                    unsupported_elements.append({"block_id": block_id,
                        "block_type": str((block.get("unsupported") or {}).get("block_type") or "unknown")})
                    # API의 leaf alias는 연결 대상의 parent를 반환할 수 있다.
                    # 원문/자식을 읽지 않는 미지원 참조를 지원 본문 실패로
                    # 중복 집계하지 않는다. 알 수 없는 유형은 아래 검증을 유지한다.
                    if ((block.get("unsupported") or {}).get("block_type") == "alias"
                            and block.get("has_children") is False):
                        continue
                # 반환된 parent 정보가 있으면 현재 조회 컨테이너와 일치해야 한다.
                parent = block.get("parent") or {}
                parent_kind = parent.get("type")
                if (projection and not parent_kind) or (parent_kind and (parent_kind not in {"page_id", "block_id"}
                                    or _id(parent.get(parent_kind)) != normalized)):
                    reasons.add("block_parent_mismatch")
                    continue
                if kind == "unsupported":
                    continue
                if kind == "child_page":
                    if projection is None:
                        unsupported_children.add(block_id)
                    reasons.add("unsupported_synced_child_pages" if projection else "unsupported_child_pages")
                    continue
                if kind == "child_database":
                    child_databases.add(block_id)
                    continue
                if kind == "synced_block" and (block.get(kind) or {}).get("synced_from"):
                    reference = (block.get(kind) or {})["synced_from"]
                    if projection:
                        # 중첩된 외부 원본은 별도 검증 없이 확장하지 않는다.
                        reasons.add("nested_synced_reference")
                    else:
                        source_id = _id(reference.get("block_id"))
                        synced_references.add(source_id)
                        reference_copies.setdefault(source_id, block)
                    continue
                if kind == "synced_block":
                    owned_synced_originals.add(_id(block_id))
                text = notion_block_text(block)
                if text:
                    segments.append({"block_id": block_id, "type": kind, "text": text,
                                     "page_id": page_id, "owner_page_id": page_id, **(projection or {})})
                if block.get("has_children"):
                    walk(block_id, depth + 1, projection)
            if not data.get("has_more"):
                return
            next_cursor = str(data.get("next_cursor") or "")
            if not next_cursor or next_cursor in cursors:
                reasons.add("invalid_cursor")
                return
            cursors.add(next_cursor)
            cursor = next_cursor

    walk(page_id, 0)
    own_failures = reasons - {"unsupported_block"}
    # 같은 페이지의 검증된 소유 트리에서 원본을 읽었다면 복제 위치를 다시
    # 따라갈 필요가 없다. 원본 순서와 무관하며, 원본 순회의 실패는 그대로 남는다.
    for source_id in sorted(synced_references - owned_synced_originals):
        copy = reference_copies[source_id]
        if not allowed_database_ids:
            reasons.add("foreign_synced_block")
            continue
        segment_start = len(segments)
        unsupported_start = len(unsupported_elements)
        try:
            if (copy.get("parent") or {}).get("type") not in {"page_id", "block_id"}:
                raise _ReadFailure("synced_copy_parent_unknown")
            source, parents, owner, database_id = _synced_source(source_id, allowed_database_ids, budget)
            projection = {"owner_page_id": owner, "display_page_id": page_id,
                          "synced_source_id": source_id, "synced_copy_id": str(copy["id"]),
                          "source_database_id": database_id}
            walk(source_id, 1, projection)
            current, current_parents, _, _ = _synced_source(source_id, allowed_database_ids, budget)
            if _source_signature(source, parents) != _source_signature(current, current_parents):
                raise _ReadFailure("synced_source_changed")
            # 페이지 수정 시각만으로는 복제 블록의 참조 교체를 판별할 수 없다.
            current_copy = _request(f"blocks/{copy['id']}", budget)
            if (current_copy.get("object") != "block" or _id(current_copy.get("id")) != _id(copy["id"])
                    or current_copy.get("archived") or current_copy.get("in_trash")
                    or current_copy.get("type") != "synced_block"
                    or current_copy.get("parent") != copy.get("parent")
                    or current_copy.get("synced_block") != copy.get("synced_block")):
                raise _ReadFailure("synced_copy_changed")
            copy_parents = _page_ancestors(current_copy, budget)
            if (_source_owner(current_copy, copy_parents) != _id(page_id)
                    or not any(page_belongs_to_database(current_copy, db, copy_parents)
                               for db in allowed_database_ids)):
                raise _ReadFailure("synced_copy_moved")
            verified_synced_references.append({"source_id": source_id, "database_id": database_id})
        except _ReadFailure as exc:
            del segments[segment_start:]
            del unsupported_elements[unsupported_start:]
            reasons.update({"foreign_synced_block", str(exc)})
            if str(exc) in {"http_403", "http_404", "synced_source_outside_scope"}:
                source_exclusions.update({"foreign_synced_block", str(exc)})
    return {"text": "\n".join(segment["text"] for segment in segments), "segments": segments,
            "complete": not reasons, "incomplete_reasons": sorted(reasons),
            "supported_content_complete": not (reasons - {"unsupported_block"}),
            "unsupported_elements": unsupported_elements,
            "synced_reference_ids": sorted(synced_references - owned_synced_originals),
            "verified_synced_references": verified_synced_references,
            "unsupported_child_page_ids": sorted(unsupported_children),
            "accessible_incomplete_reasons": sorted(
                (reasons - source_exclusions - {"unsupported_block"}) | own_failures | walk_failures),
            "child_database_ids": sorted(child_databases), "block_count": block_count}


def _source_signature(raw: dict[str, Any], parents: dict[str, dict[str, Any]]) -> list[Any]:
    return [(key, *(obj.get(field) for field in
             ("id", "object", "parent", "last_edited_time", "archived", "in_trash", "synced_block")))
            for key, obj in sorted({"source": raw, **parents}.items())]


def _synced_source(identifier: str, allowed: set[str], budget: _Budget
                   ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], str, str]:
    raw = _request(f"blocks/{identifier}", budget)
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
            parents[identifier] = _request(f"{endpoint}/{identifier}", budget)
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


def validate_index_candidate(record: dict[str, Any], allowed_database_ids: list[str], *,
                             index: BodyIndex | None = None, deadline: float | None = None,
                             limits: CrawlLimits | None = None,
                             catalog_page: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """접근·소속·수정본을 확인한 원문만 검색 결과로 반환한다.

    접근 거부, 삭제, 다른 DB로의 이동은 None이다. 수정 시각이 같아도
    같은 분 안의 변경을 놓치지 않도록 현재 본문을 다시 읽는다.
    """
    allowed = {_id(value) for value in allowed_database_ids}
    page_id = str(record.get("page_id") or "")
    database_id = str(record.get("database_id") or "")
    if not page_id or _id(database_id) not in allowed:
        return None
    limits = limits or CrawlLimits(max_requests=100, max_seconds=25)
    end = min(deadline, time.monotonic() + limits.max_seconds) if deadline is not None else time.monotonic() + limits.max_seconds
    budget = _Budget(end, max(6, limits.max_requests))
    try:
        before = None
        if catalog_page and (catalog_page.get("parent") or {}).get("type") in {"database_id", "data_source_id"}:
            try:
                edited = datetime.fromisoformat(str(catalog_page.get("last_edited_time", "")).replace("Z", "+00:00"))
                old_enough = (datetime.now(timezone.utc) - edited).total_seconds() > 60
            except (ValueError, TypeError):
                old_enough = False
            if old_enough and _valid_page(catalog_page, page_id, database_id, allowed):
                before = catalog_page
        if before is None:
            before = _request(f"pages/{page_id}", budget)
        before_parents = _page_ancestors(before, budget)
    except _ReadFailure as exc:
        if index is not None:
            index.record_failure(page_id, str(exc))
        return None
    if not _valid_page(before, page_id, database_id, allowed, before_parents):
        if index is not None:
            index.record_failure(page_id, "page_unavailable_or_moved")
        return None
    revision = str(before["last_edited_time"])
    from backend.notion.property_snapshot import read_full_properties
    try:
        expanded = read_full_properties(before, budget)
    except _ReadFailure as exc:
        if index is not None:
            index.record_failure(page_id, str(exc))
        return None
    content = crawl_notion_page_content(page_id, limits=limits, deadline=end, allowed_database_ids=allowed)
    reasons = set(content["incomplete_reasons"])
    try:
        after = _request(f"pages/{page_id}", budget)
        # 부모 이동은 자식의 수정 시각을 바꾸지 않을 수 있어 수집 뒤 다시 읽는다.
        after_parents = _page_ancestors(after, budget)
    except _ReadFailure as exc:
        if index is not None:
            index.record_failure(page_id, str(exc))
        return None
    if not _valid_page(after, page_id, database_id, allowed, after_parents):
        if index is not None:
            index.record_failure(page_id, "page_unavailable_or_moved")
        return None
    if str(after["last_edited_time"]) != revision:
        if index is not None:
            index.upsert_page(database_id, after, parent_pages=after_parents)
            index.record_failure(page_id, "page_changed_during_read")
        return None
    after = {**after, "properties": expanded["properties"],
             "_body_index_full_property_revision": revision}
    if index is not None and content["unsupported_child_page_ids"]:
        parents = {**after_parents, _id(page_id): after}
        registered = True
        for child_id in content["unsupported_child_page_ids"]:
            try:
                child = _request(f"pages/{child_id}", budget)
                _page_ancestors(child, budget, parents)
            except _ReadFailure as exc:
                reasons.add(str(exc))
                registered = False
                continue
            if not _valid_page(child, child_id, database_id, allowed, parents):
                registered = False
                continue
            index.upsert_page(database_id, child, parent_pages=parents)
        if registered:
            # 자식 본문은 부모 증거에 합치지 않는다. 별도 pending 페이지로 수집한다.
            reasons.discard("unsupported_child_pages")
    complete = not reasons
    accessible_reasons = set(content.get("accessible_incomplete_reasons", reasons))
    if "unsupported_child_pages" not in reasons:
        accessible_reasons.discard("unsupported_child_pages")
    accessible_complete = not accessible_reasons and not (reasons - {
        "unsupported_block", "foreign_synced_block", "http_403", "http_404", "synced_source_outside_scope"})
    if index is not None:
        index.upsert_page(database_id, after, parent_pages=after_parents)
        if not index.replace_content(page_id, content["segments"], complete=complete,
                                     expected_edited_time=revision,
                                     coverage={"supported_content_complete": not (reasons - {"unsupported_block"}),
                                               "accessible_content_complete": accessible_complete,
                                               "synced_reference_ids": content["synced_reference_ids"],
                                               "verified_synced_references": content["verified_synced_references"],
                                               "unsupported_elements": content["unsupported_elements"],
                                               "incomplete_reasons": sorted(reasons)}):
            reasons.add("page_changed_during_read")
            complete = False
        if not complete:
            index.record_failure(page_id, ",".join(sorted(reasons)))
    return {"raw_page": after, "database_id": database_id, "content": content["text"],
            "segments": content["segments"], "complete": complete, "incomplete_reasons": sorted(reasons),
            "supported_content_complete": not (reasons - {"unsupported_block"}),
            "unsupported_elements": content["unsupported_elements"],
            "accessible_content_complete": accessible_complete,
            "unsupported_child_page_ids": content["unsupported_child_page_ids"]}


def sync_notion_body_index(database_ids: list[str] | None = None, *, max_pages: int = 100,
                          max_seconds: float = 300, index: BodyIndex | None = None,
                          refresh_catalog: bool = True, refresh_content: bool = False) -> dict[str, Any]:
    """전체 DB 목록을 먼저 갱신한 뒤 저장된 미완료 페이지부터 본문을 채운다."""
    if max_pages < 0 or max_seconds <= 0:
        raise ValueError("max_pages must be nonnegative and max_seconds positive")
    started = time.monotonic()
    deadline = started + max_seconds
    settings = get_notion_settings()
    roots = list(settings.get("database_ids") or [])
    if not settings.get("token") or not roots:
        raise ValueError("Notion token or database list is not configured")
    scope, scope_complete = resolve_notion_read_scope(roots, settings["token"])
    allowed = {_id(identifier) for identifier in scope}
    selected = list(dict.fromkeys(_id(identifier) for identifier in (database_ids if database_ids is not None else scope)))
    if not selected or any(identifier not in allowed for identifier in selected):
        raise ValueError("Requested database is outside the configured Notion read scope")
    index = index if index is not None else BodyIndex()
    reasons: set[str] = set() if scope_complete else {"child_database_discovery"}
    if not refresh_catalog:
        prior = index.status(selected)
        try:
            generated = datetime.fromisoformat(str(prior.get("catalog_updated_at") or ""))
            age = (datetime.now(timezone.utc) - generated).total_seconds()
        except (TypeError, ValueError):
            age = -1
        if not prior.get("catalog_complete") or not 0 <= age <= 86400:
            raise ValueError("Resuming requires a complete catalog refreshed within the last 24 hours")
        reasons.add("catalog_not_refreshed")
    catalog_limit = _configured_int("NOTION_BODY_INDEX_CATALOG_REQUESTS", 10000, 100000)
    budget = _Budget(deadline, catalog_limit)
    for database_id in selected if refresh_catalog else []:
        rows: list[dict[str, Any]] = []
        parent_pages: dict[str, dict[str, Any]] = {}
        cursor = ""
        cursors: set[str] = set()
        catalog_complete = False
        while True:
            payload: dict[str, Any] = {"page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            try:
                data = _request(f"databases/{database_id}/query", budget, payload=payload, post=True)
            except _ReadFailure as exc:
                reasons.add(str(exc))
                break
            values = data.get("results")
            if not isinstance(values, list):
                reasons.add("invalid_response")
                break
            malformed = False
            for raw in values:
                if not isinstance(raw, dict) or not raw.get("id"):
                    malformed = True
                    continue
                if raw.get("archived") or raw.get("in_trash"):
                    continue
                # 위키 DB의 목록에는 하위 DB 객체도 들어온다. 해당 DB는 별도
                # 허용 범위에서 수집하며, 페이지로 저장하거나 본문에 합치지 않는다.
                if raw.get("object") == "database":
                    if _id(raw["id"]) not in allowed:
                        reasons.add("child_database_discovery")
                        malformed = True
                    continue
                try:
                    _page_ancestors(raw, budget, parent_pages)
                except _ReadFailure as exc:
                    reasons.add(str(exc))
                    malformed = True
                    continue
                if not _valid_page(raw, str(raw["id"]), database_id, allowed, parent_pages):
                    malformed = True
                    continue
                rows.append(raw)
            if malformed:
                reasons.add("invalid_catalog_page")
                break
            if not data.get("has_more"):
                catalog_complete = True
                break
            next_cursor = str(data.get("next_cursor") or "")
            if not next_cursor or next_cursor in cursors:
                reasons.add("invalid_cursor")
                break
            cursors.add(next_cursor)
            cursor = next_cursor
        index.upsert_catalog(database_id, rows, complete=catalog_complete, parent_pages=parent_pages)
    if refresh_content:
        index.mark_pending(selected)
    attempted = 0
    completed = 0
    for record in index.pending_pages(selected, max_pages=max_pages) if max_pages else []:
        if time.monotonic() >= deadline:
            reasons.add("time_limit")
            break
        attempted += 1
        result = validate_index_candidate(record, selected, index=index, deadline=deadline,
                                          limits=CrawlLimits.configured())
        if result is None:
            reasons.add("page_revalidation_failed")
        elif result["complete"]:
            completed += 1
        else:
            reasons.update(result["incomplete_reasons"])
    status = index.status(selected)
    return {**status, "complete": bool(status.get("complete") and scope_complete and not reasons),
            "scope_complete": scope_complete, "catalog_refreshed": refresh_catalog,
            "attempted_page_count": attempted,
            "completed_page_count": completed, "incomplete_reasons": sorted(reasons),
            "elapsed_seconds": round(time.monotonic() - started, 3)}
