"""Liveness/readiness checks and the static application entry point."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

from backend.auth import users_db_path
from backend.config import AUDIT_DB_PATH, CHROMA_DB_PATH, MANTIS_INDEX_DB_PATH, STATIC_DIR
from backend.sources.mantis import get_mantis_snapshot_freshness_status
from backend.startup_validation import get_model_transport_security_status
from backend.workspace import workspace_db_path


router = APIRouter()


@router.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


def _sqlite_check(
    path: Path,
    required_table: str,
    *,
    verify_integrity: bool = True,
) -> Dict[str, Any]:
    if not path.exists():
        return {"status": "error", "reason": "missing"}
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
            quick_check = (
                str(connection.execute("PRAGMA quick_check").fetchone()[0])
                if verify_integrity
                else "ok"
            )
            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (required_table,),
            ).fetchone()
    except sqlite3.Error as exc:
        return {"status": "error", "reason": type(exc).__name__}
    if quick_check != "ok" or table is None:
        return {
            "status": "error",
            "reason": "integrity" if quick_check != "ok" else "schema",
        }
    return {"status": "ok"}


@router.get("/ready")
def readiness() -> JSONResponse:
    model_transport = get_model_transport_security_status()
    checks = {
        "usersDb": _sqlite_check(users_db_path(), "users"),
        "workspaceDb": _sqlite_check(workspace_db_path(), "conversations"),
        "auditDb": _sqlite_check(AUDIT_DB_PATH, "validation_logs"),
        # 수백 MB 스냅샷에 PRAGMA quick_check를 매 요청마다 실행하면 readiness가
        # 수십 초 동안 블로킹된다. 전체 무결성은 원자 재구축/배포 단계에서 검증하고,
        # 여기서는 읽기 전용 open과 필수 스키마 존재만 확인한다.
        "snapshotDb": _sqlite_check(
            CHROMA_DB_PATH,
            "embedding_metadata",
            verify_integrity=False,
        ),
        # 보조 인덱스는 점진적으로 배포하며, 없거나 손상된 경우 Chroma 순회로
        # 폴백하므로 readiness 자체를 막지는 않는다.
        "structuredIndexDb": _sqlite_check(
            MANTIS_INDEX_DB_PATH,
            "issues",
            verify_integrity=False,
        ),
    }
    required_checks = ("usersDb", "workspaceDb", "auditDb", "snapshotDb")
    ready = all(checks[name]["status"] == "ok" for name in required_checks) and model_transport[
        "status"
    ] not in {"error", "expired"}
    payload = {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "freshness": {"mantis": get_mantis_snapshot_freshness_status()},
        "security": {"modelTransport": model_transport},
    }
    return JSONResponse(payload, status_code=200 if ready else 503)


@router.get("/")
def index() -> FileResponse:
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
        },
    )


@router.get("/api/app-version")
def app_version() -> Dict[str, str]:
    """열려 있는 화면이 새 운영 릴리스를 감지할 수 있게 한다."""
    return {"version": os.getenv("APP_RELEASE_ID", "development").strip() or "development"}
