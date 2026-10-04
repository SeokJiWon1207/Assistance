"""Mantis snapshot scans with a structured-index shadow and safe fallback."""

from __future__ import annotations

import heapq
import logging
import os
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional

from backend.tools.mantis_index import (
    INDEX_FILENAME,
    MantisIndexError,
    build_candidate_query,
    validate_mantis_index,
)


LOGGER = logging.getLogger(__name__)
VALID_INDEX_MODES = {"off", "shadow", "primary"}


@lru_cache(maxsize=8)
def _validate_index_signature(path: str, _mtime_ns: int, _size: int) -> None:
    validate_mantis_index(Path(path))


def _empty_result(reason: str, scanned_count: int = 0) -> Dict[str, Any]:
    return {
        "available": False,
        "scanned_count": scanned_count,
        "matched_count": 0,
        "matched_ids": set(),
        "issues": [],
        "group_counts": {},
        "reason": reason,
    }


def _scan_documents(
    documents: Iterable[str],
    matches: Callable[[str], bool],
    normalize: Callable[[str], Optional[Dict[str, Any]]],
    *,
    example_limit: int,
    scanned_count: int | None = None,
    group_key: Optional[Callable[[Dict[str, Any]], str]] = None,
) -> Dict[str, Any]:
    scanned_ids: set[int] = set()
    matched_ids: set[int] = set()
    group_counts: Dict[str, int] = {}
    matched_groups: Dict[int, str] = {}
    examples: list[Dict[str, Any]] = []

    for content in documents:
        issue = normalize(str(content or ""))
        if not issue or not issue.get("id"):
            continue
        issue_id = int(issue["id"])
        if issue_id in scanned_ids:
            continue
        scanned_ids.add(issue_id)
        if not matches(str(content or "")):
            continue
        matched_ids.add(issue_id)
        if group_key:
            label = str(group_key(issue) or "").strip() or "미지정"
            group_counts[label] = group_counts.get(label, 0) + 1
            matched_groups[issue_id] = label
        if len(examples) < example_limit:
            examples.append(issue)
        elif issue_id > min(int(item.get("id") or 0) for item in examples):
            oldest_index = min(
                range(len(examples)),
                key=lambda index: int(examples[index].get("id") or 0),
            )
            examples[oldest_index] = issue

    examples.sort(key=lambda issue: int(issue.get("id") or 0), reverse=True)
    return {
        "available": True,
        "scanned_count": len(scanned_ids) if scanned_count is None else scanned_count,
        "matched_count": len(matched_ids),
        "matched_ids": matched_ids,
        "issues": examples,
        "group_counts": group_counts,
        "matched_groups": matched_groups,
        "reason": "",
    }


def _scan_chroma_documents(
    database_path: Path,
    matches: Callable[[str], bool],
    normalize: Callable[[str], Optional[Dict[str, Any]]],
    *,
    example_limit: int,
    scan_limit: Optional[int],
    group_key: Optional[Callable[[Dict[str, Any]], str]],
) -> Dict[str, Any]:
    if not database_path.exists():
        return _empty_result("Mantis 로컬 인덱스가 없습니다.")

    try:
        with closing(sqlite3.connect(database_path)) as connection:
            cursor = connection.execute(
                "SELECT string_value FROM embedding_metadata WHERE key = 'chroma:document'"
            )
            if scan_limit:
                newest_documents: list[tuple[int, str]] = []
                scanned_ids: set[int] = set()
                for (content,) in cursor:
                    issue = normalize(str(content or ""))
                    if not issue or not issue.get("id"):
                        continue
                    issue_id = int(issue["id"])
                    if issue_id in scanned_ids:
                        continue
                    scanned_ids.add(issue_id)
                    item = (issue_id, str(content or ""))
                    if len(newest_documents) < scan_limit:
                        heapq.heappush(newest_documents, item)
                    elif issue_id > newest_documents[0][0]:
                        heapq.heapreplace(newest_documents, item)
                selected = [content for _, content in sorted(newest_documents, reverse=True)]
                return _scan_documents(
                    selected,
                    matches,
                    normalize,
                    example_limit=example_limit,
                    scanned_count=len(selected),
                    group_key=group_key,
                )
            return _scan_documents(
                (str(content or "") for (content,) in cursor),
                matches,
                normalize,
                example_limit=example_limit,
                group_key=group_key,
            )
    except sqlite3.Error as exc:
        return _empty_result(f"Mantis 로컬 인덱스 조회 실패: {exc}")


def _scan_structured_index(
    index_path: Path,
    matches: Callable[[str], bool],
    normalize: Callable[[str], Optional[Dict[str, Any]]],
    *,
    constraints: Optional[Dict[str, Any]],
    example_limit: int,
    scan_limit: Optional[int],
    group_key: Optional[Callable[[Dict[str, Any]], str]],
) -> Dict[str, Any]:
    try:
        stat = index_path.stat()
        _validate_index_signature(str(index_path), stat.st_mtime_ns, stat.st_size)
        query, params = build_candidate_query(constraints, scan_limit)
        with closing(
            sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
        ) as connection:
            total_count = int(connection.execute("SELECT COUNT(*) FROM issues").fetchone()[0])
            documents = [str(row[0] or "") for row in connection.execute(query, params)]
        return _scan_documents(
            documents,
            matches,
            normalize,
            example_limit=example_limit,
            scanned_count=min(total_count, scan_limit) if scan_limit else total_count,
            group_key=group_key,
        )
    except (OSError, sqlite3.Error, MantisIndexError) as exc:
        return _empty_result(f"Mantis 구조화 인덱스 조회 실패: {exc}")


def _results_match(left: Dict[str, Any], right: Dict[str, Any]) -> bool:
    comparable_keys = (
        "available",
        "scanned_count",
        "matched_count",
        "matched_ids",
        "issues",
        "group_counts",
    )
    return all(left.get(key) == right.get(key) for key in comparable_keys)


def scan_mantis_documents(
    database_path: Path,
    matches: Callable[[str], bool],
    normalize: Callable[[str], Optional[Dict[str, Any]]],
    example_limit: int = 10,
    scan_limit: Optional[int] = None,
    group_key: Optional[Callable[[Dict[str, Any]], str]] = None,
    constraints: Optional[Dict[str, Any]] = None,
    index_database_path: Optional[Path] = None,
    use_raw_constraints: bool = False,
) -> Dict[str, Any]:
    """Count and sample matching issues with shadow comparison and fallback.

    ``MANTIS_INDEX_MODE`` controls rollout: ``off`` always scans Chroma,
    ``shadow`` (default) compares both engines but returns the legacy result,
    and ``primary`` returns the structured result when it is healthy.
    """
    from backend.config import CHROMA_DB_PATH
    from backend.sources.mantis_store import read_complete_store, read_store_metadata, render_document, start_store_refresh
    stored = read_complete_store() if database_path == CHROMA_DB_PATH else None
    if stored is not None:
        selected = stored[:scan_limit] if scan_limit else stored
        scanned_count = len(selected)
        if use_raw_constraints and constraints is not None:
            from backend.sources.mantis import select_mantis_rows
            selected = select_mantis_rows(selected, constraints)
        result = _scan_documents(
            (render_document(raw) for raw in selected),
            (lambda _text: True) if use_raw_constraints and constraints is not None else matches, normalize,
            example_limit=example_limit, group_key=group_key, scanned_count=scanned_count,
        )
        result.update(stored.meta)
        # 명시한 최신 N건 창은 그 범위 안에서만 완료이다.
        result["search_complete"] = not scan_limit or len(stored) <= scan_limit
        return result
    if database_path == CHROMA_DB_PATH:
        metadata = read_store_metadata(allow_stale=True)
        if metadata and not metadata["search_complete"]:
            # 만료된 전체 저장본을 더 오래된 인덱스로 대체하지 않는다.
            # CLI/서버 수명주기 밖의 검색도 다음 요청에서 새 세대를 쓸 수 있다.
            start_store_refresh()
            return {**metadata, **_empty_result("Mantis 전체 저장본의 갱신이 지연되어 새 데이터를 수집 중입니다."),
                    "refreshPending": True}
    mode = os.getenv("MANTIS_INDEX_MODE", "shadow").strip().lower()
    if mode not in VALID_INDEX_MODES:
        LOGGER.warning("알 수 없는 MANTIS_INDEX_MODE=%s; shadow로 처리합니다.", mode)
        mode = "shadow"
    index_path = index_database_path or Path(
        os.getenv("MANTIS_INDEX_DB_PATH", str(database_path.parent / INDEX_FILENAME))
    ).expanduser()

    legacy_result: Dict[str, Any] | None = None
    if mode in {"off", "shadow"}:
        legacy_result = _scan_chroma_documents(
            database_path,
            matches,
            normalize,
            example_limit=example_limit,
            scan_limit=scan_limit,
            group_key=group_key,
        )
        if mode == "off":
            return legacy_result

    structured_result = _scan_structured_index(
        index_path,
        matches,
        normalize,
        constraints=constraints,
        example_limit=example_limit,
        scan_limit=scan_limit,
        group_key=group_key,
    )
    if mode == "primary":
        if structured_result["available"]:
            return structured_result
        LOGGER.warning("구조화 인덱스를 사용할 수 없어 Chroma 순회로 폴백합니다: %s", structured_result["reason"])
        return _scan_chroma_documents(
            database_path,
            matches,
            normalize,
            example_limit=example_limit,
            scan_limit=scan_limit,
            group_key=group_key,
        )

    assert legacy_result is not None
    if structured_result["available"] and not _results_match(legacy_result, structured_result):
        LOGGER.warning(
            "Mantis 구조화 인덱스 shadow 결과 불일치: legacy=%s structured=%s",
            {
                "scanned_count": legacy_result.get("scanned_count"),
                "matched_count": legacy_result.get("matched_count"),
                "matched_ids": sorted(legacy_result.get("matched_ids") or set())[:20],
            },
            {
                "scanned_count": structured_result.get("scanned_count"),
                "matched_count": structured_result.get("matched_count"),
                "matched_ids": sorted(structured_result.get("matched_ids") or set())[:20],
            },
        )
    return legacy_result
