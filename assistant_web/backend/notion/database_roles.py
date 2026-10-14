"""Notion DB 역할 카탈로그를 읽고 안전하게 검색 범위를 좁힌다."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

from backend.common import normalize_for_match
from backend.config import CONFIG_DIR, RUNTIME_DIR


ROLE_CONFIG_FILENAME = "notion_database_roles.json"
ROLE_CATALOG_FILENAME = "notion_database_roles.json"


def infer_notion_database_roles(title: str, properties: dict[str, dict[str, Any]]) -> list[str]:
    """제목과 구별력이 높은 속성 조합으로만 역할을 추정한다."""
    normalized_title = normalize_for_match(title)
    normalized_properties = {normalize_for_match(name) for name in properties}
    roles: list[str] = []
    if normalized_title == "이슈리스트" or {
        "이슈할당", "등록일", "고객사", "매체",
    }.issubset(normalized_properties):
        roles.append("issue")
    if "기술공유" in normalized_title:
        roles.append("document")
    if "버전관리" in normalized_title:
        roles.append("release")
    if normalized_title.startswith("db02"):
        roles.append("project")
    if normalized_title.startswith("db03") or normalized_title.endswith("작업"):
        roles.append("task")
    return roles


def build_notion_database_role_catalog(
    database_rows: Iterable[tuple[str, str, dict[str, dict[str, Any]]]],
    *,
    generated_at: str,
    root_database_ids: list[str],
) -> dict[str, Any]:
    databases = {
        database_id.replace("-", "").lower(): {
            "title": title,
            "roles": infer_notion_database_roles(title, properties),
            "source": "inferred",
        }
        for database_id, title, properties in database_rows
    }
    return {
        "generated_at": generated_at,
        "complete": True,
        "root_database_ids": [value.replace("-", "").lower() for value in root_database_ids],
        "databases": databases,
    }


def save_notion_database_role_catalog(catalog: dict[str, Any]) -> Path:
    """완성된 카탈로그만 런타임 경로에 원자적으로 교체한다."""
    if catalog.get("complete") is not True or not catalog.get("databases"):
        raise ValueError("only a complete Notion database role catalog can be saved")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    path = RUNTIME_DIR / ROLE_CATALOG_FILENAME
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    _role_catalog_cached.cache_clear()
    return path


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@lru_cache(maxsize=8)
def _role_catalog_cached(signature: tuple[tuple[str, int, int], ...]) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    paths = [Path(item[0]) for item in signature]
    runtime = _read_json(paths[0]) if paths else {}
    if runtime.get("complete") is True:
        merged.update(runtime.get("databases") or {})
    configured = _read_json(paths[1]) if len(paths) > 1 else {}
    for database_id, row in (configured.get("databases") or {}).items():
        normalized_id = str(database_id).replace("-", "").lower()
        configured_row = row if isinstance(row, dict) else {"roles": row}
        merged[normalized_id] = {**merged.get(normalized_id, {}), **configured_row, "source": "configured"}
    return merged


def load_notion_database_roles() -> dict[str, dict[str, Any]]:
    paths = [RUNTIME_DIR / ROLE_CATALOG_FILENAME, CONFIG_DIR / ROLE_CONFIG_FILENAME]
    signature: list[tuple[str, int, int]] = []
    for path in paths:
        try:
            stat = path.stat()
            signature.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            signature.append((str(path), 0, 0))
    return dict(_role_catalog_cached(tuple(signature)))


def select_notion_database_ids(
    database_ids: list[str], query_text: str, terms: list[str], constraints: dict[str, Any],
) -> tuple[list[str], str]:
    """자유 검색어가 없는 조건형 이슈 질문만 이슈 DB로 좁힌다.

    카탈로그에 없는 DB는 항상 포함하므로 새 DB 때문에 거짓 0건을 만들지 않는다.
    """
    if (terms or constraints.get("database_label") or constraints.get("exact_title")
            or constraints.get("body_filter_clauses") or "이슈" not in normalize_for_match(query_text)):
        return database_ids, ""
    has_condition = bool(
        constraints.get("status")
        or constraints.get("owner")
        or constraints.get("unassigned_owner")
        or constraints.get("group_by")
        or constraints.get("common_filter_clauses")
    )
    if not has_condition:
        return database_ids, ""
    catalog = load_notion_database_roles()
    normalized = {value.replace("-", "").lower(): value for value in database_ids}
    unknown = [
        original for key, original in normalized.items()
        if key not in catalog
        or not catalog[key].get("roles")
        or catalog[key].get("source") != "configured"
    ]
    matched = [
        original for key, original in normalized.items()
        if "issue" in set(catalog.get(key, {}).get("roles") or [])
    ]
    selected = list(dict.fromkeys([*matched, *unknown]))
    return (selected, "issue") if matched and len(selected) < len(database_ids) else (database_ids, "")


__all__ = [
    "build_notion_database_role_catalog",
    "infer_notion_database_roles",
    "load_notion_database_roles",
    "save_notion_database_role_catalog",
    "select_notion_database_ids",
]
