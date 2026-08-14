"""Structured SQLite companion index for the Mantis Chroma snapshot."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable


INDEX_FILENAME = "mantis_index.sqlite3"
# v2: 이슈 유형(멘티스 severity.label) 컬럼 추가
INDEX_SCHEMA_VERSION = "2"
DATE_FIELD_NAMES = (
    "고객사요청일",
    "고객사전달 목표일",
    "고객사전달일",
    "개발완료 목표일",
    "QC 완료 목표일",
    "버전반영일",
)


class MantisIndexError(RuntimeError):
    """The structured index could not be created or validated."""


def normalize_index_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", str(value or "").lower())


def normalize_index_date(value: Any) -> str:
    raw = str(value or "").strip()
    if re.fullmatch(r"\d{8}", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


def extract_document_field(document: str, field_name: str) -> str:
    match = re.search(
        rf"(?m)^-\s*{re.escape(field_name)}:\s*(.*?)\s*$",
        document,
    )
    return str(match.group(1) if match else "").strip()


def extract_document_title(document: str) -> str:
    match = re.search(r"(?ms)^\[제목\]\s*\n(.*?)(?:\n\n|\Z)", document)
    return str(match.group(1) if match else "").strip()


def parse_index_document(document: str) -> dict[str, Any]:
    issue_match = re.search(r"이슈\s*ID:\s*#?(\d+)", document)
    if not issue_match:
        raise MantisIndexError("이슈 ID가 없는 문서는 구조화 인덱스에 넣을 수 없습니다.")

    customer = extract_document_field(document, "프로젝트(고객사)")
    if not customer:
        customer = extract_document_field(document, "프로젝트")
    platform = extract_document_field(document, "매체구분")
    handler = extract_document_field(document, "담당자")
    reporter = extract_document_field(document, "요청자")
    status = extract_document_field(document, "현재 상태")
    # 과거에 수집된 문서에는 이슈 유형 줄이 없다. 빈 값으로 두고 조회 쪽에서 거른다.
    issue_type = extract_document_field(document, "이슈 유형")
    created_at = extract_document_field(document, "등록일")
    updated_at = extract_document_field(document, "최종수정일")
    title = extract_document_title(document)
    return {
        "issue_id": int(issue_match.group(1)),
        "created_at": created_at,
        "updated_at": updated_at,
        "status": status,
        "issue_type": issue_type,
        "handler": handler,
        "reporter": reporter,
        "customer": customer,
        "platform": platform,
        "screen_number": extract_document_field(document, "화면번호"),
        "work_subject": extract_document_field(document, "작업주체"),
        "title": title,
        "document": document,
        "document_hash": hashlib.sha256(document.encode("utf-8")).hexdigest(),
        "status_normalized": normalize_index_text(status),
        "issue_type_normalized": normalize_index_text(issue_type),
        "handler_normalized": normalize_index_text(handler),
        "reporter_normalized": normalize_index_text(reporter),
        "customer_normalized": normalize_index_text(customer),
        "platform_normalized": normalize_index_text(platform),
        "screen_number_normalized": normalize_index_text(
            extract_document_field(document, "화면번호")
        ),
        "date_fields": {
            field_name: normalize_index_date(extract_document_field(document, field_name))
            for field_name in DATE_FIELD_NAMES
        },
    }


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE metadata (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL
        );
        CREATE TABLE issues (
          issue_id INTEGER PRIMARY KEY,
          created_at TEXT,
          updated_at TEXT,
          status TEXT,
          issue_type TEXT,
          handler TEXT,
          reporter TEXT,
          customer TEXT,
          platform TEXT,
          screen_number TEXT,
          work_subject TEXT,
          title TEXT NOT NULL,
          document TEXT NOT NULL,
          document_hash TEXT NOT NULL,
          status_normalized TEXT NOT NULL,
          issue_type_normalized TEXT NOT NULL,
          handler_normalized TEXT NOT NULL,
          reporter_normalized TEXT NOT NULL,
          customer_normalized TEXT NOT NULL,
          platform_normalized TEXT NOT NULL,
          screen_number_normalized TEXT NOT NULL
        );
        CREATE TABLE issue_dates (
          issue_id INTEGER NOT NULL,
          field_name TEXT NOT NULL,
          date_value TEXT NOT NULL,
          PRIMARY KEY (issue_id, field_name),
          FOREIGN KEY (issue_id) REFERENCES issues(issue_id) ON DELETE CASCADE
        );
        CREATE INDEX idx_issues_created_at ON issues(created_at);
        CREATE INDEX idx_issues_updated_at ON issues(updated_at);
        CREATE INDEX idx_issues_status ON issues(status_normalized);
        CREATE INDEX idx_issues_issue_type ON issues(issue_type_normalized);
        CREATE INDEX idx_issues_customer ON issues(customer_normalized);
        CREATE INDEX idx_issues_platform ON issues(platform_normalized);
        CREATE INDEX idx_issues_handler ON issues(handler_normalized);
        CREATE INDEX idx_issues_reporter ON issues(reporter_normalized);
        CREATE INDEX idx_issues_screen_number ON issues(screen_number_normalized);
        CREATE INDEX idx_issue_dates_lookup ON issue_dates(field_name, date_value, issue_id);
        """
    )


def _documents_digest(rows: Iterable[tuple[int, str]]) -> str:
    digest = hashlib.sha256()
    for issue_id, document_hash in sorted(rows):
        digest.update(f"{issue_id}:{document_hash}\n".encode("ascii"))
    return digest.hexdigest()


def build_mantis_index(index_path: Path, documents: Iterable[str]) -> dict[str, Any]:
    """Build the companion index into a temporary file and atomically replace it."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = index_path.with_name(f".{index_path.name}.{uuid.uuid4().hex}.tmp")
    records = [parse_index_document(str(document or "")) for document in documents]
    if not records:
        raise MantisIndexError("구조화 인덱스에 넣을 문서가 없습니다.")

    try:
        with closing(sqlite3.connect(temporary_path)) as connection, connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = OFF")
            connection.execute("PRAGMA synchronous = OFF")
            _create_schema(connection)
            connection.executemany(
                """
                INSERT INTO issues (
                  issue_id, created_at, updated_at, status, issue_type, handler, reporter,
                  customer, platform, screen_number, work_subject, title,
                  document, document_hash, status_normalized, issue_type_normalized,
                  handler_normalized, reporter_normalized, customer_normalized,
                  platform_normalized, screen_number_normalized
                ) VALUES (
                  :issue_id, :created_at, :updated_at, :status, :issue_type, :handler, :reporter,
                  :customer, :platform, :screen_number, :work_subject, :title,
                  :document, :document_hash, :status_normalized, :issue_type_normalized,
                  :handler_normalized, :reporter_normalized, :customer_normalized,
                  :platform_normalized, :screen_number_normalized
                )
                """,
                records,
            )
            date_rows = [
                (record["issue_id"], field_name, date_value)
                for record in records
                for field_name, date_value in record["date_fields"].items()
                if date_value and date_value != "미상"
            ]
            connection.executemany(
                "INSERT INTO issue_dates(issue_id, field_name, date_value) VALUES (?, ?, ?)",
                date_rows,
            )
            created_dates = [
                str(record["created_at"])
                for record in records
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(record["created_at"]))
            ]
            metadata = {
                "schema_version": INDEX_SCHEMA_VERSION,
                "document_count": str(len(records)),
                "max_issue_id": str(max(int(record["issue_id"]) for record in records)),
                "min_created_at": min(created_dates, default=""),
                "max_created_at": max(created_dates, default=""),
                "documents_digest": _documents_digest(
                    (int(record["issue_id"]), str(record["document_hash"]))
                    for record in records
                ),
            }
            connection.executemany(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                metadata.items(),
            )
        validate_mantis_index(index_path=temporary_path, expected_documents=len(records))
        os.replace(temporary_path, index_path)
        return {
            "document_count": len(records),
            "max_issue_id": int(metadata["max_issue_id"]),
            "min_created_at": metadata["min_created_at"],
            "max_created_at": metadata["max_created_at"],
            "documents_digest": metadata["documents_digest"],
        }
    except (OSError, sqlite3.Error, MantisIndexError) as exc:
        if isinstance(exc, MantisIndexError):
            raise
        raise MantisIndexError(f"구조화 인덱스 생성 실패: {exc}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)


def read_chroma_documents(chroma_database_path: Path) -> list[str]:
    try:
        with closing(sqlite3.connect(chroma_database_path)) as connection:
            return [
                str(row[0] or "")
                for row in connection.execute(
                    "SELECT string_value FROM embedding_metadata WHERE key = 'chroma:document'"
                )
            ]
    except sqlite3.Error as exc:
        raise MantisIndexError(f"Chroma 문서 조회 실패: {exc}") from exc


def rebuild_mantis_index(chroma_database_path: Path, index_path: Path) -> dict[str, Any]:
    return build_mantis_index(index_path, read_chroma_documents(chroma_database_path))


def validate_mantis_index(
    index_path: Path,
    expected_documents: int | None = None,
    expected_digest: str | None = None,
) -> dict[str, str]:
    if not index_path.exists():
        raise MantisIndexError(f"구조화 인덱스가 없습니다: {index_path}")
    try:
        with closing(sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)) as connection:
            quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
            if quick_check != "ok":
                raise MantisIndexError(f"구조화 인덱스 무결성 검사 실패: {quick_check}")
            metadata = dict(connection.execute("SELECT key, value FROM metadata"))
            row_count = int(connection.execute("SELECT COUNT(*) FROM issues").fetchone()[0])
            actual_digest = _documents_digest(
                (int(issue_id), str(document_hash))
                for issue_id, document_hash in connection.execute(
                    "SELECT issue_id, document_hash FROM issues"
                )
            )
    except sqlite3.Error as exc:
        raise MantisIndexError(f"구조화 인덱스 검사 실패: {exc}") from exc

    if metadata.get("schema_version") != INDEX_SCHEMA_VERSION:
        raise MantisIndexError("구조화 인덱스 스키마 버전이 일치하지 않습니다.")
    if int(metadata.get("document_count", "-1")) != row_count:
        raise MantisIndexError("구조화 인덱스 메타데이터 건수가 실제 행 수와 다릅니다.")
    if metadata.get("documents_digest") != actual_digest:
        raise MantisIndexError("구조화 인덱스 문서 해시가 일치하지 않습니다.")
    if expected_documents is not None and row_count != expected_documents:
        raise MantisIndexError(
            f"구조화 인덱스 건수 불일치: expected={expected_documents}, stored={row_count}"
        )
    if expected_digest is not None and actual_digest != expected_digest:
        raise MantisIndexError("Chroma 문서와 구조화 인덱스 해시가 일치하지 않습니다.")
    return metadata


def chroma_documents_digest(documents: Iterable[str]) -> str:
    records = [parse_index_document(document) for document in documents]
    return _documents_digest(
        (int(record["issue_id"]), str(record["document_hash"]))
        for record in records
    )


def build_candidate_query(
    constraints: dict[str, Any] | None,
    scan_limit: int | None,
) -> tuple[str, list[Any]]:
    """Build an indexed candidate query; final matching remains in Python."""
    if scan_limit:
        return "SELECT document FROM issues ORDER BY issue_id DESC LIMIT ?", [scan_limit]

    constraints = constraints or {}
    clauses: list[str] = []
    params: list[Any] = []
    if constraints.get("exclude_issue_ids"):
        issue_ids = [int(issue_id) for issue_id in constraints["exclude_issue_ids"]]
        clauses.append("issue_id NOT IN (" + ",".join("?" for _ in issue_ids) + ")")
        params.extend(issue_ids)
    if constraints.get("max_issue_id_exclusive"):
        clauses.append("issue_id < ?")
        params.append(int(constraints["max_issue_id_exclusive"]))

    created_at = constraints.get("created_at")
    if isinstance(created_at, dict) and created_at.get("from") and created_at.get("to"):
        clauses.append("created_at BETWEEN ? AND ?")
        params.extend([str(created_at["from"]), str(created_at["to"])])

    exact_customer = normalize_index_text(constraints.get("customer_exact"))
    if exact_customer:
        clauses.append("customer_normalized = ?")
        params.append(exact_customer)
    elif constraints.get("customer_aliases"):
        aliases = [
            normalize_index_text(alias)
            for alias in constraints["customer_aliases"]
            if normalize_index_text(alias)
        ]
        if aliases:
            clauses.append("(" + " OR ".join("customer_normalized LIKE ?" for _ in aliases) + ")")
            params.extend(f"%{alias}%" for alias in aliases)

    if constraints.get("platform_aliases"):
        aliases = [
            normalize_index_text(alias)
            for alias in constraints["platform_aliases"]
            if normalize_index_text(alias)
        ]
        if aliases:
            clauses.append(
                "(platform_normalized IN ('', '기타미상') OR "
                + " OR ".join("platform_normalized LIKE ?" for _ in aliases)
                + ")"
            )
            params.extend(f"%{alias}%" for alias in aliases)

    if constraints.get("unassigned_people"):
        clauses.append("handler_normalized IN ('', '담당자미지정', '미지정', '미상')")

    screen_numbers = [
        normalize_index_text(value)
        for value in constraints.get("screen_numbers", [])
        if normalize_index_text(value)
    ]
    if screen_numbers:
        clauses.append(
            "(" + " OR ".join(
                "screen_number_normalized = ? OR title LIKE ?" for _ in screen_numbers
            ) + ")"
        )
        for screen_number in screen_numbers:
            params.extend([screen_number, f"%{screen_number}%"])

    for field_name, date_value in (constraints.get("date_fields") or {}).items():
        clauses.append(
            "EXISTS (SELECT 1 FROM issue_dates d WHERE d.issue_id = issues.issue_id "
            "AND d.field_name = ? AND d.date_value = ?)"
        )
        params.extend([str(field_name), str(date_value)])

    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return f"SELECT document FROM issues{where} ORDER BY issue_id DESC", params
