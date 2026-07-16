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
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

import requests
from langchain.schema import Document
from langchain_community.embeddings import OllamaEmbeddings
from langchain_community.vectorstores import Chroma

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CHROMA_DIR = PROJECT_ROOT / "data" / "mantis_chroma_db"
BACKUP_DIR = PROJECT_ROOT / "data" / "backups"
CHROMA_SQLITE = CHROMA_DIR / "chroma.sqlite3"
EMBEDDING_MODEL = "bge-m3:latest"
MAX_CHAR_LIMIT = 2500  # Ollama embedding wrapper adds prompt overhead; keep below bge-m3 context.
EMBED_BATCH_SIZE = 50

DATE_CUSTOM_FIELDS = {
    "고객사요청일",
    "고객사전달 목표일",
    "고객사전달일",
    "개발완료 목표일",
    "QC 완료 목표일",
    "버전반영일",
}


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
        elif field_name in DATE_CUSTOM_FIELDS:
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


def load_existing_issue_ids() -> set[int]:
    """기존 스냅샷에 이미 들어있는 이슈 ID 집합."""
    if not CHROMA_SQLITE.exists():
        return set()
    issue_ids: set[int] = set()
    with sqlite3.connect(CHROMA_SQLITE) as connection:
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
        response = requests.get(
            url,
            headers=headers,
            params={"page_size": page_size, "page": page},
            timeout=60,
        )
        if response.status_code != 200:
            print(f"❌ Mantis API 오류: Status {response.status_code}", file=sys.stderr)
            return
        issues = response.json().get("issues", [])
        if not issues:
            return
        yield page, issues
        page += 1


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
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
    vector_db = Chroma(persist_directory=str(CHROMA_DIR), embedding_function=embeddings)

    new_documents: list[Document] = []
    refreshed_count = 0
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
                deleted = delete_documents_for_issue(vector_db, issue_id)
                if deleted:
                    add_documents_batched(vector_db, [build_issue_document(issue)])
                    refreshed_count += 1
                    page_has_work = True
        print(f"✅ {page}페이지 검토 완료 (신규 누적 {len(new_documents)}건, 갱신 {refreshed_count}건)")
        if page_has_work:
            stale_page_streak = 0
        else:
            stale_page_streak += 1
            if stale_page_streak >= 2:
                print("⏹️ 새 이슈/갱신 대상 없는 페이지가 이어져 조기 종료합니다.")
                break

    if new_documents:
        print(f"🧠 신규 이슈 {len(new_documents)}건 임베딩 중...")
        add_documents_batched(vector_db, new_documents)
    print(f"🏁 증분 갱신 완료: 신규 {len(new_documents)}건 추가, {refreshed_count}건 재임베딩")


def run_full(url: str, headers: dict, max_pages: int) -> None:
    if CHROMA_DIR.exists():
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        backup = BACKUP_DIR / f"{CHROMA_DIR.name}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        shutil.move(str(CHROMA_DIR), str(backup))
        print(f"🗄️ 기존 스냅샷을 백업했습니다: {backup}")

    documents: list[Document] = []
    for page, issues in fetch_issue_pages(url, headers, max_pages=max_pages):
        documents.extend(build_issue_document(issue) for issue in issues)
        print(f"✅ {len(documents)}개 이슈 누적 추출 (현재 {page}페이지)")

    print(f"🎉 총 {len(documents)}개 이슈 파싱 완료. 임베딩을 시작합니다...")
    embeddings = OllamaEmbeddings(model=EMBEDDING_MODEL)
    CHROMA_DIR.parent.mkdir(parents=True, exist_ok=True)
    vector_db = Chroma(persist_directory=str(CHROMA_DIR), embedding_function=embeddings)
    add_documents_batched(vector_db, documents)
    print("🏆 전체 재구축이 완료되었습니다.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--full", action="store_true", help="전체 재구축(기존 DB 백업 후 새로 생성)")
    parser.add_argument(
        "--refresh-days",
        type=int,
        default=0,
        help="증분 모드에서 최근 N일 내 수정된 기존 이슈를 재임베딩(상태 변경 반영)",
    )
    parser.add_argument("--max-pages", type=int, default=200, help="Mantis 조회 최대 페이지 수")
    args = parser.parse_args()

    load_dotenv()
    url, headers = get_mantis_settings()

    if args.full:
        run_full(url, headers, args.max_pages)
    else:
        run_incremental(url, headers, args.refresh_days, args.max_pages)


if __name__ == "__main__":
    main()
