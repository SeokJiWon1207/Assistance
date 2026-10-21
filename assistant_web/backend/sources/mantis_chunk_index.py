"""검색용 FTS5 구간 인덱스. 원본 읽기 트랜잭션과 원자 교체로 세대를 고정한다."""
from contextlib import closing, contextmanager
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import tempfile
from threading import Lock, Thread
from typing import Generator, Any

from backend.execution import checkpoint
from backend.sources.mantis_chunks import embedding_text, search_tokens, split_issue

VERSION = 2
LOGGER = logging.getLogger(__name__)
_build_lock = Lock()


def _connect(path: Path):
    db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("BEGIN")
    return db


def _metadata(db) -> dict[str, Any]:
    return json.loads(db.execute("SELECT value FROM metadata").fetchone()[0])


def build_index(source: Path, target: Path, identity: str) -> dict[str, Any]:
    """키워드 인덱스만 구축한다. 네트워크·원본 DB 쓰기·일괄 임베딩은 없다."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "running"}
        fd, temporary = tempfile.mkstemp(prefix="mantis-chunks-", suffix=".sqlite3", dir=target.parent)
        os.close(fd)
        try:
            with closing(_connect(source)) as raw_db, closing(sqlite3.connect(temporary)) as db, db:
                source_meta = meta = _metadata(raw_db)
                if meta.get("connection") != identity:
                    raise ValueError("Mantis 연결 식별자가 다릅니다.")
                incremental = False
                if target.is_file():
                    try:
                        with closing(_connect(target)) as previous:
                            previous_meta = _metadata(previous)
                            if previous_meta.get("version") == VERSION and previous_meta.get("connection") == identity:
                                previous.backup(db)
                                incremental = True
                    except (sqlite3.Error, ValueError, TypeError, KeyError):
                        pass
                if not incremental:
                    db.executescript("""
                    CREATE TABLE metadata(value TEXT NOT NULL);
                    CREATE TABLE issue_hashes(issue_id INTEGER PRIMARY KEY, digest TEXT NOT NULL);
                    CREATE TABLE chunks(id INTEGER PRIMARY KEY, issue_id INTEGER NOT NULL,
                        title TEXT NOT NULL, field TEXT NOT NULL, start INTEGER, end INTEGER, text TEXT NOT NULL);
                    CREATE INDEX chunks_issue ON chunks(issue_id);
                    CREATE VIRTUAL TABLE chunks_fts USING fts5(tokens);
                """)
                existing = dict(db.execute("SELECT issue_id, digest FROM issue_hashes"))
                issue_count, reindexed = 0, 0

                def remove_issue(issue_id):
                    db.execute("DELETE FROM chunks_fts WHERE rowid IN (SELECT id FROM chunks WHERE issue_id=?)", [issue_id])
                    db.execute("DELETE FROM chunks WHERE issue_id=?", [issue_id])
                    db.execute("DELETE FROM issue_hashes WHERE issue_id=?", [issue_id])

                for row in raw_db.execute("SELECT id, payload FROM issues ORDER BY id"):
                    checkpoint()
                    issue_id, payload = row
                    issue_count += 1
                    digest = hashlib.sha256(payload.encode()).hexdigest()
                    if existing.pop(issue_id, None) == digest:
                        continue
                    issue = json.loads(payload)
                    reindexed += 1
                    remove_issue(issue_id)
                    for chunk in split_issue(issue):
                        checkpoint()
                        cursor = db.execute("INSERT INTO chunks(issue_id, title, field, start, end, text) VALUES (?, ?, ?, ?, ?, ?)",
                                            [chunk[key] for key in ("issue_id", "title", "field", "start", "end", "text")])
                        db.execute("INSERT INTO chunks_fts(rowid, tokens) VALUES (?, ?)",
                                   [cursor.lastrowid, " ".join(search_tokens(embedding_text(chunk)))])
                    db.execute("INSERT INTO issue_hashes VALUES (?, ?)", [issue_id, digest])
                for issue_id in existing:
                    checkpoint()
                    remove_issue(issue_id)
                if issue_count != meta.get("count"):
                    raise ValueError("원본 메타데이터와 이슈 건수가 다릅니다.")
                chunk_count = db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
                meta = {**meta, "version": VERSION, "issues": issue_count, "chunks": chunk_count}
                db.execute("DELETE FROM metadata")
                db.execute("INSERT INTO metadata VALUES (?)", [json.dumps(meta)])
                if reindexed or existing:
                    db.execute("INSERT INTO chunks_fts(chunks_fts) VALUES ('optimize')")
            with closing(_connect(source)) as current:
                source_advanced = _metadata(current) != source_meta
            # 완성된 읽기 세대만 게시한다. 원본이 앞서 갔으면 open_index가 사용을 막고
            # 다음 증분 구축에서 바뀐 이슈만 따라잡는다. 초기 전체 구축을 반복하지 않는다.
            os.replace(temporary, target)
            return {"status": "complete", "issues": issue_count, "chunks": chunk_count, "fetchedAt": meta["fetchedAt"],
                    "reindexed_issues": reindexed, "source_advanced": source_advanced}
        finally:
            for suffix in ("", "-journal", "-wal", "-shm"):
                Path(temporary + suffix).unlink(missing_ok=True)


@contextmanager
def open_index(source: Path, target: Path, identity: str):
    """불완전·다른 연결·다른 세대 인덱스를 검색에 섞지 않는다."""
    raw_db = db = None
    opened = None
    try:
        raw_db = _connect(source)
        db = _connect(target)
        raw_meta, meta = _metadata(raw_db), _metadata(db)
        if (meta.get("version") == VERSION and raw_meta.get("connection") == identity
                and all(meta.get(key) == raw_meta.get(key) for key in ("connection", "fetchedAt", "count"))):
            opened = db, raw_db, meta
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
        pass
    try:
        yield opened
    finally:
        if db is not None:
            db.close()
        if raw_db is not None:
            raw_db.close()


def lexical_chunks(db, query: str, *, issue_ids: set[int] | None = None) -> Generator[dict[str, Any], None, None]:
    tokens = list(dict.fromkeys(search_tokens(query)))[:80]
    if not tokens:
        return
    expression = " OR ".join(f'"{token}"' for token in tokens)
    # 먼저 잘라 버리지 않고, 호출자가 원본 조건에 맞는 이슈 수로 제한한다.
    # 순위 계산에는 행 ID·점수만 필요하다. 긴 구간 본문은 정렬이 끝난 뒤
    # 호출자가 소비하는 행만 읽어 같은 BM25·동점 순서를 유지한다.
    allowed_chunks: set[int] | None = None
    if issue_ids is not None:
        if not issue_ids:
            return
        allowed_chunks = set()
        selected = sorted(issue_ids)
        # chunks_issue의 작은 ID 색인만 읽는다. FTS 순위 계산에 본문 테이블을
        # 결합하면 넓은 키워드가 맞는 본문 페이지를 다시 무작위로 읽게 된다.
        for start in range(0,len(selected),500):
            checkpoint()
            batch = selected[start:start+500]
            allowed_chunks.update(row[0] for row in db.execute(
                'SELECT id FROM chunks WHERE issue_id IN (' + ','.join('?' for _ in batch) + ')', batch))
        if not allowed_chunks:
            return
    cursor = db.execute("""SELECT rowid, bm25(chunks_fts) AS lexical_score
        FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY lexical_score, rowid""", [expression])
    try:
        for ranked in cursor:
            checkpoint()
            if allowed_chunks is not None and ranked['rowid'] not in allowed_chunks:
                continue
            row = db.execute("SELECT * FROM chunks WHERE id=?", [ranked["rowid"]]).fetchone()
            if row is not None:
                yield {**dict(row), "lexical_score": ranked["lexical_score"]}
    finally:
        cursor.close()


def start_index_build(source: Path, target: Path, identity: str) -> None:
    if not source.is_file() or not _build_lock.acquire(blocking=False):
        return

    def work():
        try:
            result = build_index(source, target, identity)
            if result.get("source_advanced"):
                build_index(source, target, identity)
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            LOGGER.warning("Mantis 구간 인덱스 갱신 실패: 기존 검색 경로를 유지합니다.")
        finally:
            _build_lock.release()

    Thread(target=work, name="mantis-chunk-index", daemon=True).start()
