"""연결별 Notion 본문 후보 색인. 페이지 단위 갱신은 SQLite 트랜잭션으로 게시한다.

검색 결과는 현재 권한과 수정 시각을 확인할 후보이며 원본의 최신성을 보증하지 않는다.
실패하거나 일부만 읽은 본문은 마지막 완전본을 보존하고 trusted=False로 반환한다.
"""

from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from backend.common import normalize_for_match
from backend.config import NOTION_API_BASE, RUNTIME_DIR, get_notion_settings


SCHEMA_VERSION = "1"
# 완료 판정 규칙이 달라지면 이전 본문을 보존하면서 다시 검증한다.
CONTENT_POLICY_VERSION = 1


class BodyIndexError(RuntimeError):
    """사용할 수 없는 색인. 호출자는 실시간 검색으로 대체할 수 있다."""


def _id(value: Any) -> str:
    return str(value or "").replace("-", "").lower()


def segment_belongs_to_page(segment: dict[str, Any], page_id: str) -> bool:
    """직접 소유 근거 또는 원본 검증을 마친 동기화 복제 근거만 허용한다.

    저장본의 표시는 후보 발견에만 쓰며, 결과에는 매번 API 재검증한 근거를 쓴다.
    """
    target = _id(page_id)
    if _id(segment.get("owner_page_id") or segment.get("page_id")) == target:
        return True
    return bool(target and _id(segment.get("display_page_id")) == target
                and segment.get("owner_page_id") and segment.get("synced_source_id")
                and segment.get("synced_copy_id") and segment.get("source_database_id"))


def _ancestor_page_path(raw: dict[str, Any], database_id: str,
                         parent_pages: dict[str, dict[str, Any]] | None = None) -> list[str] | None:
    parents = parent_pages or {}
    current = raw
    seen: set[str] = set()
    page_ids: list[str] = []
    for _ in range(65):
        identifier = _id(current.get("id"))
        if not identifier or identifier in seen or current.get("archived") or current.get("in_trash"):
            return None
        seen.add(identifier)
        parent = current.get("parent") or {}
        if parent.get("type") == "database_id":
            return page_ids if _id(parent.get("database_id")) == _id(database_id) else None
        kind = parent.get("type")
        if kind not in {"page_id", "block_id"}:
            return None
        ancestor_id = _id(parent.get(kind))
        current = parents.get(ancestor_id, {})
        if _id(current.get("id")) != ancestor_id or current.get("object") != kind.removesuffix("_id"):
            return None
        if kind == "page_id":
            page_ids.append(ancestor_id)
    return None


def page_belongs_to_database(raw: dict[str, Any], database_id: str,
                             parent_pages: dict[str, dict[str, Any]] | None = None) -> bool:
    """페이지·블록의 실제 부모 경로가 지정 DB까지 이어질 때만 인정한다."""
    return _ancestor_page_path(raw, database_id, parent_pages) is not None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ttl_seconds() -> int:
    try:
        return max(0, min(int(os.getenv("NOTION_BODY_INDEX_TTL_SECONDS", "86400")), 31536000))
    except ValueError:
        return 86400


def _fresh(timestamp: str, ttl: int) -> bool:
    try:
        stamp = datetime.fromisoformat(timestamp)
        if stamp.tzinfo is None:
            return False
        return 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() < ttl
    except (TypeError, ValueError):
        return False


def body_index_path() -> Path:
    return RUNTIME_DIR / "notion_body_index.sqlite3"


def connection_fingerprint() -> str:
    settings = get_notion_settings()
    identity = [NOTION_API_BASE, settings.get("token", ""), settings.get("version", ""),
                sorted({_id(value) for value in settings.get("database_ids", []) if value}),
                os.getenv("NOTION_INCLUDE_CHILD_DATABASES", "false").lower() in {"1", "true", "yes"}]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


class BodyIndex:
    def __init__(self, path: Path | str | None = None, fingerprint: str | None = None, *, create: bool = True):
        self.path = Path(path) if path is not None else body_index_path()
        self._uses_config_fingerprint = fingerprint is None
        self.fingerprint = fingerprint if fingerprint is not None else connection_fingerprint()
        if not self.fingerprint:
            raise BodyIndexError("Notion 본문 색인 연결 식별자가 없습니다.")
        if create:
            self._initialize()
        else:
            with self._database() as db:
                self._check_schema(db)

    def _initialize(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
                self.path.chmod(0o600)
                db.execute("PRAGMA journal_mode=WAL")
                db.execute("PRAGMA synchronous=FULL")
                db.executescript("""
                    CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS catalogs (
                        fingerprint TEXT NOT NULL, database_id TEXT NOT NULL,
                        complete INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
                        PRIMARY KEY (fingerprint, database_id)
                    );
                    CREATE TABLE IF NOT EXISTS pages (
                        fingerprint TEXT NOT NULL, page_id TEXT NOT NULL, database_id TEXT NOT NULL,
                        raw_page TEXT NOT NULL, last_edited_time TEXT NOT NULL,
                        indexed_edited_time TEXT NOT NULL DEFAULT '',
                        content_complete INTEGER NOT NULL DEFAULT 0, needs_refresh INTEGER NOT NULL DEFAULT 1,
                        failure_count INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
                        updated_at TEXT NOT NULL, indexed_at TEXT NOT NULL DEFAULT '',
                        PRIMARY KEY (fingerprint, page_id)
                    );
                    CREATE INDEX IF NOT EXISTS body_pages_database ON pages(fingerprint, database_id);
                    CREATE TABLE IF NOT EXISTS blocks (
                        fingerprint TEXT NOT NULL, page_id TEXT NOT NULL, block_id TEXT NOT NULL,
                        owner_page_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                        type TEXT NOT NULL, text TEXT NOT NULL, normalized_text TEXT NOT NULL,
                        PRIMARY KEY (fingerprint, page_id, block_id),
                        FOREIGN KEY (fingerprint, page_id) REFERENCES pages(fingerprint, page_id) ON DELETE CASCADE
                    );
                """)
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', ?)", (SCHEMA_VERSION,))
                self._check_schema(db)
                self._ensure_partial_schema(db)
        except (OSError, sqlite3.Error) as exc:
            raise BodyIndexError("Notion 본문 색인을 열 수 없습니다.") from exc

    @staticmethod
    def _check_schema(db: sqlite3.Connection) -> None:
        row = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
        if row is None or row[0] != SCHEMA_VERSION:
            raise BodyIndexError("지원하지 않는 Notion 본문 색인 형식입니다.")

    @staticmethod
    def _ensure_partial_schema(db: sqlite3.Connection) -> None:
        # v1 파일의 선택적 확장이다. 읽기 전용 검색은 기존 파일을 변경하지 않는다.
        db.execute("""CREATE TABLE IF NOT EXISTS partial_blocks (
            fingerprint TEXT NOT NULL, page_id TEXT NOT NULL, block_id TEXT NOT NULL,
            owner_page_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            type TEXT NOT NULL, text TEXT NOT NULL, normalized_text TEXT NOT NULL,
            indexed_edited_time TEXT NOT NULL, indexed_at TEXT NOT NULL,
            PRIMARY KEY (fingerprint, page_id, block_id),
            FOREIGN KEY (fingerprint, page_id) REFERENCES pages(fingerprint, page_id) ON DELETE CASCADE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS content_policies (
            fingerprint TEXT NOT NULL, page_id TEXT NOT NULL, version INTEGER NOT NULL,
            PRIMARY KEY (fingerprint, page_id),
            FOREIGN KEY (fingerprint, page_id) REFERENCES pages(fingerprint, page_id) ON DELETE CASCADE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS collection_coverage (
            fingerprint TEXT NOT NULL, page_id TEXT NOT NULL,
            revision TEXT NOT NULL, observed_at TEXT NOT NULL, policy INTEGER NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY (fingerprint, page_id),
            FOREIGN KEY (fingerprint, page_id) REFERENCES pages(fingerprint, page_id) ON DELETE CASCADE
        )""")
        db.execute("""CREATE TABLE IF NOT EXISTS synced_segments (
            fingerprint TEXT NOT NULL, page_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
            normalized_text TEXT NOT NULL, data TEXT NOT NULL,
            PRIMARY KEY (fingerprint, page_id, ordinal),
            FOREIGN KEY (fingerprint, page_id) REFERENCES pages(fingerprint, page_id) ON DELETE CASCADE
        )""")

    @staticmethod
    def _current_policy_predicate(db: sqlite3.Connection) -> str:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='content_policies'").fetchone()
        if not exists:
            return "0"
        return ("EXISTS (SELECT 1 FROM content_policies policy WHERE policy.fingerprint=pages.fingerprint "
                f"AND policy.page_id=pages.page_id AND policy.version={int(CONTENT_POLICY_VERSION)})")

    @staticmethod
    def _has_partial_table(db: sqlite3.Connection) -> bool:
        return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='partial_blocks'").fetchone() is not None

    @contextmanager
    def _database(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        db = None
        try:
            if self._uses_config_fingerprint and connection_fingerprint() != self.fingerprint:
                raise BodyIndexError("수집 또는 검색 중 Notion 연결 설정이 변경되었습니다.")
            uri = self.path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
            db = sqlite3.connect(uri, uri=True, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                self._ensure_partial_schema(db)
            yield db
            if write:
                db.commit()
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as exc:
            raise BodyIndexError("Notion 본문 색인 읽기 또는 갱신에 실패했습니다.") from exc
        finally:
            if db is not None:
                db.close()

    def _prepare_pages(self, database_id: str, pages: list[dict[str, Any]],
                       parent_pages: dict[str, dict[str, Any]] | None = None) -> list[tuple[str, dict[str, Any]]]:
        prepared = []
        for raw in pages:
            identifier = _id(raw.get("id"))
            # 삭제 행은 기존 직접 부모 검사로 제거할 수 있게 유지한다.
            candidate = {**raw, "archived": False, "in_trash": False}
            ancestor_path = _ancestor_page_path(candidate, database_id, parent_pages)
            if not identifier or ancestor_path is None:
                raise BodyIndexError("본문 색인 페이지가 지정한 DB에 속하지 않습니다.")
            raw = {**raw, "_body_index_ancestor_page_ids": ancestor_path}
            prepared.append((identifier, raw))
        return prepared

    def _upsert_pages(self, db: sqlite3.Connection, database_id: str,
                      prepared: list[tuple[str, dict[str, Any]]], stamp: str) -> set[str]:
        seen = set()
        for identifier, raw in prepared:
            if raw.get("archived") or raw.get("in_trash"):
                db.execute("DELETE FROM pages WHERE fingerprint=? AND page_id=?", (self.fingerprint, identifier))
                continue
            seen.add(identifier)
            revision = str(raw.get("last_edited_time") or "")
            db.execute("""
                INSERT INTO pages (fingerprint, page_id, database_id, raw_page, last_edited_time, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (fingerprint, page_id) DO UPDATE SET
                    needs_refresh=CASE WHEN pages.last_edited_time != excluded.last_edited_time
                        OR pages.database_id != excluded.database_id THEN 1 ELSE pages.needs_refresh END,
                    database_id=excluded.database_id, raw_page=excluded.raw_page,
                    last_edited_time=excluded.last_edited_time, updated_at=excluded.updated_at
            """, (self.fingerprint, identifier, database_id, json.dumps(raw, ensure_ascii=False), revision, stamp))
        return seen

    def upsert_catalog(self, database_id: str, pages: list[dict[str, Any]], *, complete: bool,
                       parent_pages: dict[str, dict[str, Any]] | None = None) -> None:
        """완료된 DB 목록만 누락 페이지를 제거한다. 불완전 목록은 기존 행을 보존한다."""
        database_id = _id(database_id)
        prepared = self._prepare_pages(database_id, pages, parent_pages)
        stamp = _now()
        with self._database(write=True) as db:
            seen = self._upsert_pages(db, database_id, prepared, stamp)
            if complete:
                stored = db.execute("SELECT page_id, raw_page FROM pages WHERE fingerprint=? AND database_id=?",
                                    (self.fingerprint, database_id)).fetchall()
                # 일반 DB query는 본문에서 발견한 독립 하위 페이지를 반환하지
                # 않는다. 여전히 목록에 있는 부모의 자손은 원문 재검증까지 보존한다.
                descendants = {}
                for row in stored:
                    saved = json.loads(row[1])
                    parent = saved.get("parent") or {}
                    if parent.get("type") == "page_id":
                        descendants[row[0]] = _id(parent.get("page_id"))
                    elif parent.get("type") == "block_id" and saved.get("_body_index_ancestor_page_ids"):
                        descendants[row[0]] = saved["_body_index_ancestor_page_ids"][0]
                while True:
                    added = {identifier for identifier, parent in descendants.items()
                             if identifier not in seen and parent in seen}
                    if not added:
                        break
                    seen.update(added)
                db.executemany("DELETE FROM pages WHERE fingerprint=? AND page_id=?",
                               ((self.fingerprint, row[0]) for row in stored if row[0] not in seen))
            db.execute("""INSERT INTO catalogs VALUES (?, ?, ?, ?)
                          ON CONFLICT(fingerprint, database_id) DO UPDATE SET
                          complete=excluded.complete, updated_at=excluded.updated_at""",
                       (self.fingerprint, database_id, int(complete), stamp))

    def upsert_page(self, database_id: str, raw_page: dict[str, Any], *,
                    parent_pages: dict[str, dict[str, Any]] | None = None) -> None:
        """한 페이지를 재검증하며 이미 완료된 DB 카탈로그 상태는 유지한다."""
        database_id = _id(database_id)
        prepared = self._prepare_pages(database_id, [raw_page], parent_pages)
        stamp = _now()
        with self._database(write=True) as db:
            self._upsert_pages(db, database_id, prepared, stamp)
            db.execute("INSERT OR IGNORE INTO catalogs VALUES (?, ?, 0, ?)",
                       (self.fingerprint, database_id, stamp))

    def replace_content(self, page_id: str, segments: list[dict[str, Any]], *, complete: bool,
                        expected_edited_time: str, coverage: dict[str, Any] | None = None) -> bool:
        """수집 시작/종료 사이 페이지 버전이 바뀌면 오래된 본문을 게시하지 않는다."""
        page_id = _id(page_id)
        own = {}
        for segment in segments:
            owner = _id(segment.get("owner_page_id") or segment.get("page_id"))
            identifier = _id(segment.get("block_id"))
            if owner == page_id and identifier:
                own[identifier] = (str(segment.get("type") or ""), str(segment.get("text") or ""))
        with self._database(write=True) as db:
            saved = db.execute("SELECT * FROM pages WHERE fingerprint=? AND page_id=?",
                               (self.fingerprint, page_id)).fetchone()
            if saved is None or saved["last_edited_time"] != expected_edited_time:
                return False
            db.execute("INSERT OR REPLACE INTO content_policies VALUES (?, ?, ?)",
                       (self.fingerprint, page_id, CONTENT_POLICY_VERSION))
            stamp = _now()
            # 선택적 진단 확장: 기존 순회 프로세스와 공존하고 정책 전수 무효화를 하지 않는다.
            projected = [item for item in segments if _id(item.get("owner_page_id")) != page_id
                         and segment_belongs_to_page(item, page_id)]
            if complete or segments:
                db.execute("DELETE FROM synced_segments WHERE fingerprint=? AND page_id=?",
                           (self.fingerprint, page_id))
                db.executemany("INSERT INTO synced_segments VALUES (?, ?, ?, ?, ?)",
                               ((self.fingerprint, page_id, ordinal, normalize_for_match(str(item.get("text", ""))),
                                 json.dumps(item, ensure_ascii=False)) for ordinal, item in enumerate(projected)))
            db.execute("DELETE FROM collection_coverage WHERE fingerprint=? AND page_id=?", (self.fingerprint, page_id))
            if coverage is not None:
                counts = {"body": 0, "table": 0, "code": 0}
                for item in segments:
                    if segment_belongs_to_page(item, page_id) and item.get("text"):
                        category = {"table_row": "table", "code": "code"}.get(str(item.get("type")), "body")
                        counts[category] += 1
                data = {**coverage, "supported_segment_counts": counts}
                db.execute("INSERT INTO collection_coverage VALUES (?, ?, ?, ?, ?, ?)",
                           (self.fingerprint, page_id, expected_edited_time, stamp, CONTENT_POLICY_VERSION,
                            json.dumps(data, ensure_ascii=False)))
            # 마지막 완전본과 새 부분 근거를 별도로 보존해야 수정/추가된
            # 코드·표를 다음 검색에서도 발견할 수 있다. 빈 실패는 둘 다 보존한다.
            if not complete and (saved["content_complete"] or not own):
                if own:
                    db.execute("DELETE FROM partial_blocks WHERE fingerprint=? AND page_id=?",
                               (self.fingerprint, page_id))
                    db.executemany("INSERT INTO partial_blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                   ((self.fingerprint, page_id, identifier, page_id, ordinal, kind, text,
                                     normalize_for_match(text), expected_edited_time, stamp)
                                    for ordinal, (identifier, (kind, text)) in enumerate(own.items())))
                db.execute("UPDATE pages SET needs_refresh=1, last_error='incomplete_body', updated_at=? "
                           "WHERE fingerprint=? AND page_id=?", (stamp, self.fingerprint, page_id))
                return True
            db.execute("DELETE FROM blocks WHERE fingerprint=? AND page_id=?", (self.fingerprint, page_id))
            db.execute("DELETE FROM partial_blocks WHERE fingerprint=? AND page_id=?", (self.fingerprint, page_id))
            db.executemany("INSERT INTO blocks VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                           ((self.fingerprint, page_id, identifier, page_id, ordinal, kind, text,
                             normalize_for_match(text))
                            for ordinal, (identifier, (kind, text)) in enumerate(own.items())))
            db.execute("""UPDATE pages SET indexed_edited_time=?, content_complete=?, needs_refresh=?,
                       indexed_at=?, updated_at=?, last_error=?, failure_count=0
                       WHERE fingerprint=? AND page_id=?""",
                       (expected_edited_time, int(complete), int(not complete), stamp, stamp,
                        "" if complete else "incomplete_body", self.fingerprint, page_id))
            return True

    def record_failure(self, page_id: str, reason: str = "fetch_failed") -> None:
        with self._database(write=True) as db:
            observed = db.execute("SELECT data FROM collection_coverage WHERE fingerprint=? AND page_id=?",
                                  (self.fingerprint, _id(page_id))).fetchone()
            if observed and ",".join(json.loads(observed[0]).get("incomplete_reasons", [])) != reason:
                db.execute("DELETE FROM collection_coverage WHERE fingerprint=? AND page_id=?",
                           (self.fingerprint, _id(page_id)))
            db.execute("UPDATE pages SET needs_refresh=1, failure_count=failure_count+1, last_error=?, updated_at=? "
                       "WHERE fingerprint=? AND page_id=?", (reason[:120], _now(), self.fingerprint, _id(page_id)))

    def mark_pending(self, database_ids: list[str]) -> int:
        """운영자가 재수집을 요청하면 마지막 본문은 보존하고 실패로 집계하지 않는다."""
        placeholders, params = self._scope(database_ids)
        if not placeholders:
            return 0
        with self._database(write=True) as db:
            cursor = db.execute(f"""UPDATE pages SET needs_refresh=1, last_error='', updated_at=?
                                  WHERE fingerprint=? AND database_id IN ({placeholders})""", [_now(), *params])
            return cursor.rowcount

    def _record(self, db: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        raw = json.loads(row["raw_page"])
        if not isinstance(raw, dict) or _id(raw.get("id")) != row["page_id"]:
            raise BodyIndexError("Notion 본문 색인 페이지 정보가 올바르지 않습니다.")
        segments = [{"block_id": block["block_id"], "page_id": block["page_id"],
                     "owner_page_id": block["owner_page_id"], "type": block["type"], "text": block["text"],
                     "snapshot": "complete" if row["content_complete"] else "partial",
                     "indexed_edited_time": row["indexed_edited_time"], "indexed_at": row["indexed_at"]}
                    for block in db.execute("SELECT * FROM blocks WHERE fingerprint=? AND page_id=? ORDER BY ordinal",
                                            (self.fingerprint, row["page_id"]))]
        partial_segments = [{"block_id": block["block_id"], "page_id": block["page_id"],
                             "owner_page_id": block["owner_page_id"], "type": block["type"], "text": block["text"],
                             "snapshot": "partial", "indexed_edited_time": block["indexed_edited_time"],
                             "indexed_at": block["indexed_at"]}
                            for block in db.execute("SELECT * FROM partial_blocks WHERE fingerprint=? AND page_id=? ORDER BY ordinal",
                                                    (self.fingerprint, row["page_id"]))] if self._has_partial_table(db) else []
        projected = []
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='synced_segments'").fetchone():
            projected = [json.loads(item[0]) for item in db.execute(
                "SELECT data FROM synced_segments WHERE fingerprint=? AND page_id=? ORDER BY ordinal",
                (self.fingerprint, row["page_id"]))]
        segments.extend(projected)
        policy_current = bool(db.execute(
            f"SELECT {self._current_policy_predicate(db)} FROM pages WHERE fingerprint=? AND page_id=?",
            (self.fingerprint, row["page_id"])).fetchone()[0])
        trusted = bool(policy_current and row["last_edited_time"] and row["content_complete"] and not row["needs_refresh"]
                       and row["indexed_edited_time"] == row["last_edited_time"]
                       and _fresh(row["indexed_at"], _ttl_seconds()))
        return {**{key: row[key] for key in row.keys() if key not in {"fingerprint", "raw_page"}},
                "raw_page": raw, "segments": segments, "content": "\n".join(block["text"] for block in segments),
                "candidate_segments": [*segments, *partial_segments],
                "needs_refresh": bool(row["needs_refresh"] or not policy_current),
                "content_complete": bool(row["content_complete"] and policy_current),
                "content_policy_current": policy_current,
                "trusted": trusted}

    def read_page(self, page_id: str) -> dict[str, Any] | None:
        with self._database() as db:
            row = db.execute("SELECT * FROM pages WHERE fingerprint=? AND page_id=?",
                             (self.fingerprint, _id(page_id))).fetchone()
            return self._record(db, row) if row is not None else None

    def _scope(self, database_ids: list[str]) -> tuple[str, list[str]]:
        identifiers = sorted({_id(value) for value in database_ids if value})
        return ",".join("?" for _ in identifiers), [self.fingerprint, *identifiers]

    def pending_pages(self, database_ids: list[str], max_pages: int = 100) -> list[dict[str, Any]]:
        placeholders, params = self._scope(database_ids)
        if not placeholders:
            return []
        now = datetime.now(timezone.utc)
        cutoff = (now - timedelta(seconds=_ttl_seconds())).isoformat()
        with self._database() as db:
            rows = db.execute(f"""SELECT * FROM pages WHERE fingerprint=? AND database_id IN ({placeholders})
                              AND (needs_refresh=1 OR content_complete=0 OR indexed_edited_time!=last_edited_time
                                   OR last_edited_time='' OR indexed_at<=? OR indexed_at>?
                                   OR NOT ({self._current_policy_predicate(db)}))
                              ORDER BY failure_count, updated_at, page_id LIMIT ?""",
                              [*params, cutoff, now.isoformat(), max(0, max_pages)])
            return [self._record(db, row) for row in rows]

    def search(self, database_ids: list[str], terms: list[str]) -> list[dict[str, Any]]:
        """소유한 개별 블록에서 OR 리터럴 후보를 반환한다. 최종 조건과 limit는 호출자 책임이다."""
        placeholders, params = self._scope(database_ids)
        if not placeholders:
            return []
        normalized = list(dict.fromkeys(normalize_for_match(str(term)) for term in terms if normalize_for_match(str(term))))
        with self._database() as db:
            tables = ["blocks", "partial_blocks"] if self._has_partial_table(db) else ["blocks"]
            projected = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='synced_segments'").fetchone())
            condition = ""
            if normalized:
                predicates = " OR ".join("instr(b.normalized_text, ?) > 0" for _ in normalized)
                conditions = [f"""EXISTS (SELECT 1 FROM {table} b WHERE b.fingerprint=p.fingerprint
                               AND b.page_id=p.page_id AND b.owner_page_id=p.page_id AND ({predicates}))"""
                              for table in tables]
                if projected:
                    conditions.append(f"""EXISTS (SELECT 1 FROM synced_segments b WHERE b.fingerprint=p.fingerprint
                                       AND b.page_id=p.page_id AND ({predicates}))""")
                condition = "AND (" + " OR ".join(conditions) + ")"
            rows = db.execute(f"""SELECT p.* FROM pages p WHERE p.fingerprint=?
                               AND p.database_id IN ({placeholders}) {condition}
                               ORDER BY p.last_edited_time DESC, p.page_id""", [*params, *(normalized * (len(tables) + int(projected)))])
            records = [self._record(db, row) for row in rows]
            # 저장본 자체의 content/segments는 read_page에서 유지한다. 후보 선택은
            # 실제 블록 ID와 수집 세대를 가진 새 부분 근거도 함께 고려한다.
            return [{**record, "segments": record["candidate_segments"]} for record in records]

    def status(self, database_ids: list[str]) -> dict[str, Any]:
        placeholders, params = self._scope(database_ids)
        database_count = len(params) - 1
        with self._database() as db:
            catalogs = db.execute(f"SELECT * FROM catalogs WHERE fingerprint=? AND database_id IN ({placeholders})",
                                  params).fetchall() if placeholders else []
            rows = db.execute(f"SELECT *, {self._current_policy_predicate(db)} AS policy_current FROM pages "
                              f"WHERE fingerprint=? AND database_id IN ({placeholders})",
                              params).fetchall() if placeholders else []
            diagnostics = {}
            if placeholders and db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='collection_coverage'").fetchone():
                diagnostics = {item["page_id"]: dict(item) for item in db.execute(
                    f"SELECT c.* FROM collection_coverage c JOIN pages p USING (fingerprint,page_id) "
                    f"WHERE c.fingerprint=? AND p.database_id IN ({placeholders})", params)}
        ttl = _ttl_seconds()
        complete_rows = [row for row in rows if row["policy_current"] and row["last_edited_time"] and row["content_complete"]
                         and not row["needs_refresh"] and row["indexed_edited_time"] == row["last_edited_time"]]
        indexed = sum(_fresh(row["indexed_at"], ttl) for row in complete_rows)
        completed_databases = sum(bool(row["complete"]) for row in catalogs)
        catalog_complete = bool(database_count and completed_databases == database_count)
        catalog_fresh = bool(catalogs) and all(_fresh(row["updated_at"], ttl) for row in catalogs)
        content_complete = indexed == len(rows)
        body_times = [row["indexed_at"] for row in rows if row["indexed_at"]]
        update_times = [row["updated_at"] for row in [*catalogs, *rows]]
        supported_ids = {row["page_id"] for row in complete_rows if _fresh(row["indexed_at"], ttl)}
        unsupported_pages = {row["page_id"] for row in rows if "unsupported_block" in row["last_error"].split(",")}
        counts: dict[str, int] = {}
        segment_counts = {"body": 0, "table": 0, "code": 0}
        diagnosed = set()
        for row in rows:
            diagnostic = diagnostics.get(row["page_id"])
            if (not diagnostic or diagnostic["revision"] != row["last_edited_time"]
                    or diagnostic["policy"] != CONTENT_POLICY_VERSION or not row["policy_current"]
                    or not _fresh(diagnostic["observed_at"], ttl)
                    or diagnostic["observed_at"] < row["updated_at"] and not row["last_error"]):
                continue
            data = json.loads(diagnostic["data"])
            expected_error = ",".join(data.get("incomplete_reasons", []))
            if row["last_error"] not in {"", "incomplete_body", expected_error}:
                continue
            if row["last_error"] == "incomplete_body" and row["updated_at"] > diagnostic["observed_at"]:
                continue
            diagnosed.add(row["page_id"])
            if data.get("supported_content_complete"):
                supported_ids.add(row["page_id"])
            if data.get("unsupported_elements"):
                unsupported_pages.add(row["page_id"])
            for item in data.get("unsupported_elements", []):
                kind = str(item.get("block_type") or "unknown")
                counts[kind] = counts.get(kind, 0) + 1
            for kind in segment_counts:
                segment_counts[kind] += data.get("supported_segment_counts", {}).get(kind, 0)
        return {"available": bool(catalogs), "database_count": database_count,
                "supported_collected_page_count": len(supported_ids),
                "supported_pending_or_unknown_page_count": len(rows) - len(supported_ids),
                "unsupported_element_page_count": len(unsupported_pages),
                "unsupported_element_counts": counts,
                "unsupported_subtype_unknown_page_count": len(unsupported_pages - diagnosed),
                "detailed_coverage_page_count": len(diagnosed),
                "supported_segment_counts": segment_counts,
                "supported_segment_counts_scope": "fresh_detailed_coverage_pages_only",
                "api_limitation_only_page_count": sum(row["last_error"] == "unsupported_block" for row in rows),
                "collection_error_page_count": sum(bool(row["failure_count"]) and row["last_error"] != "unsupported_block"
                                                   for row in rows),
                "transient_error_page_count": sum(any(reason in {"http_429", "request_failed", "time_limit", "request_limit"}
                                                      or reason.startswith("http_5")
                                                      for reason in row["last_error"].split(",")) for row in rows),
                "completed_database_count": completed_databases, "page_count": len(rows),
                "indexed_page_count": indexed, "pending_page_count": len(rows) - indexed,
                "content_policy_version": CONTENT_POLICY_VERSION,
                "policy_pending_page_count": sum(not row["policy_current"] for row in rows),
                "failed_page_count": sum(bool(row["failure_count"]) for row in rows),
                "catalog_complete": catalog_complete, "catalog_fresh": catalog_fresh,
                "content_complete": content_complete, "stale_page_count": len(complete_rows) - indexed,
                "snapshot_complete": catalog_complete and len(complete_rows) == len(rows), "ttl_seconds": ttl,
                "complete": catalog_complete and catalog_fresh and content_complete,
                "catalog_updated_at": min((row["updated_at"] for row in catalogs), default=""),
                "updated_at": max(update_times, default=""),
                "oldest_content_at": min(body_times, default=""), "latest_content_at": max(body_times, default="")}


def open_body_index(path: Path | str | None = None, fingerprint: str | None = None) -> BodyIndex | None:
    """요청 중 새 파일을 만들지 않는다. 다른 연결·손상된 색인은 사용하지 않는다."""
    target = Path(path) if path is not None else body_index_path()
    if not target.is_file():
        return None
    try:
        index = BodyIndex(target, fingerprint, create=False)
        with index._database() as db:
            exists = db.execute("SELECT 1 FROM catalogs WHERE fingerprint=? LIMIT 1", (index.fingerprint,)).fetchone()
        return index if exists is not None else None
    except (BodyIndexError, OSError):
        return None
