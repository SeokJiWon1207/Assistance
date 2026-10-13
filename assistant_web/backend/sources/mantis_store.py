"""Mantis 원본 전체 세대. 외부 모델 없이 수집하고 SQLite를 원자 교체한다."""
from __future__ import annotations

from contextlib import closing, contextmanager
from contextvars import ContextVar
from functools import wraps
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Event, Thread, Lock
import time
from typing import Any, Callable

import requests
from fastapi import HTTPException

from backend.config import RUNTIME_DIR, get_mantis_settings
from backend.sources.mantis_collection import IssueBatch, iter_pages_in_order
from backend.timing import measured
from backend.sources.immutable import freeze

LOGGER = logging.getLogger(__name__)
MAX_AGE_SECONDS = 3600
_request_store: ContextVar[dict | None] = ContextVar("mantis_request_store", default=None)
_generation_lock = Lock()
_generation_cache: dict[tuple, tuple] = {}
_refresh_lock = Lock()
_refresh_thread: Thread | None = None


def _generation_key(path: Path) -> tuple:
    def stamp(target):
        try:
            stat = target.stat()
            return (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
        except FileNotFoundError:
            return None
    return (str(path.resolve()), connection_key(), stamp(path), stamp(Path(str(path) + "-wal")))


@contextmanager
def store_request_scope():
    """한 요청의 검색 단계가 동일 세대를 공유한다. 요청 종료 시 원본을 해제한다."""
    existing = _request_store.get()
    if existing is not None:
        yield
        return
    token = _request_store.set({})
    try:
        yield
    finally:
        _request_store.reset(token)


def with_store_scope(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with store_request_scope():
            return function(*args, **kwargs)
    return wrapped



def store_path() -> Path:
    return RUNTIME_DIR / "mantis_complete.sqlite3"


def connection_key() -> str:
    settings = get_mantis_settings()
    return hashlib.sha256(json.dumps([settings["url"], settings["token"]]).encode()).hexdigest()


def read_store_metadata(*, allow_stale: bool = False) -> dict[str, Any] | None:
    path = store_path()
    if not path.exists():
        return None
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            meta = json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])
        if meta["connection"] != connection_key():
            return None
        age = time.time() - datetime.fromisoformat(meta["fetchedAt"]).timestamp()
        fresh = 0 <= age < MAX_AGE_SECONDS
        if not fresh and not allow_stale:
            return None
        return {
            "status": "snapshot", "fetchedAt": meta["fetchedAt"], "count": meta["count"],
            "search_complete": fresh, "snapshot_complete": True,
            "incomplete_reasons": [] if fresh else ["stale_snapshot"],
            "reason": "" if fresh else "저장 데이터의 갱신이 지연되었습니다.",
        }
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, HTTPException):
        return None


@measured("store_read")
def read_complete_store(*, allow_stale: bool = False, summary_only: bool = False) -> IssueBatch | None:
    scope = _request_store.get()
    try:
        key = (str(store_path()), connection_key(), allow_stale, summary_only) if scope is not None else None
    except HTTPException:
        return None
    if scope is not None and key in scope:
        return scope[key]
    rows = _read_complete_store(allow_stale=allow_stale, summary_only=summary_only)
    if rows is not None and scope is not None:
        scope[key] = rows
    return rows


def _read_complete_store(*, allow_stale: bool = False, summary_only: bool = False) -> IssueBatch | None:
    # 하나의 세대만 유지하고 동시 요청의 중복 디코딩을 막는다. 요청별 목록과
    # 메타데이터는 분리하되 중첩 원본은 읽기 전용으로 공유한다.
    try:
        with _generation_lock:
            generation = _generation_key(store_path())
            key = (generation, summary_only)
            cached = _generation_cache.get(key)
            if cached is None:
                rows = _load_complete_store(allow_stale=True, summary_only=summary_only)
                if rows is None:
                    return None
                if generation != _generation_key(store_path()):
                    # 읽는 동안 게시된 세대는 다음 요청에서 다시 읽는다.
                    return None
                cached = (tuple(freeze(row) for row in rows), rows.meta["fetchedAt"])
                for old in list(_generation_cache):
                    if old[0] != generation:
                        del _generation_cache[old]
                _generation_cache[key] = cached
            age = time.time() - datetime.fromisoformat(cached[1]).timestamp()
            fresh = 0 <= age < MAX_AGE_SECONDS
            if not fresh and not allow_stale:
                return None
            return IssueBatch(cached[0], {
                "status": "snapshot", "fetchedAt": cached[1],
                "search_complete": fresh, "snapshot_complete": True,
                "incomplete_reasons": [] if fresh else ["stale_snapshot"],
                "reason": "" if fresh else "저장 데이터의 갱신이 지연되었습니다.",
            })
    except (OSError, ValueError, TypeError, HTTPException):
        return None


def _load_complete_store(*, allow_stale: bool = False, summary_only: bool = False) -> IssueBatch | None:
    # 메타데이터와 원본은 같은 연결에서 읽어 교체 중 서로 다른 세대를 합치지 않는다.
    path = store_path()
    if not path.exists():
        return None
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            db.execute("BEGIN")
            saved = json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])
            if saved["connection"] != connection_key():
                return None
            age = time.time() - datetime.fromisoformat(saved["fetchedAt"]).timestamp()
            fresh = 0 <= age < MAX_AGE_SECONDS
            if not fresh and not allow_stale:
                return None
            # 업무 집계에 필요 없는 본문/첨부/변경 이력을 메모리에 적재하지 않는다.
            omitted = ["history", "attachments", "monitors", "relationships"]
            if summary_only:
                omitted += ["description", "steps_to_reproduce", "notes", "additional_information"]
            paths = ", ".join("'$." + key + "'" for key in omitted)
            rows = [json.loads(row[0]) for row in db.execute(f"SELECT json_remove(payload, {paths}) FROM issues ORDER BY id DESC")]
            if len(rows) != saved["count"]:
                return None
        return IssueBatch(rows, {
            "status": "snapshot", "fetchedAt": saved["fetchedAt"],
            "search_complete": fresh, "snapshot_complete": True,
            "incomplete_reasons": [] if fresh else ["stale_snapshot"],
            "reason": "" if fresh else "저장 데이터의 갱신이 지연되었습니다.",
        })
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError, HTTPException):
        return None


def _collect_pass(db, fetch, *, page_size: int, max_pages: int, workers: int, started: float):
    db.execute("DELETE FROM issues")
    issue_ids: set[int] = set()
    duplicate = False
    pages = iter_pages_in_order(fetch, max_pages=max_pages, workers=workers)
    try:
        for _, batch in pages:
            if time.monotonic() - started > 900:
                raise RuntimeError("Mantis 전체 동기화 시간 한도 초과")
            for issue in batch:
                issue_id = int(issue.get("id") or 0)
                if issue_id <= 0:
                    raise ValueError("유효하지 않은 Mantis 이슈 ID")
                payload = json.dumps(issue, ensure_ascii=False, sort_keys=True)
                duplicate |= issue_id in issue_ids
                issue_ids.add(issue_id)
                db.execute("INSERT OR REPLACE INTO issues VALUES (?, ?)", (issue_id, payload))
            if len(batch) < page_size:
                return issue_ids, duplicate
        raise RuntimeError("Mantis 전체 동기화 페이지 한도 초과")
    finally:
        pages.close()


def sync_complete_store(
    fetch_page: Callable[[int], list[dict[str, Any]]] | None = None,
    *, page_size: int = 100, max_pages: int = 1000, workers: int = 6,
) -> dict[str, Any]:
    """최대 3회 안에 연속 2회 같은 ID 집합이 확인된 경우 최종 수집본으로 교체한다.

    중복된 패스는 저장 후보로 쓰지 않는다. ID 집합을 비교해 삭제/이동으로
    생긴 누락도 다음 패스에서 탐지한다. API 트랜잭션 수준의 보장은 아니다.
    """
    from backend.sources.mantis import fetch_mantis_issues
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "running"}
        identity = connection_key()
        fetch = fetch_page or (lambda page: fetch_mantis_issues(
            {"page": page, "page_size": page_size}, timeout=12, attempts=2,
        ))
        fd, temporary = tempfile.mkstemp(prefix="mantis-complete-", suffix=".sqlite3", dir=path.parent)
        os.close(fd)
        meta: dict[str, Any] = {}
        try:
            with closing(sqlite3.connect(temporary)) as db, db:
                db.execute("CREATE TABLE issues (id INTEGER PRIMARY KEY, payload TEXT NOT NULL)")
                db.execute("CREATE TABLE metadata (value TEXT NOT NULL)")
                previous = None
                for attempt in range(3):
                    fetched_at = datetime.now(timezone.utc).isoformat()
                    # 한도는 수집 한 번마다 둔다. 전체에 한 번만 두면 Mantis가 느린 날
                    # 두 번째 확인 수집 도중 한도가 끝나 저장본이 계속 갱신되지 않는다.
                    started = time.monotonic()
                    try:
                        issue_ids, duplicate = _collect_pass(
                            db, fetch, page_size=page_size, max_pages=max_pages, workers=workers, started=started,
                        )
                    except (requests.RequestException, HTTPException) as exc:
                        if isinstance(exc, HTTPException) and exc.status_code not in {429, 502, 503, 504}:
                            raise
                        previous = None
                        if attempt == 2:
                            raise
                        continue
                    if not duplicate and issue_ids == previous:
                        meta = {"connection": identity, "fetchedAt": fetched_at, "fullSyncAt": fetched_at,
                                "count": len(issue_ids), "consistency": "two_matching_passes", "passes": attempt + 1}
                        break
                    previous = None if duplicate else issue_ids
                if not meta:
                    raise RuntimeError("Mantis 페이지가 계속 변경되어 전체 범위를 확정하지 못했습니다.")
                if identity != connection_key():
                    raise RuntimeError("수집 중 Mantis 연결 설정이 변경되었습니다.")
                db.execute("INSERT INTO metadata VALUES (?)", (json.dumps(meta),))
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"status": "complete", "count": meta["count"], "fetchedAt": meta["fetchedAt"], "passes": meta["passes"]}


# 평소에는 바뀐 이슈만 받아 저장본에 덮어쓰고, 삭제·이동 정리는 하루 한 번 전체 수집으로 한다.
# Mantis는 이슈를 최근 수정 시각 내림차순으로 페이지에 담아 주므로 앞 페이지만 읽으면 된다.
INCREMENTAL_OVERLAP_SECONDS = 600
INCREMENTAL_MAX_PAGES = 30
FULL_SYNC_INTERVAL_SECONDS = 86400
SCHEDULE_INTERVAL_SECONDS = 600


def _saved_metadata() -> dict[str, Any] | None:
    """갱신 시각과 관계없이 현재 연결 설정의 저장본 메타데이터를 읽는다."""
    path = store_path()
    if not path.exists():
        return None
    try:
        with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
            meta = json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])
        return meta if meta.get("connection") == connection_key() else None
    except (OSError, sqlite3.Error, ValueError, TypeError, HTTPException):
        return None


def _updated_at(issue: dict[str, Any]) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(issue.get("updated_at") or ""))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _changed_since(fetch, cutoff: datetime, *, page_size: int, max_pages: int) -> tuple[dict[int, dict], bool]:
    """cutoff 이후 수정된 이슈를 모은다. (이슈들, cutoff 이전 이슈까지 도달했는지)."""
    changed: dict[int, dict] = {}
    for page in range(1, max_pages + 1):
        batch = fetch(page)
        reached = not batch or len(batch) < page_size
        for issue in batch:
            updated = _updated_at(issue)
            if updated is None or updated >= cutoff:
                changed[int(issue["id"])] = issue
            else:
                reached = True
        if reached:
            return changed, True
    return changed, False


def refresh_changed_issues(
    fetch_page: Callable[[int], list[dict[str, Any]]] | None = None,
    *, page_size: int = 100, max_pages: int = INCREMENTAL_MAX_PAGES,
) -> dict[str, Any]:
    """지난 저장 이후 수정·등록된 이슈만 받아 저장본에 덮어쓴다.

    두 번 읽어 합친다. 읽는 도중 다른 이슈가 수정돼 맨 앞으로 오면 뒤 이슈들이 한 칸씩
    밀려 페이지 경계의 이슈를 한 번 놓칠 수 있기 때문이다. 바뀐 이슈가 너무 많거나
    저장본이 없으면 'needs_full'을 돌려주고 전체 수집에 맡긴다.
    """
    from backend.sources.mantis import fetch_mantis_issues
    path = store_path()
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "running"}
        saved = _saved_metadata()
        if saved is None:
            return {"status": "needs_full", "reason": "no_store"}
        fetch = fetch_page or (lambda page: fetch_mantis_issues(
            {"page": page, "page_size": page_size}, timeout=12, attempts=2,
        ))
        identity = connection_key()
        fetched_at = datetime.now(timezone.utc)
        cutoff = datetime.fromisoformat(saved["fetchedAt"]) - timedelta(seconds=INCREMENTAL_OVERLAP_SECONDS)
        first, reached_first = _changed_since(fetch, cutoff, page_size=page_size, max_pages=max_pages)
        second, reached_second = _changed_since(fetch, cutoff, page_size=page_size, max_pages=max_pages)
        if not (reached_first and reached_second):
            return {"status": "needs_full", "reason": "too_many_changes"}
        changed = {**first, **second}
        if identity != connection_key():
            raise RuntimeError("수집 중 Mantis 연결 설정이 변경되었습니다.")
        fd, temporary = tempfile.mkstemp(prefix="mantis-complete-", suffix=".sqlite3", dir=path.parent)
        os.close(fd)
        try:
            with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as source, \
                    closing(sqlite3.connect(temporary)) as db:
                source.backup(db)
                with db:
                    for issue_id, issue in changed.items():
                        payload = json.dumps(issue, ensure_ascii=False, sort_keys=True)
                        db.execute("INSERT OR REPLACE INTO issues VALUES (?, ?)", (issue_id, payload))
                    count = db.execute("SELECT COUNT(*) FROM issues").fetchone()[0]
                    meta = {**saved, "connection": identity, "fetchedAt": fetched_at.isoformat(), "count": count,
                            "fullSyncAt": saved.get("fullSyncAt") or saved["fetchedAt"],
                            "consistency": "incremental", "changed": len(changed)}
                    db.execute("DELETE FROM metadata")
                    db.execute("INSERT INTO metadata VALUES (?)", (json.dumps(meta),))
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    return {"status": "complete", "mode": "incremental", "count": count, "changed": len(changed),
            "fetchedAt": meta["fetchedAt"]}


def refresh_store(fetch_page: Callable[[int], list[dict[str, Any]]] | None = None) -> dict[str, Any]:
    """평소에는 바뀐 이슈만, 하루가 지났거나 증분으로 확정할 수 없으면 전체를 수집한다."""
    saved = _saved_metadata()
    if saved:
        full_at = datetime.fromisoformat(saved.get("fullSyncAt") or saved["fetchedAt"])
        if (datetime.now(timezone.utc) - full_at).total_seconds() < FULL_SYNC_INTERVAL_SECONDS:
            result = refresh_changed_issues(fetch_page)
            if result.get("status") != "needs_full":
                return result
    return sync_complete_store(fetch_page)


def refresh_in_progress() -> bool:
    try:
        with store_path().with_suffix(".lock").open("r") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def start_store_refresh() -> Thread:
    """동일 프로세스의 여러 검색 요청은 하나의 갱신 작업을 공유한다."""
    global _refresh_thread
    def refresh() -> None:
        try:
            refresh_store()
        except Exception as exc:
            # API 응답/인증값을 로그에 남기지 않는다.
            LOGGER.warning("Mantis 전체 동기화 실패: %s", type(exc).__name__)
    with _refresh_lock:
        if _refresh_thread is None or not _refresh_thread.is_alive():
            _refresh_thread = Thread(target=refresh, name="mantis-store-refresh", daemon=True)
            _refresh_thread.start()
        return _refresh_thread


def start_store_scheduler() -> Event:
    stop = Event()
    def run() -> None:
        while not stop.is_set():
            try:
                refresh_store()
            except Exception as exc:
                LOGGER.warning("Mantis 전체 동기화 실패: %s", type(exc).__name__)
            stop.wait(SCHEDULE_INTERVAL_SECONDS)
    Thread(target=run, name="mantis-store-scheduler", daemon=True).start()
    return stop


def render_document(raw: dict[str, Any]) -> str:
    """기존 구조화 검색과 동일한 필드 이름을 사용하고 본문을 자르지 않는다."""
    from backend.sources.mantis import normalize_issue
    issue = normalize_issue(raw)
    fields = {
        "이슈 ID": f"#{issue['id']}", "프로젝트(고객사)": issue['project'],
        "매체구분": issue['media_type'], "화면번호": issue['screen_number'],
        "작업주체": issue['work_subject'], "현재 상태": issue['status'],
        "이슈 유형": issue['severity'], "우선순위": issue['priority'],
        "담당자": issue['handler'], "요청자": issue['reporter'],
        "등록일": issue['created_at'], "최종수정일": issue['updated_at'],
    }
    lines = ["[Mantis 이슈 정보]", *(f"- {name}: {value}" for name, value in fields.items())]
    for label, values in [("일정/전달일", issue['date_fields']), ("공수", issue['effort_fields'])]:
        lines.extend(["", f"[{label}]", *(f"- {name}: {value}" for name, value in values.items())])
    lines.extend(["", "[제목]", issue['summary'], "", "[고객사 본문 요청 내용]", raw.get('description') or "",
                  "", "[추가 정보]", raw.get('additional_information') or "",
                  "", "[재현 과정 및 단계]", raw.get('steps_to_reproduce') or "", "", "[내부 개발 진행 및 대화 이력]"])
    lines.extend(str(note.get('text') or '') for note in raw.get('notes') or [])
    return "\n".join(lines)
