import importlib.util
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "make_vector_db.py"
SPEC = importlib.util.spec_from_file_location("make_vector_db", SCRIPT_PATH)
make_vector_db = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(make_vector_db)


class SnapshotRebuildTests(unittest.TestCase):
    def test_reuse_comparison_ignores_only_unordered_custom_field_sections(self):
        original = """[Mantis 이슈 정보]
- 이슈 ID: #1

[일정/전달일]
- 버전반영일: 미상
- 고객사요청일: 2026-08-10

[공수]
- QC 공수: 1
- 개발 공수: 2

[제목]
동일한 제목"""
        reordered = """[Mantis 이슈 정보]
- 이슈 ID: #1

[일정/전달일]
- 고객사요청일: 2026-08-10
- 버전반영일: 미상

[공수]
- 개발 공수: 2
- QC 공수: 1

[제목]
동일한 제목"""
        changed = reordered.replace("동일한 제목", "변경된 제목")

        self.assertEqual(
            make_vector_db.canonical_document_for_reuse(original),
            make_vector_db.canonical_document_for_reuse(reordered),
        )
        self.assertNotEqual(
            make_vector_db.canonical_document_for_reuse(original),
            make_vector_db.canonical_document_for_reuse(changed),
        )

    def test_validation_rejects_partial_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            snapshot_dir = Path(temporary_directory)
            with closing(sqlite3.connect(snapshot_dir / "chroma.sqlite3")) as connection, connection:
                connection.execute(
                    "CREATE TABLE embedding_metadata (id INTEGER, key TEXT, string_value TEXT)"
                )
                connection.execute(
                    "INSERT INTO embedding_metadata VALUES (1, 'chroma:document', 'one')"
                )

            with self.assertRaisesRegex(make_vector_db.SnapshotBuildError, "건수 불일치"):
                make_vector_db.validate_snapshot(snapshot_dir, expected_documents=2)

    def test_failed_full_fetch_keeps_existing_snapshot_in_place(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            current = root / "mantis_chroma_db"
            current.mkdir()
            marker = current / "existing.marker"
            marker.write_text("keep", encoding="utf-8")

            def fail_fetch(*_args, **_kwargs):
                raise make_vector_db.SnapshotBuildError("network failed")
                yield

            with patch.object(make_vector_db, "CHROMA_DIR", current), patch.object(
                make_vector_db, "BACKUP_DIR", root / "backups"
            ), patch.object(make_vector_db, "fetch_issue_pages", fail_fetch):
                with self.assertRaisesRegex(make_vector_db.SnapshotBuildError, "network failed"):
                    make_vector_db.run_full("https://mantis.example", {}, 10)

            self.assertEqual(marker.read_text(encoding="utf-8"), "keep")
            self.assertFalse((root / "backups").exists())

    def test_valid_full_build_replaces_snapshot_only_after_validation(self):
        class FakeChroma:
            def __init__(self, persist_directory, embedding_function=None):
                del embedding_function
                self.database_path = Path(persist_directory) / "chroma.sqlite3"
                with closing(sqlite3.connect(self.database_path)) as connection, connection:
                    connection.execute(
                        "CREATE TABLE embedding_metadata "
                        "(id INTEGER, key TEXT, string_value TEXT)"
                    )

            def add_documents(self, documents):
                with closing(sqlite3.connect(self.database_path)) as connection, connection:
                    for index, document in enumerate(documents, start=1):
                        connection.execute(
                            "INSERT INTO embedding_metadata VALUES (?, 'chroma:document', ?)",
                            (index, document.page_content),
                        )

        issue = {
            "id": 1,
            "project": {"name": "테스트증권"},
            "status": {"label": "할당"},
            "handler": {"real_name": "담당자"},
            "reporter": {"real_name": "요청자"},
            "summary": "테스트 이슈",
            "created_at": "2026-08-10",
            "updated_at": "2026-08-10",
            "custom_fields": [],
        }
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            current = root / "mantis_chroma_db"
            current.mkdir()
            (current / "existing.marker").write_text("old", encoding="utf-8")
            backups = root / "backups"
            with patch.object(make_vector_db, "CHROMA_DIR", current), patch.object(
                make_vector_db, "BACKUP_DIR", backups
            ), patch.object(
                make_vector_db, "fetch_issue_pages", return_value=iter([(1, [issue])])
            ), patch.object(make_vector_db, "Chroma", FakeChroma):
                make_vector_db.run_full("https://mantis.example", {}, 10)

            make_vector_db.validate_snapshot(current, expected_documents=1)
            self.assertTrue((current / "mantis_index.sqlite3").exists())
            freshness = (current / "freshness.json").read_text(encoding="utf-8")
            self.assertIn('\"documentCount\": 1', freshness)
            self.assertFalse((current / "existing.marker").exists())
            backup_markers = list(backups.glob("*/existing.marker"))
            self.assertEqual(len(backup_markers), 1)
            self.assertEqual(backup_markers[0].read_text(encoding="utf-8"), "old")


if __name__ == "__main__":
    unittest.main()
