#!/usr/bin/env python3
"""기존 수집 종료 후 부분 페이지·일시 오류를 한 번씩 안전하게 재검증한다."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


def alive(pid: int) -> bool:
    if pid <= 0:
        raise ValueError("Invalid process ID")
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for_readers(pids: list[int], deadline: float) -> None:
    # 설정/Notion 클라이언트를 초기화하기 전에 실행한다.
    while any(alive(pid) for pid in pids):
        if time.monotonic() >= deadline:
            raise TimeoutError("existing_reader_still_running")
        time.sleep(20)


def transient(reason: str) -> bool:
    return any(part in {"http_429", "request_failed", "time_limit", "request_limit"}
               or part.startswith("http_5") for part in reason.split(","))


def repair_priority(row: dict[str, Any]) -> int | None:
    reason = str(row.get("last_error") or "")
    if transient(reason):
        return 0
    if "foreign_synced_block" in reason.split(","):
        return 1
    if "unsupported_block" in reason.split(","):
        return 2
    if not row.get("content_policy_current"):
        return 3
    return None


@contextmanager
def repair_lock(output: Path):
    with (output / "supported-repair.lock").open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def write_json(path: Path, data: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def run(output: Path, after_pids: list[int], max_seconds: float) -> None:
    output.mkdir(parents=True, exist_ok=True)
    (output / "supported-repair.pid").write_text(str(os.getpid()) + "\n")
    progress: dict[str, Any] = {"stage": "waiting_existing_reader", "after_pids": after_pids,
                                "pid": os.getpid(), "results": [], "complete": False}
    write_json(output / "supported-repair-progress.json", progress)
    wait_for_readers(after_pids, time.monotonic() + 86400)
    pointer = output / "full-sync.pid"
    if pointer.exists():
        current = int(pointer.read_text().strip())
        if current not in after_pids and current != os.getpid() and alive(current):
            raise RuntimeError("another_collector_started")
    pointer.write_text(str(os.getpid()) + "\n")
    # 기다리는 동안에는 API 요청·색인 쓰기·절전 방지 중복 실행이 없다.
    from backend.environment import load_dotenv
    load_dotenv(APP_DIR / ".env")
    from backend.config import get_notion_settings
    from backend.notion.body_index import open_body_index
    from backend.notion.body_index_sync import CrawlLimits, sync_notion_body_index, validate_index_candidate
    from backend.notion.read_scope import resolve_notion_read_scope

    deadline = time.monotonic() + max_seconds
    inhibitor = subprocess.Popen(["caffeinate", "-i", "-s", "-w", str(os.getpid()), "-t", str(int(max_seconds) + 60)])
    try:
        settings = get_notion_settings()
        scope, complete = resolve_notion_read_scope(settings["database_ids"], settings["token"])
        if not complete:
            raise RuntimeError("read_scope_incomplete")
        index = open_body_index()
        if index is None:
            raise RuntimeError("body_index_unavailable")
        progress["stage"] = "refresh_catalog"
        write_json(output / "supported-repair-progress.json", progress)
        catalog = sync_notion_body_index(list(scope), max_pages=0, max_seconds=min(1800, max_seconds), index=index)
        write_json(output / "supported-repair-catalog.json", catalog)
        if not catalog.get("catalog_complete") or not catalog.get("catalog_fresh"):
            raise RuntimeError("catalog_refresh_incomplete")
        rows = [row for row in index.pending_pages(list(scope), max_pages=200000) if repair_priority(row) is not None]
        rows.sort(key=lambda row: (repair_priority(row), row["page_id"]))
        progress.update(stage="repair", planned=len(rows), started_at=datetime.now(timezone.utc).isoformat())
        for row in rows:
            if time.monotonic() >= deadline:
                break
            result = validate_index_candidate(row, list(scope), index=index, deadline=deadline,
                                              limits=CrawlLimits.configured())
            progress["results"].append({"page_id": row["page_id"], "previous_reason": row["last_error"],
                "complete": bool(result and result["complete"]),
                "supported_content_complete": bool(result and result["supported_content_complete"]),
                "unsupported_elements": (result or {}).get("unsupported_elements", []),
                "reasons": (result or {}).get("incomplete_reasons", ["validation_failed"]),
                "synced_segments": [seg for seg in (result or {}).get("segments", []) if seg.get("synced_source_id")]})
            progress.update(attempted=len(progress["results"]), updated_at=datetime.now(timezone.utc).isoformat())
            write_json(output / "supported-repair-progress.json", progress)
        progress.update(stage="repair_pass_finished", pass_finished=len(progress["results"]) == len(rows),
                        status=index.status(list(scope)), complete=False,
                        next_step="independent_source_and_full_scope_search_verification")
        write_json(output / "supported-repair-progress.json", progress)
    finally:
        inhibitor.terminate()
        inhibitor.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--after-pid", action="append", type=int, default=[])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-seconds", type=float, default=21600)
    args = parser.parse_args()
    if args.max_seconds <= 0 or any(pid <= 0 for pid in args.after_pid):
        parser.error("positive time budget and process IDs required")
    args.output.mkdir(parents=True, exist_ok=True)
    try:
        with repair_lock(args.output):
            run(args.output, args.after_pid, args.max_seconds)
    except Exception as exc:
        # HTTP 예외나 설정 내용/자격증명은 로그에 노출하지 않는다.
        write_json(args.output / "supported-repair-error.json", {"error_type": type(exc).__name__,
                   "at": datetime.now(timezone.utc).isoformat()})
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
