"""Fast count scans over the local Mantis Chroma document snapshot."""

from __future__ import annotations

import sqlite3
import heapq
from pathlib import Path
from typing import Any, Callable, Dict, Optional


def scan_mantis_documents(
    database_path: Path,
    matches: Callable[[str], bool],
    normalize: Callable[[str], Optional[Dict[str, Any]]],
    example_limit: int = 10,
    scan_limit: Optional[int] = None,
) -> Dict[str, Any]:
    if not database_path.exists():
        return {
            "available": False,
            "scanned_count": 0,
            "matched_count": 0,
            "issues": [],
            "reason": "Mantis 로컬 인덱스가 없습니다.",
        }

    scanned_ids = set()
    matched_ids: set[int] = set()
    matched_count = 0
    examples = []
    newest_documents = []
    try:
        with sqlite3.connect(database_path) as connection:
            cursor = connection.execute(
                "SELECT string_value FROM embedding_metadata WHERE key = 'chroma:document'"
            )
            for (content,) in cursor:
                issue = normalize(str(content or ""))
                if not issue or not issue.get("id"):
                    continue
                issue_id = int(issue["id"])
                if issue_id in scanned_ids:
                    continue
                scanned_ids.add(issue_id)
                if scan_limit:
                    item = (issue_id, issue, str(content or ""))
                    if len(newest_documents) < scan_limit:
                        heapq.heappush(newest_documents, item)
                    elif issue_id > newest_documents[0][0]:
                        heapq.heapreplace(newest_documents, item)
                    continue
                if not matches(str(content or "")):
                    continue
                matched_count += 1
                matched_ids.add(issue_id)
                if len(examples) < example_limit:
                    examples.append(issue)
                elif issue_id > min(int(item.get("id") or 0) for item in examples):
                    oldest_index = min(range(len(examples)), key=lambda index: int(examples[index].get("id") or 0))
                    examples[oldest_index] = issue
    except sqlite3.Error as exc:
        return {
            "available": False,
            "scanned_count": len(scanned_ids),
            "matched_count": 0,
            "matched_ids": set(),
            "issues": [],
            "reason": f"Mantis 로컬 인덱스 조회 실패: {exc}",
        }

    if scan_limit:
        selected = sorted(newest_documents, key=lambda item: item[0], reverse=True)
        scanned_count = len(selected)
        for _, issue, content in selected:
            if not matches(content):
                continue
            matched_count += 1
            matched_ids.add(int(issue["id"]))
            if len(examples) < example_limit:
                examples.append(issue)
    else:
        scanned_count = len(scanned_ids)
    examples.sort(key=lambda issue: int(issue.get("id") or 0), reverse=True)
    return {
        "available": True,
        "scanned_count": scanned_count,
        "matched_count": matched_count,
        "matched_ids": matched_ids,
        "issues": examples,
        "reason": "",
    }
