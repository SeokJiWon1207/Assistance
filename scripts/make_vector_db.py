#!/usr/bin/env python3
"""Mantis 이슈를 로컬 Chroma 스냅샷(data/mantis_chroma_db)으로 구축/갱신한다.

사용법:
    python3 scripts/make_vector_db.py                  # 증분: DB에 없는 새 이슈만 추가
    python3 scripts/make_vector_db.py --refresh-days 14  # 증분 + 최근 14일 내 수정된 이슈 재임베딩
    python3 scripts/make_vector_db.py --full           # 전체 재구축(기존 DB는 백업 후 새로 생성)

인증 정보는 하드코딩하지 않는다. 환경변수 MANTIS_API_URL / MANTIS_API_TOKEN
또는 assistant_web/.env 에서 읽는다.

매일 새벽 증분 갱신 crontab 예시(등록은 직접 결정해서 추가):
    30 6 * * * cd /path/to/Assistance && /usr/bin/python3 scripts/make_vector_db.py --refresh-days 7 >> data/mantis_chroma_db/rebuild.log 2>&1

문서 포맷 주의: 아래 헤더 필드명(등록일, 매체구분 등)은 assistant_web/backend/app.py 의
extract_chroma_field / extract_chroma_date 파서와 맞춰져 있다. 필드명을 바꾸면
백엔드 파서도 함께 바꿔야 한다.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path

import requests
import chromadb

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ASSISTANT_WEB_DIR = PROJECT_ROOT / "assistant_web"
if str(ASSISTANT_WEB_DIR) not in sys.path:
    sys.path.insert(0, str(ASSISTANT_WEB_DIR))

from backend.tools.mantis_index import (  # noqa: E402
    INDEX_FILENAME,
    MantisIndexError,
    build_mantis_index,
    chroma_documents_digest,
    read_chroma_documents,
    rebuild_mantis_index,
    validate_mantis_index,
)

CHROMA_DIR = PROJECT_ROOT / "data" / "mantis_chroma_db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
CHROMA_SQLITE = CHROMA_DIR / "chroma.sqlite3"
MANTIS_INDEX_SQLITE = CHROMA_DIR / INDEX_FILENAME
EMBEDDING_MODEL = "bge-m3:latest"
MAX_CHAR_LIMIT = 2500  # Ollama embedding wrapper adds prompt overhead; keep below bge-m3 context.
EMBED_BATCH_SIZE = 50
FRESHNESS_METADATA_FILENAME = "freshness.json"

DATE_CUSTOM_FIELDS = (
    "고객사요청일",
    "고객사전달 목표일",
    "고객사전달일",
    "개발완료 목표일",
    "QC 완료 목표일",
    "버전반영일",
)
DATE_CUSTOM_FIELD_NAMES = set(DATE_CUSTOM_FIELDS)


class SnapshotBuildError(RuntimeError):
    """A full snapshot could not be fetched, built, or validated safely."""


class Document:
    def __init__(self, page_content: str, metadata: dict) -> None:
        self.page_content = page_content
        self.metadata = metadata


class OllamaEmbeddingFunction:
    def __init__(self, url: str, model_name: str, timeout: int = 60) -> None:
        self.url = url.rstrip("/")
        self.model_name = model_name
        self.timeout = timeout

    @staticmethod
    def name() -> str:
        return "assistant-ollama"

    def __call__(self, input: list[str]) -> list[list[float]]:
        response = requests.post(
            f"{self.url}/api/embed",
            json={"model": self.model_name, "input": input},
            timeout=self.timeout,
        )
        response.raise_for_status()
        embeddings = response.json().get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) != len(input):
            raise SnapshotBuildError("Ollama 임베딩 응답 건수가 요청과 일치하지 않습니다.")
        return embeddings


class Chroma:
    """Small compatibility adapter over the official Chroma client API."""

    def __init__(self, persist_directory: str, embedding_function=None) -> None:
        if embedding_function is None:
            embedding_function = OllamaEmbeddingFunction(
                url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
                model_name=EMBEDDING_MODEL,
                timeout=60,
            )
        self._client = chromadb.PersistentClient(path=persist_directory)
        self._collection = self._client.get_or_create_collection(
            name="langchain",
            embedding_function=embedding_function,
        )

    def add_documents(
        self,
        documents: list[Document],
        embeddings: list[list[float]] | None = None,
    ) -> None:
        add_arguments = {
            "ids": [uuid.uuid4().hex for _ in documents],
            "documents": [document.page_content for document in documents],
            "metadatas": [document.metadata for document in documents],
        }
        if embeddings is not None:
            add_arguments["embeddings"] = embeddings
        self._collection.add(
            **add_arguments,
        )


def load_dotenv() -> None:
    env_path = PROJECT_ROOT / "assistant_web" / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def get_mantis_settings() -> tuple[str, dict]:
    url = os.getenv("MANTIS_API_URL", "").strip()
    token = os.getenv("MANTIS_API_TOKEN", "").strip()
    if not url or not token:
        print(
            "❌ MANTIS_API_URL / MANTIS_API_TOKEN 이 설정되지 않았습니다. "
            "assistant_web/.env 또는 환경변수로 설정하세요.",
            file=sys.stderr,
        )
        sys.exit(1)
    return url, {"Authorization": token, "Content-Type": "application/json"}


def format_custom_date(value) -> str:
    raw_value = str(value or "").strip()
    if not raw_value or raw_value in {"0", "-"}:
        return "미상"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_value):
        return raw_value
    if re.fullmatch(r"\d{8}", raw_value):
        return f"{raw_value[:4]}-{raw_value[4:6]}-{raw_value[6:8]}"
    if re.fullmatch(r"\d{10,13}", raw_value):
        timestamp = int(raw_value)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        try:
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return raw_value
    return raw_value


def issue_date(value) -> str:
    """Mantis의 created_at/updated_at(ISO 형식)을 YYYY-MM-DD로 정규화."""
    raw = str(value or "").strip()
    return raw[:10] if re.match(r"\d{4}-\d{2}-\d{2}", raw) else "미상"


def build_issue_document(issue: dict) -> Document:
    issue_id = issue.get("id")
    project_name = issue.get("project", {}).get("name", "프로젝트 미상")
    status_label = issue.get("status", {}).get("label", "상태 미상")
    handler_name = issue.get("handler", {}).get("real_name", "담당자 미지정")
    reporter_name = issue.get("reporter", {}).get("real_name", "미상")
    summary = issue.get("summary", "제목 없음")
    description = issue.get("description", "")
    steps = issue.get("steps_to_reproduce", "재현 단계 정보 없음")
    created_at = issue_date(issue.get("created_at"))
    updated_at = issue_date(issue.get("updated_at"))

    notes_text_pieces = []
    for note in issue.get("notes", []):
        author = note.get("reporter", {}).get("real_name", "미상")
        notes_text_pieces.append(f"- [{author}]: {note.get('text', '')}")
    notes_text = "\n".join(notes_text_pieces) if notes_text_pieces else "등록된 이력 없음"

    media_type = "기타/미상"
    screen_number = "미상"
    work_subject = "미상"
    date_fields = {field_name: "미상" for field_name in DATE_CUSTOM_FIELDS}
    effort_pieces = []
    for c_field in issue.get("custom_fields", []):
        field_name = c_field.get("field", {}).get("name", "")
        field_value = c_field.get("value", "")
        if field_name == "매체구분" and field_value:
            media_type = field_value
        elif field_name == "화면번호" and field_value:
            screen_number = field_value
        elif field_name == "작업주체" and field_value:
            work_subject = field_value
        elif field_name in DATE_CUSTOM_FIELD_NAMES:
            date_fields[field_name] = format_custom_date(field_value)
        elif field_name and "공수" in field_name and str(field_value).strip():
            effort_pieces.append(f"- {field_name}: {field_value}")

    date_fields_text = "\n".join(
        f"- {field_name}: {field_value}" for field_name, field_value in date_fields.items()
    )
    effort_text = "\n".join(effort_pieces) if effort_pieces else "- 기록 없음"

    text = f"""
[Mantis 이슈 정보]
- 이슈 ID: #{issue_id}
- 프로젝트(고객사): {project_name}
- 매체구분: {media_type}
- 화면번호: {screen_number}
- 작업주체: {work_subject}
- 현재 상태: {status_label}
- 담당자: {handler_name}
- 요청자: {reporter_name}
- 등록일: {created_at}
- 최종수정일: {updated_at}

[일정/전달일]
{date_fields_text}

[공수]
{effort_text}

[제목]
{summary}

[고객사 본문 요청 내용]
{description}

[재현 과정 및 단계]
{steps}

[내부 개발 진행 및 대화 이력]
{notes_text}
""".strip()

    if len(text) > MAX_CHAR_LIMIT:
        text = text[:MAX_CHAR_LIMIT] + "\n\n... [SYSTEM WARNING: 너무 긴 텍스트로 인해 이하 내용 생략] ..."
    return Document(page_content=text, metadata={"id": issue_id})


def load_existing_issue_ids(database_path: Path | None = None) -> set[int]:
    """기존 스냅샷에 이미 들어있는 이슈 ID 집합."""
    database_path = database_path or CHROMA_SQLITE
    if not database_path.exists():
        return set()
    issue_ids: set[int] = set()
    with closing(sqlite3.connect(database_path)) as connection:
        rows = connection.execute(
            "SELECT string_value FROM embedding_metadata WHERE key = 'chroma:document'"
        )
        for (content,) in rows:
            match = re.search(r"이슈 ID: #(\d+)", str(content or ""))
            if match:
                issue_ids.add(int(match.group(1)))
    return issue_ids


def fetch_issue_pages(url: str, headers: dict, page_size: int = 200, max_pages: int = 200):
    page = 1
    while page <= max_pages:
        response = None
        last_error = None
        for attempt in range(1, 4):
            try:
                candidate = requests.get(
                    url,
                    headers=headers,
                    params={"page_size": page_size, "page": page},
                    timeout=60,
                )
            except requests.RequestException as exc:
                last_error = exc
            else:
                if candidate.status_code < 500:
                    response = candidate
                    break
                last_error = SnapshotBuildError(
                    f"Mantis API 일시 오류: Status {candidate.status_code}"
                )
            if attempt < 3:
                delay = 2 ** (attempt - 1)
                print(
                    f"⚠️ {page}페이지 수집 실패, {delay}초 후 재시도 "
                    f"({attempt}/3): {type(last_error).__name__}",
                    file=sys.stderr,
                )
                time.sleep(delay)
        if response is None:
            raise SnapshotBuildError(
                f"Mantis {page}페이지를 3회 시도했지만 수집하지 못했습니다: {last_error}"
            ) from last_error
        if response.status_code != 200:
            raise SnapshotBuildError(f"Mantis API 오류: Status {response.status_code}")
        issues = response.json().get("issues", [])
        if not issues:
            return
        yield page, issues
        if len(issues) < page_size:
            return
        page += 1
    raise SnapshotBuildError(
        f"최대 {max_pages}페이지에 도달해 전체 수집 완료를 확인하지 못했습니다."
    )


def delete_documents_for_issue(vector_db: Chroma, issue_id: int) -> int:
    """metadata id로 기존 문서를 찾아 삭제(refresh 재임베딩용)."""
    existing = vector_db._collection.get(where={"id": issue_id})
    doc_ids = existing.get("ids") or []
    if doc_ids:
        vector_db._collection.delete(ids=doc_ids)
    return len(doc_ids)


def add_documents_batched(vector_db: Chroma, documents: list[Document]) -> None:
    for start in range(0, len(documents), EMBED_BATCH_SIZE):
        batch = documents[start : start + EMBED_BATCH_SIZE]
        vector_db.add_documents(batch)
        print(f"  🧠 임베딩 진행 {min(start + EMBED_BATCH_SIZE, len(documents))}/{len(documents)}")


def canonical_document_for_reuse(document: str) -> str:
    """순서 의미가 없는 사용자 정의 필드 구간만 정렬해 과거 문서와 비교한다."""
    lines = document.splitlines()
    for section_name in ("[일정/전달일]", "[공수]"):
        try:
            section_start = lines.index(section_name) + 1
        except ValueError:
            continue
        section_end = section_start
        while section_end < len(lines) and lines[section_end]:
            section_end += 1
        lines[section_start:section_end] = sorted(lines[section_start:section_end])
    return "\n".join(lines)


def write_snapshot_freshness_metadata(
    snapshot_dir: Path,
    documents: list[Document],
    document_count: int,
    *,
    merge_existing: bool = False,
) -> None:
    today = datetime.now().date().isoformat()
    candidates = []
    for document in documents:
        match = re.search(r"(?m)^- 등록일:\s*(\d{4}-\d{2}-\d{2})$", document.page_content)
        if match and match.group(1) <= today:
            candidates.append(match.group(1))

    metadata_path = snapshot_dir / FRESHNESS_METADATA_FILENAME
    if merge_existing and metadata_path.exists():
        try:
            existing = json.loads(metadata_path.read_text(encoding="utf-8"))
            existing_as_of = str(existing.get("asOf") or "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", existing_as_of):
                candidates.append(existing_as_of)
        except (OSError, json.JSONDecodeError):
            pass
    if not candidates:
        raise SnapshotBuildError("스냅샷 등록일 메타데이터를 계산할 수 없습니다.")

    payload = {
        "asOf": max(candidates),
        "generatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "documentCount": document_count,
    }
    temporary_path = metadata_path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, metadata_path)


def reuse_unchanged_embeddings(
    vector_db: Chroma,
    documents: list[Document],
) -> list[Document]:
    """내용이 완전히 같은 기존 문서의 임베딩만 새 스냅샷에서 재사용한다."""
    if not (CHROMA_DIR / "chroma.sqlite3").exists():
        return documents

    desired_by_id = {
        int(document.metadata["id"]): document
        for document in documents
        if document.metadata.get("id") is not None
    }
    try:
        source_client = chromadb.PersistentClient(path=str(CHROMA_DIR))
        source_collection = source_client.get_collection(name="langchain")
        source_count = source_collection.count()
    except Exception as exc:
        print(
            f"⚠️ 기존 임베딩을 읽을 수 없어 전체를 다시 계산합니다: {type(exc).__name__}",
            file=sys.stderr,
        )
        return documents

    reused_ids: set[int] = set()
    read_batch_size = 500
    try:
        for offset in range(0, source_count, read_batch_size):
            result = source_collection.get(
                limit=read_batch_size,
                offset=offset,
                include=["documents", "metadatas", "embeddings"],
            )
            source_documents = result.get("documents") or []
            source_metadatas = result.get("metadatas") or []
            source_embeddings = result.get("embeddings")
            if source_embeddings is None:
                raise SnapshotBuildError("기존 스냅샷에 임베딩 값이 없습니다.")

            reusable_documents: list[Document] = []
            reusable_embeddings: list[list[float]] = []
            for source_document, metadata, embedding in zip(
                source_documents,
                source_metadatas,
                source_embeddings,
                strict=True,
            ):
                if not metadata or metadata.get("id") is None:
                    continue
                issue_id = int(metadata["id"])
                desired = desired_by_id.get(issue_id)
                if (
                    desired is None
                    or issue_id in reused_ids
                    or canonical_document_for_reuse(source_document)
                    != canonical_document_for_reuse(desired.page_content)
                ):
                    continue
                reusable_documents.append(desired)
                reusable_embeddings.append(
                    embedding.tolist() if hasattr(embedding, "tolist") else list(embedding)
                )
                reused_ids.add(issue_id)

            if reusable_documents:
                vector_db.add_documents(reusable_documents, reusable_embeddings)
            print(
                f"  ♻️ 기존 임베딩 검증 {min(offset + read_batch_size, source_count)}/"
                f"{source_count} (재사용 {len(reused_ids)})"
            )
    except Exception as exc:
        raise SnapshotBuildError(
            f"기존 임베딩 재사용 중 오류가 발생해 새 임시 DB를 폐기합니다: {exc}"
        ) from exc

    print(
        f"♻️ 내용이 동일한 {len(reused_ids)}건은 기존 임베딩을 재사용하고 "
        f"{len(documents) - len(reused_ids)}건만 다시 계산합니다."
    )
    return [
        document
        for document in documents
        if int(document.metadata.get("id", -1)) not in reused_ids
    ]


def run_incremental(url: str, headers: dict, refresh_days: int, max_pages: int) -> None:
    existing_ids = load_existing_issue_ids()
    print(f"📦 기존 스냅샷 이슈 수: {len(existing_ids)}")
    if not existing_ids:
        print("⚠️ 기존 스냅샷이 비어 있습니다. --full 로 전체 구축을 먼저 실행하세요.", file=sys.stderr)
        sys.exit(1)

    refresh_cutoff = (
        (datetime.now() - timedelta(days=refresh_days)).strftime("%Y-%m-%d")
        if refresh_days > 0
        else None
    )
    new_documents: list[Document] = []
    refreshed_documents: list[Document] = []
    # Mantis가 최신순으로 반환하므로, "새 이슈도 refresh 대상도 전혀 없는 페이지"가
    # 연속으로 나오면 그 뒤로는 전부 기존 데이터라고 보고 조기 종료한다.
    stale_page_streak = 0

    for page, issues in fetch_issue_pages(url, headers, max_pages=max_pages):
        page_has_work = False
        for issue in issues:
            issue_id = issue.get("id")
            if not issue_id:
                continue
            if issue_id not in existing_ids:
                new_documents.append(build_issue_document(issue))
                existing_ids.add(issue_id)
                page_has_work = True
            elif refresh_cutoff and issue_date(issue.get("updated_at")) >= refresh_cutoff:
                refreshed_documents.append(build_issue_document(issue))
                page_has_work = True
        print(
            f"✅ {page}페이지 검토 완료 "
            f"(신규 누적 {len(new_documents)}건, 갱신 후보 {len(refreshed_documents)}건)"
        )
        if page_has_work:
            stale_page_streak = 0
        else:
            stale_page_streak += 1
            if stale_page_streak >= 2:
                print("⏹️ 새 이슈/갱신 대상 없는 페이지가 이어져 조기 종료합니다.")
                break

    changed_documents = new_documents + refreshed_documents
    if not changed_documents and MANTIS_INDEX_SQLITE.exists():
        print("🏁 증분 갱신 완료: 변경 사항이 없습니다.")
        return

    CHROMA_DIR.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = CHROMA_DIR.parent / f".{CHROMA_DIR.name}-incremental-{uuid.uuid4().hex}"
    try:
        shutil.copytree(CHROMA_DIR, temporary_dir)
        temporary_chroma = temporary_dir / "chroma.sqlite3"
        temporary_index = temporary_dir / INDEX_FILENAME
        vector_db = Chroma(persist_directory=str(temporary_dir))
        for document in refreshed_documents:
            deleted = delete_documents_for_issue(vector_db, int(document.metadata["id"]))
            if deleted != 1:
                raise SnapshotBuildError(
                    f"갱신 대상 #{document.metadata['id']}의 기존 문서가 {deleted}건입니다."
                )
        if changed_documents:
            print(f"🧠 신규·갱신 이슈 {len(changed_documents)}건 임베딩 중...")
            add_documents_batched(vector_db, changed_documents)
        document_count = vector_db._collection.count()
        if changed_documents:
            write_snapshot_freshness_metadata(
                temporary_dir,
                changed_documents,
                document_count,
                merge_existing=True,
            )
        del vector_db
        gc.collect()
        index_stats = rebuild_mantis_index(temporary_chroma, temporary_index)
        validate_snapshot(temporary_dir, document_count)
        backup = activate_snapshot(temporary_dir)
        print(
            f"🗂️ Chroma와 구조화 인덱스 {index_stats['document_count']}건을 "
            f"원자적으로 갱신했습니다. 이전 스냅샷: {backup}"
        )
    finally:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)
    print(
        f"🏁 증분 갱신 완료: 신규 {len(new_documents)}건 추가, "
        f"{len(refreshed_documents)}건 재임베딩"
    )


def validate_snapshot(snapshot_dir: Path, expected_documents: int) -> None:
    database_path = snapshot_dir / "chroma.sqlite3"
    if not database_path.exists():
        raise SnapshotBuildError("새 스냅샷에 chroma.sqlite3가 없습니다.")
    with closing(sqlite3.connect(database_path)) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        if integrity != "ok":
            raise SnapshotBuildError(f"새 스냅샷 무결성 검사 실패: {integrity}")
        try:
            stored_documents = int(
                connection.execute(
                    "SELECT COUNT(*) FROM embedding_metadata WHERE key = 'chroma:document'"
                ).fetchone()[0]
            )
        except sqlite3.Error as exc:
            raise SnapshotBuildError(f"새 스냅샷 스키마 검사 실패: {exc}") from exc
    try:
        minimum_documents = max(1, int(os.getenv("SNAPSHOT_MIN_DOCUMENTS", "1")))
    except ValueError:
        minimum_documents = 1
    if expected_documents < minimum_documents:
        raise SnapshotBuildError(
            f"수집 문서 {expected_documents}건이 최소 기준 {minimum_documents}건보다 적습니다."
        )
    if stored_documents != expected_documents:
        raise SnapshotBuildError(
            f"문서 건수 불일치: collected={expected_documents}, stored={stored_documents}"
        )
    metadata_path = snapshot_dir / FRESHNESS_METADATA_FILENAME
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SnapshotBuildError(f"신선도 메타데이터 검사 실패: {exc}") from exc
    if metadata.get("documentCount") != expected_documents or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", str(metadata.get("asOf") or "")
    ):
        raise SnapshotBuildError("신선도 메타데이터 내용이 스냅샷과 일치하지 않습니다.")
    documents = read_chroma_documents(database_path)
    try:
        validate_mantis_index(
            snapshot_dir / INDEX_FILENAME,
            expected_documents=expected_documents,
            expected_digest=chroma_documents_digest(documents),
        )
    except MantisIndexError as exc:
        raise SnapshotBuildError(f"구조화 인덱스 검사 실패: {exc}") from exc


def prune_snapshot_backups() -> None:
    try:
        keep = max(1, min(int(os.getenv("SNAPSHOT_BACKUP_KEEP", "3")), 20))
    except ValueError:
        keep = 3
    backups = sorted(
        (path for path in BACKUP_DIR.glob(f"{CHROMA_DIR.name}-*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for expired in backups[keep:]:
        shutil.rmtree(expired)


def activate_snapshot(temporary_dir: Path) -> Path | None:
    """Atomically activate a validated snapshot directory with rollback."""
    backup = None
    if CHROMA_DIR.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / (
            f"{CHROMA_DIR.name}-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
        )
        os.replace(CHROMA_DIR, backup)
    try:
        os.replace(temporary_dir, CHROMA_DIR)
    except Exception:
        if backup is not None and backup.exists() and not CHROMA_DIR.exists():
            os.replace(backup, CHROMA_DIR)
        raise
    prune_snapshot_backups()
    return backup


def run_full(url: str, headers: dict, max_pages: int) -> None:
    documents: list[Document] = []
    for page, issues in fetch_issue_pages(url, headers, max_pages=max_pages):
        documents.extend(build_issue_document(issue) for issue in issues)
        print(f"✅ {len(documents)}개 이슈 누적 추출 (현재 {page}페이지)")

    if not documents:
        raise SnapshotBuildError("Mantis에서 수집된 이슈가 0건이라 기존 스냅샷을 유지합니다.")

    print(f"🎉 총 {len(documents)}개 이슈 파싱 완료. 임베딩을 시작합니다...")
    CHROMA_DIR.parent.mkdir(parents=True, exist_ok=True)
    temporary_dir = Path(
        tempfile.mkdtemp(prefix=f".{CHROMA_DIR.name}-build-", dir=CHROMA_DIR.parent)
    )
    try:
        vector_db = Chroma(persist_directory=str(temporary_dir))
        documents_to_embed = reuse_unchanged_embeddings(vector_db, documents)
        add_documents_batched(vector_db, documents_to_embed)
        index_stats = build_mantis_index(
            temporary_dir / INDEX_FILENAME,
            (document.page_content for document in documents),
        )
        write_snapshot_freshness_metadata(temporary_dir, documents, len(documents))
        del vector_db
        gc.collect()
        validate_snapshot(temporary_dir, len(documents))
        print(
            f"🗂️ 구조화 인덱스 검증 완료: {index_stats['document_count']}건, "
            f"최대 이슈 #{index_stats['max_issue_id']}"
        )

        backup = activate_snapshot(temporary_dir)
        if backup is not None:
            print(f"🗄️ 기존 스냅샷을 백업했습니다: {backup}")
        print("🏆 전체 재구축 검증 및 원자 교체가 완료되었습니다.")
    finally:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="전체 재구축(기존 DB 백업 후 새로 생성)")
    parser.add_argument(
        "--rebuild-index-only",
        action="store_true",
        help="기존 Chroma 문서에서 구조화 보조 인덱스만 원자적으로 재구축",
    )
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=0,
        help="증분 모드에서 최근 N일 내 수정된 기존 이슈를 재임베딩(상태 변경 반영)",
    )
    parser.add_argument("--max-pages", type=int, default=200, help="Mantis 조회 최대 페이지 수")
    args = parser.parse_args()

    if args.rebuild_index_only:
        if not CHROMA_SQLITE.exists():
            raise SnapshotBuildError(f"Chroma DB가 없습니다: {CHROMA_SQLITE}")
        stats = rebuild_mantis_index(CHROMA_SQLITE, MANTIS_INDEX_SQLITE)
        validate_snapshot(CHROMA_DIR, int(stats["document_count"]))
        print(
            f"🏆 구조화 인덱스 재구축 완료: {stats['document_count']}건, "
            f"기간 {stats['min_created_at'] or '미상'}~{stats['max_created_at'] or '미상'}"
        )
        return

    load_dotenv()
    url, headers = get_mantis_settings()

    if args.full:
        run_full(url, headers, args.max_pages)
    else:
        run_incremental(url, headers, args.refresh_days, args.max_pages)


if __name__ == "__main__":
    main()
