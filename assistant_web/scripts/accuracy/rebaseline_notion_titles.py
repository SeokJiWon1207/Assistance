#!/usr/bin/env python3
"""Classify historical Notion title checks against the current integration scope."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APP_DIR))

from backend.environment import load_dotenv

load_dotenv(APP_DIR / ".env")

from backend.common import normalize_for_match
from backend.config import NOTION_API_BASE, RUNTIME_DIR, get_notion_settings, notion_post_json
from backend.notion.client import notion_title
from backend.notion.client import fetch_notion_database_schema, fetch_notion_database_title
from backend.notion.database_roles import (
    build_notion_database_role_catalog,
    save_notion_database_role_catalog,
)
from backend.notion.read_scope import (
    preserve_legacy_notion_read_scope,
    refresh_notion_read_scope,
    save_complete_notion_read_scope,
)


REPORT_DIR = RUNTIME_DIR / "accuracy_reports"


def historical_titles() -> list[str]:
    latest: dict[str, list[str]] = {}
    for path in sorted(REPORT_DIR.glob("accuracy-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                question = value.get("question")
                expected = value.get("expected")
                if value.get("source") == "notion" and value.get("expected_kind") == "titles" and isinstance(question, str) and isinstance(expected, list):
                    latest[question] = [str(title) for title in expected if str(title).strip()]
                for child in value.values():
                    walk(child)
            elif isinstance(value, list):
                for child in value:
                    walk(child)

        walk(data)
    return sorted({title for titles in latest.values() for title in titles})


def fetch_page_catalog(database_ids: list[str]) -> tuple[dict[str, list[dict[str, Any]]], str, bool]:
    """등록된 DB를 직접 조회해 전역 검색에서 빠지는 페이지까지 색인한다."""
    catalog: dict[str, list[dict[str, Any]]] = {}
    errors: list[str] = []
    for database_id in database_ids:
        payload: dict[str, Any] = {"page_size": 100}
        for _ in range(100):
            response = notion_post_json(
                f"{NOTION_API_BASE}/databases/{database_id}/query",
                payload,
                timeout=20,
                retries=2,
            )
            if response.status_code >= 400:
                errors.append(f"{database_id}: HTTP {response.status_code}")
                break
            data = response.json()
            for page in data.get("results") or []:
                found_title = notion_title(page.get("properties") or {})
                normalized_title = normalize_for_match(found_title)
                if not normalized_title:
                    continue
                catalog.setdefault(normalized_title, []).append({
                    "page_id": str(page.get("id") or "").replace("-", ""),
                    "title": found_title,
                    "parent_id": database_id.replace("-", ""),
                    "parent_type": "database_id",
                    "url": str(page.get("url") or ""),
                })
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
            if not cursor or cursor == payload.get("start_cursor"):
                errors.append(f"{database_id}: invalid query cursor")
                break
            payload["start_cursor"] = cursor
        else:
            errors.append(f"{database_id}: page catalog limit reached")
    return catalog, "; ".join(errors)[:500], not errors


def build_title_index(
    run_at: str,
    roots: list[str],
    read_scope: dict[str, str],
    catalog: dict[str, list[dict[str, Any]]],
    *,
    scope_complete: bool,
    catalog_complete: bool,
) -> dict[str, Any]:
    return {
        "generated_at": run_at,
        "complete": scope_complete and catalog_complete,
        "scope_complete": scope_complete,
        "catalog_complete": catalog_complete,
        "root_database_ids": roots,
        "read_scope": read_scope,
        "titles": catalog,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()
    settings = get_notion_settings()
    roots = list(settings.get("database_ids") or [])
    if not settings.get("token") or not roots:
        raise SystemExit("Notion token or database list is not configured")
    preserve_legacy_notion_read_scope(roots)
    read_scope, scope_complete = refresh_notion_read_scope(roots, settings["token"])
    if scope_complete:
        save_complete_notion_read_scope(
            roots,
            read_scope,
            datetime.now().astimezone().isoformat(timespec="seconds"),
        )
        role_rows = []
        try:
            for database_id in read_scope:
                role_rows.append((
                    database_id,
                    fetch_notion_database_title(database_id),
                    fetch_notion_database_schema(database_id).get("properties", {}),
                ))
            save_notion_database_role_catalog(build_notion_database_role_catalog(
                role_rows,
                generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
                root_database_ids=roots,
            ))
        except Exception as exc:  # 마지막 완성본을 보존하고 제목 기준선 작업은 계속한다.
            print(f"Notion DB 역할 카탈로그 갱신 실패: {exc}", file=sys.stderr)
    allowed = {identifier.replace("-", "") for identifier in read_scope}
    titles = historical_titles()
    if args.limit > 0:
        titles = titles[: args.limit]

    catalog, catalog_error, catalog_complete = fetch_page_catalog(list(read_scope))
    rows: list[dict[str, Any]] = []
    for index, title in enumerate(titles, start=1):
        matches = catalog.get(normalize_for_match(title), [])
        error = catalog_error
        parents = {match["parent_id"] for match in matches}
        if parents & allowed:
            classification = "in_scope"
        elif matches:
            classification = "outside_scope"
        elif error:
            classification = "dependency_error"
        else:
            classification = "missing_or_inaccessible"
        rows.append({
            "title": title,
            "classification": classification,
            "matches": matches,
            "error": error,
        })
        print(f"{index}/{len(titles)} {classification} {title}", flush=True)

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    counts = {kind: sum(row["classification"] == kind for row in rows) for kind in (
        "in_scope", "outside_scope", "missing_or_inaccessible", "dependency_error",
    )}
    run_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report = {
        "run_at": run_at,
        "scope_complete": scope_complete,
        "catalog_complete": catalog_complete,
        "root_database_count": len(roots),
        "read_database_count": len(read_scope),
        "title_count": len(rows),
        "counts": counts,
        "rows": rows,
    }
    json_path = REPORT_DIR / f"notion-title-rebaseline-{stamp}.json"
    corpus_path = REPORT_DIR / f"current-notion-title-corpus-{stamp}.jsonl"
    index_path = RUNTIME_DIR / "notion_title_index.json"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    index_path.write_text(
        json.dumps(
            build_title_index(
                run_at,
                roots,
                read_scope,
                catalog,
                scope_complete=scope_complete,
                catalog_complete=catalog_complete,
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    corpus_path.write_text("".join(
        json.dumps({"source": "notion", "question": f'노션에서 "{row["title"]}" 찾아줘',
                    "expected_kind": "titles", "expected": [row["title"]]}, ensure_ascii=False) + "\n"
        for row in rows if row["classification"] == "in_scope"
    ), encoding="utf-8")
    print(json.dumps({"report": str(json_path), "corpus": str(corpus_path),
                      "index": str(index_path), "counts": counts}, ensure_ascii=False))
    return 1 if counts["dependency_error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
