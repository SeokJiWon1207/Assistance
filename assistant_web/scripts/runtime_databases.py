"""서비스를 중지한 배포 구간에서 SQLite 이관·백업·복구를 수행한다."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from pathlib import Path


INITIALIZE = "from backend.auth import init_users_db; from backend.workspace import init_workspace_db; from backend.audit import init_audit_db; init_users_db(); init_workspace_db(); init_audit_db()"


def copy_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(dir=destination.parent, prefix=".sqlite-copy-")
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True)) as original:
            with closing(sqlite3.connect(temporary)) as copied:
                original.backup(copied)
                if copied.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError(f"DB 무결성 검사 실패: {source.name}")
        temporary.chmod(0o600)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def backup_runtime(state: Path, backup: Path) -> None:
    backup.mkdir(parents=True, exist_ok=False, mode=0o700)
    names = []
    for database in sorted(state.glob("*.sqlite3")):
        # 원자 게시 전 수집 파일은 hot journal이 남아 있을 수 있으며 복구 대상이 아니다.
        if database.name.startswith("mantis-complete-"):
            continue
        copy_database(database, backup / database.name)
        names.append(database.name)
    # 완료 manifest 이전에 실패한 백업은 복구에 사용하지 않는다.
    manifest = backup / "manifest.json"
    manifest.write_text(json.dumps(names), encoding="utf-8")
    manifest.chmod(0o600)


def import_legacy_runtime(legacy: Path, state: Path) -> None:
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("users", "workspace", "assistant_audit", "notion_actions"):
        source, destination = legacy / f"{name}.sqlite3", state / f"{name}.sqlite3"
        if source.exists() and not destination.exists():
            copy_database(source, destination)


def restore_runtime(backup: Path, state: Path) -> None:
    names = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(names, list) or any(
        not isinstance(name, str) or Path(name).name != name or not name.endswith(".sqlite3") for name in names
    ):
        raise ValueError("올바르지 않은 DB 백업 목록입니다.")
    # 어떤 파일도 교체하기 전에 백업 전체를 검증한다.
    with tempfile.TemporaryDirectory(dir=state) as temporary:
        verified = Path(temporary)
        for name in names:
            copy_database(backup / name, verified / name)
        for database in state.glob("*.sqlite3"):
            for suffix in ("-wal", "-shm", "-journal"):
                Path(str(database) + suffix).unlink(missing_ok=True)
            if database.name not in names:
                database.unlink()
        for name in names:
            (verified / name).replace(state / name)


def initialize_runtime(app: Path, state: Path) -> None:
    environment = {
        **os.environ,
        "ASSISTANT_RUNTIME_DIR": str(state),
        "AUTH_USERS_DB_PATH": str(state / "users.sqlite3"),
        "WORKSPACE_DB_PATH": str(state / "workspace.sqlite3"),
        "AUDIT_DB_PATH": str(state / "assistant_audit.sqlite3"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    subprocess.run([str(app / ".venv/bin/python"), "-B", "-c", INITIALIZE], cwd=app, env=environment, check=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("backup", "restore", "import", "check", "initialize"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--legacy", type=Path)
    parser.add_argument("--app", type=Path)
    args = parser.parse_args()
    if args.mode == "backup" and args.backup:
        backup_runtime(args.state, args.backup)
    elif args.mode == "restore" and args.backup:
        restore_runtime(args.backup, args.state)
    elif args.mode == "import" and args.legacy:
        import_legacy_runtime(args.legacy, args.state)
    elif args.mode == "initialize" and args.app:
        initialize_runtime(args.app, args.state)
    elif args.mode == "check" and args.app:
        # 큰 DB의 검사 사본은 운영 DB와 같은 디스크에 두어 /tmp tmpfs를 사용하지 않는다.
        args.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix="assistant-preflight-", dir=args.state) as temporary:
            copied = Path(temporary) / "state"
            backup_runtime(args.state, copied)
            if args.legacy:
                import_legacy_runtime(args.legacy, copied)
            initialize_runtime(args.app, copied)
    else:
        parser.error("선택한 작업에 필요한 경로가 없습니다.")


if __name__ == "__main__":
    main()
