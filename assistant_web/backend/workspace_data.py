"""Fast, shared access to the local Mantis snapshot used by workspace screens."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Tuple

from backend.common import normalize_chroma_issue_document
from backend.config import CHROMA_DB_PATH
from backend.tools.mantis_index import INDEX_FILENAME
from backend.tools.mantis_snapshot import scan_mantis_documents


def _signature(path: Path) -> tuple[int, int]:
    try:
        stat = path.stat()
        return stat.st_mtime_ns, stat.st_size
    except OSError:
        return 0, 0


@lru_cache(maxsize=8)
def _load_snapshot_cached(
    database_path: str,
    database_mtime_ns: int,
    database_size: int,
    index_path: str,
    index_mtime_ns: int,
    index_size: int,
    index_mode: str,
    limit: int,
) -> Tuple[tuple[Dict[str, Any], ...], Dict[str, Any]]:
    del database_mtime_ns, database_size, index_mtime_ns, index_size, index_mode
    result = scan_mantis_documents(
        Path(database_path),
        matches=lambda _text: True,
        normalize=normalize_chroma_issue_document,
        example_limit=limit,
        scan_limit=max(limit, 3000),
        index_database_path=Path(index_path),
    )
    meta = {
        "status": "snapshot" if result.get("available") else "error",
        "reason": str(result.get("reason") or "")[:300],
        "matchedCount": int(result.get("matched_count") or 0),
    }
    return tuple(result.get("issues") or []), meta


def load_workspace_snapshot(limit: int = 1000) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Return snapshot rows, reusing parsed data until either index file changes."""
    bounded_limit = max(100, min(int(limit), 3000))
    database_path = Path(CHROMA_DB_PATH)
    index_path = Path(os.getenv("MANTIS_INDEX_DB_PATH", str(database_path.parent / INDEX_FILENAME))).expanduser()
    database_signature = _signature(database_path)
    index_signature = _signature(index_path)
    issues, meta = _load_snapshot_cached(
        str(database_path),
        *database_signature,
        str(index_path),
        *index_signature,
        os.getenv("MANTIS_INDEX_MODE", "shadow").strip().lower(),
        bounded_limit,
    )
    return list(issues), dict(meta)


def clear_workspace_snapshot_cache() -> None:
    _load_snapshot_cached.cache_clear()
