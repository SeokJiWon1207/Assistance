#!/usr/bin/env python3
"""Run pip-audit with reviewed, expiring vulnerability exceptions."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any


APP_DIR = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = APP_DIR / "requirements.lock"
DEFAULT_EXCEPTIONS = APP_DIR / "security" / "pip-audit-ignores.json"


def load_active_exceptions(path: Path, *, today: date | None = None) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    exceptions = payload.get("exceptions")
    if not isinstance(exceptions, list):
        raise ValueError("pip-audit 예외 파일의 exceptions는 배열이어야 합니다.")

    current_date = today or date.today()
    active: list[dict[str, Any]] = []
    for item in exceptions:
        if not isinstance(item, dict):
            raise ValueError("pip-audit 예외 항목은 객체여야 합니다.")
        vulnerability_id = str(item.get("id") or "").strip()
        expires_at_raw = str(item.get("expiresAt") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not vulnerability_id or not expires_at_raw or not reason:
            raise ValueError("pip-audit 예외에는 id, expiresAt, reason이 필요합니다.")
        expires_at = date.fromisoformat(expires_at_raw)
        if expires_at < current_date:
            raise RuntimeError(f"pip-audit 예외가 만료되었습니다: {vulnerability_id} ({expires_at})")
        active.append({**item, "id": vulnerability_id, "expiresAt": expires_at_raw, "reason": reason})
    return active


def build_audit_command(requirements: Path, exceptions: list[dict[str, Any]]) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pip_audit",
        "--disable-pip",
        "--no-deps",
        "-r",
        str(requirements),
    ]
    for item in exceptions:
        command.extend(["--ignore-vuln", str(item["id"])])
    return command


def main() -> int:
    requirements = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_REQUIREMENTS
    exceptions = load_active_exceptions(DEFAULT_EXCEPTIONS)
    for item in exceptions:
        print(
            f"WARNING: {item['id']} 예외 적용 (만료 {item['expiresAt']}): {item['reason']}",
            file=sys.stderr,
        )
    return subprocess.run(build_audit_command(requirements, exceptions), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
