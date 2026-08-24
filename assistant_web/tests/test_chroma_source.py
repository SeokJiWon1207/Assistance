import sqlite3
import sys
import tempfile
import types
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from backend.sources import chroma


def issue_document(issue_id: int, title: str) -> str:
    return f"""[Mantis 이슈 정보]
- 이슈 ID: #{issue_id}
- 프로젝트(고객사): 테스트증권

[제목]
{title}

[고객사 본문 요청 내용]
테스트 본문
"""


def create_chroma_database(path: Path, documents: list[str]) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE embedding_metadata (id INTEGER, key TEXT, string_value TEXT, int_value INTEGER)"
        )
        connection.executemany(
            "INSERT INTO embedding_metadata (id, key, string_value) VALUES (?, 'chroma:document', ?)",
            [(index, document) for index, document in enumerate(documents, start=1)],
        )


class ChromaSourceTests(unittest.TestCase):
    def test_semantic_vector_search_returns_ranked_documents(self):
        collection = Mock()
        collection.count.return_value = 2
        collection.query.return_value = {
            "documents": [[issue_document(202, "메모리 개선"), issue_document(101, "일반 개선")]],
            "distances": [[0.1, 0.4]],
        }
        client = Mock()
        client.get_collection.return_value = collection
        chromadb_module = types.SimpleNamespace(PersistentClient=Mock(return_value=client))
        response = Mock()
        response.json.return_value = {"embedding": [0.1, 0.2]}

        with (
            patch.dict(sys.modules, {"chromadb": chromadb_module}),
            patch.object(chroma.requests, "post", return_value=response) as post,
            patch.object(chroma, "chroma_text_matches_constraints", return_value=True),
        ):
            documents, meta = chroma.rank_chroma_documents_semantically("메모리 개선", 2, {})

        self.assertEqual([document["issue_id"] for document in documents], [202, 101])
        self.assertEqual(meta["ranking"], "semantic_vector")
        self.assertEqual(meta["candidate_count"], 2)
        post.assert_called_once()
        collection.query.assert_called_once_with(
            query_embeddings=[[0.1, 0.2]],
            n_results=2,
            include=["documents", "distances"],
        )

    def test_semantic_search_falls_back_to_lexical_ranking_on_embedding_timeout(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "chroma.sqlite3"
            create_chroma_database(
                database_path,
                [
                    issue_document(301, "메모리 사용량 개선"),
                    issue_document(302, "로그인 오류"),
                ],
            )
            with (
                patch.object(chroma, "CHROMA_DB_PATH", database_path),
                patch.object(chroma.requests, "post", side_effect=requests.Timeout("embedding timeout")),
                patch.object(chroma, "chroma_text_matches_constraints", return_value=True),
            ):
                documents, meta = chroma.rank_chroma_documents_semantically("메모리 개선", 5, {})

        self.assertEqual([document["issue_id"] for document in documents], [301])
        self.assertEqual(meta["ranking"], "semantic_lexical_fallback")
        self.assertIn("Timeout", meta["ranking_fallback_reason"])

    def test_execute_search_revalidates_live_issues_and_keeps_snapshot_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "chroma.sqlite3"
            create_chroma_database(
                database_path,
                [issue_document(101, "테스트 검색"), issue_document(202, "테스트 검색")],
            )

            def fetch_live(issue_id: int):
                return {"id": issue_id} if issue_id == 202 else None

            with (
                patch.object(chroma, "CHROMA_DB_PATH", database_path),
                patch.object(chroma, "build_keyword_terms", return_value=["테스트"]),
                patch.object(chroma, "chroma_text_matches_constraints", return_value=True),
                patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=fetch_live),
                patch.object(chroma, "issue_matches_constraints", return_value=False),
            ):
                result = chroma.execute_chroma_search(
                    "테스트 검색",
                    limit=2,
                    constraints={
                        "customer_key": "테스트증권",
                        "customer_aliases": ["테스트증권"],
                    },
                )

        self.assertEqual(result["status"], "ok")
        self.assertEqual([document["issue_id"] for document in result["documents"]], [101])
        self.assertEqual(result["documents"][0]["latest_source"], "chroma_snapshot")
        self.assertEqual(result["meta"]["mantis_refreshed_count"], 1)
        self.assertEqual(result["meta"]["skipped_after_refresh"], 1)

    def test_execute_search_reports_missing_and_corrupt_databases(self):
        with tempfile.TemporaryDirectory() as directory:
            missing_path = Path(directory) / "missing.sqlite3"
            with patch.object(chroma, "CHROMA_DB_PATH", missing_path):
                missing = chroma.execute_chroma_search("테스트")

            corrupt_path = Path(directory) / "corrupt.sqlite3"
            corrupt_path.write_text("not sqlite", encoding="utf-8")
            with patch.object(chroma, "CHROMA_DB_PATH", corrupt_path):
                corrupt = chroma.execute_chroma_search("테스트")

        self.assertEqual(missing["status"], "skipped")
        self.assertEqual(corrupt["status"], "error")
        self.assertIn("Chroma DB 조회 실패", corrupt["reason"])

    def test_get_document_joins_id_metadata_and_handles_query_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "chroma.sqlite3"
            create_chroma_database(database_path, [issue_document(777, "상세 조회")])
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.execute(
                    "INSERT INTO embedding_metadata (id, key, int_value) VALUES (1, 'id', 777)"
                )
            with patch.object(chroma, "CHROMA_DB_PATH", database_path):
                found = chroma.get_chroma_document(777)

            corrupt_path = Path(directory) / "corrupt.sqlite3"
            corrupt_path.write_text("not sqlite", encoding="utf-8")
            with patch.object(chroma, "CHROMA_DB_PATH", corrupt_path):
                corrupt = chroma.get_chroma_document(777)

        self.assertEqual(found["issue_id"], 777)
        self.assertIn("상세 조회", found["content"])
        self.assertIsNone(corrupt)


if __name__ == "__main__":
    unittest.main()
