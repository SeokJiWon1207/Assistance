import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend.common import normalize_chroma_issue_document
from backend.tools.mantis_index import (
    INDEX_FILENAME,
    MantisIndexError,
    build_mantis_index,
    parse_index_document,
    validate_mantis_index,
)
from backend.tools.mantis_snapshot import scan_mantis_documents
from backend.understanding import chroma_text_matches_constraints


def issue_document(
    issue_id: int,
    *,
    customer: str = "132SHGM(신한굿모닝증권)",
    created_at: str = "2026-08-10",
    status: str = "할당",
    handler: str = "홍길동",
    platform: str = "MTS",
) -> str:
    return f"""[Mantis 이슈 정보]
- 이슈 ID: #{issue_id}
- 프로젝트(고객사): {customer}
- 매체구분: {platform}
- 화면번호: 9413
- 작업주체: 개발
- 현재 상태: {status}
- 담당자: {handler}
- 요청자: 김요청
- 등록일: {created_at}
- 최종수정일: {created_at}

[일정/전달일]
- 고객사요청일: {created_at}
- 고객사전달 목표일: 미상
- 고객사전달일: 미상
- 개발완료 목표일: 미상
- QC 완료 목표일: 미상
- 버전반영일: 미상

[공수]
- 기록 없음

[제목]
[9413] 테스트 이슈 {issue_id}

[고객사 본문 요청 내용]
테스트 본문
"""


def create_chroma_database(path: Path, documents: list[str]) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE embedding_metadata (id INTEGER, key TEXT, string_value TEXT)"
        )
        connection.executemany(
            "INSERT INTO embedding_metadata VALUES (?, 'chroma:document', ?)",
            [(index, document) for index, document in enumerate(documents, start=1)],
        )


class MantisIndexTests(unittest.TestCase):
    def test_builds_and_validates_structured_fields_and_hashes(self):
        documents = [issue_document(1), issue_document(2, customer="KB증권")]
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / INDEX_FILENAME
            stats = build_mantis_index(index_path, documents)
            metadata = validate_mantis_index(index_path, expected_documents=2)
            with closing(sqlite3.connect(index_path)) as connection:
                row = connection.execute(
                    "SELECT customer, platform, screen_number FROM issues WHERE issue_id = 1"
                ).fetchone()

        self.assertEqual(stats["document_count"], 2)
        # v2에서 이슈 유형(severity) 컬럼이 추가되었다.
        self.assertEqual(metadata["schema_version"], "2")
        self.assertEqual(row, ("132SHGM(신한굿모닝증권)", "MTS", "9413"))

    def test_parser_rejects_document_without_issue_id(self):
        with self.assertRaisesRegex(MantisIndexError, "이슈 ID"):
            parse_index_document("[제목]\n식별자 없음")

    def test_primary_index_matches_legacy_filter_and_group_results(self):
        documents = [
            issue_document(1, created_at="2025-12-10"),
            issue_document(2, created_at="2026-01-10"),
            issue_document(3, customer="KB증권", created_at="2025-12-20"),
        ]
        constraints = {
            "customer_aliases": ["신한"],
            "created_at": {"from": "2025-01-01", "to": "2025-12-31"},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chroma_path = root / "chroma.sqlite3"
            index_path = root / INDEX_FILENAME
            create_chroma_database(chroma_path, documents)
            build_mantis_index(index_path, documents)
            arguments = {
                "database_path": chroma_path,
                "matches": lambda content: chroma_text_matches_constraints(content, constraints),
                "normalize": normalize_chroma_issue_document,
                "group_key": lambda issue: str(issue.get("project") or ""),
                "constraints": constraints,
            }
            with patch.dict(os.environ, {"MANTIS_INDEX_MODE": "off"}):
                legacy = scan_mantis_documents(**arguments)
            with patch.dict(os.environ, {"MANTIS_INDEX_MODE": "primary"}):
                structured = scan_mantis_documents(**arguments)

        self.assertEqual(structured, legacy)
        self.assertEqual(structured["matched_ids"], {1})

    def test_primary_mode_falls_back_when_index_is_corrupt(self):
        documents = [issue_document(1)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chroma_path = root / "chroma.sqlite3"
            index_path = root / INDEX_FILENAME
            create_chroma_database(chroma_path, documents)
            index_path.write_bytes(b"not a sqlite database")
            with patch.dict(os.environ, {"MANTIS_INDEX_MODE": "primary"}):
                result = scan_mantis_documents(
                    chroma_path,
                    lambda _content: True,
                    normalize_chroma_issue_document,
                )

        self.assertTrue(result["available"])
        self.assertEqual(result["matched_ids"], {1})

    def test_shadow_mode_returns_legacy_result_when_results_differ(self):
        legacy_documents = [issue_document(1), issue_document(2)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            chroma_path = root / "chroma.sqlite3"
            create_chroma_database(chroma_path, legacy_documents)
            build_mantis_index(root / INDEX_FILENAME, [issue_document(1)])
            with patch.dict(os.environ, {"MANTIS_INDEX_MODE": "shadow"}), self.assertLogs(
                "backend.tools.mantis_snapshot", level="WARNING"
            ) as logs:
                result = scan_mantis_documents(
                    chroma_path,
                    lambda _content: True,
                    normalize_chroma_issue_document,
                )

        self.assertEqual(result["matched_ids"], {1, 2})
        self.assertIn("shadow 결과 불일치", "\n".join(logs.output))

    def test_explain_query_plan_uses_created_at_index(self):
        with tempfile.TemporaryDirectory() as directory:
            index_path = Path(directory) / INDEX_FILENAME
            build_mantis_index(index_path, [issue_document(1)])
            with closing(sqlite3.connect(index_path)) as connection:
                plan = " ".join(
                    str(row[3])
                    for row in connection.execute(
                        "EXPLAIN QUERY PLAN SELECT issue_id FROM issues "
                        "WHERE created_at BETWEEN ? AND ?",
                        ("2026-01-01", "2026-12-31"),
                    )
                )

        self.assertIn("idx_issues_created_at", plan)


if __name__ == "__main__":
    unittest.main()
