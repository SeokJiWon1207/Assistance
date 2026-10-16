"""긴 원문 검색을 집계용 저장본과 분리하는 회귀 테스트."""
import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import requests


def raw_issue(issue_id=1, title="접속 장애", body="", notes=None, project="테스트증권"):
    return {"id": issue_id, "summary": title, "description": body,
            "project": {"name": project}, "handler": {"real_name": "김개발"},
            "status": {"name": "assigned", "label": "할당"},
            "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-15T00:00:00Z",
            "notes": notes or [], "custom_fields": [{"field": {"name": "매체구분"}, "value": "MTS"}]}


def source_database(path, issues, generation="2026-10-15T00:00:00Z", identity="test"):
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("DROP TABLE IF EXISTS metadata; DROP TABLE IF EXISTS issues; "
                         "CREATE TABLE metadata(value TEXT); CREATE TABLE issues(id INTEGER PRIMARY KEY, payload TEXT);")
        db.execute("INSERT INTO metadata VALUES (?)", [json.dumps(
            {"connection": identity, "fetchedAt": generation, "count": len(issues)})])
        db.executemany("INSERT INTO issues VALUES (?, ?)", [(r['id'], json.dumps(r, ensure_ascii=False)) for r in issues])


class ChunkIndexTests(unittest.TestCase):
    def test_long_body_and_last_note_keep_text_offsets_and_bounded_chunks(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.sources.mantis_chunks"), "구간 분할 구현 필요")
        from backend.sources.mantis_chunks import split_issue
        body = "접속 요청 내용을 확인했습니다. " * 240 + "인증서 만료 오류"
        note = "로그 확인 " * 450 + "인증서를 갱신하여 해결했습니다."
        chunks = list(split_issue(raw_issue(body=body, notes=[{"id": 99, "text": note}])))
        self.assertGreater(len(chunks), 4)
        for field, original in [("description", body), ("note:99", note)]:
            selected = [c for c in chunks if c["field"] == field]
            self.assertEqual(selected[0]["start"], 0)
            self.assertEqual(selected[-1]["end"], len(original))
            for chunk in selected:
                self.assertEqual(chunk["text"], original[chunk["start"]:chunk["end"]])
                self.assertLessEqual(len(chunk["text"]), 1200)
            for previous, current in zip(selected, selected[1:], strict=False):
                self.assertLess(current["start"], previous["end"])
        self.assertTrue(any("인증서를 갱신하여 해결" in c["text"] for c in chunks))

    def test_index_finds_late_note_and_preserves_source_database(self):
        from backend.sources.mantis_chunk_index import build_index, open_index, lexical_chunks
        with tempfile.TemporaryDirectory() as directory:
            source, index = Path(directory) / "raw.sqlite3", Path(directory) / "chunks.sqlite3"
            source_database(source, [raw_issue(notes=[{"id": 9, "text": "진행 기록 " * 700 + "인증서 갱신으로 해결"}]),
                                     raw_issue(2, "일반 문의", "사용 안내")])
            before = source.read_bytes()
            stats = build_index(source, index, "test")
            self.assertEqual(stats["issues"], 2)
            self.assertGreater(stats["chunks"], 2)
            with open_index(source, index, "test") as opened:
                self.assertIsNotNone(opened)
                db, _raw_db, _meta = opened
                hits = list(lexical_chunks(db, "인증서 갱신"))
            self.assertEqual(hits[0]["issue_id"], 1)
            self.assertEqual(hits[0]["field"], "note:9")
            self.assertIn("인증서 갱신", hits[0]["text"])
            self.assertEqual(source.read_bytes(), before)

    def test_changed_generation_or_connection_cannot_reuse_old_chunks(self):
        from backend.sources.mantis_chunk_index import build_index, open_index
        with tempfile.TemporaryDirectory() as directory:
            source, index = Path(directory) / "raw.sqlite3", Path(directory) / "chunks.sqlite3"
            source_database(source, [raw_issue()])
            build_index(source, index, "test")
            with open_index(source, index, "another-connection") as opened:
                self.assertIsNone(opened)
            source_database(source, [raw_issue()], generation="2026-10-15T01:00:00Z")
            with open_index(source, index, "test") as opened:
                self.assertIsNone(opened)

    def test_failed_rebuild_keeps_previous_index(self):
        from backend.sources.mantis_chunk_index import build_index
        with tempfile.TemporaryDirectory() as directory:
            source, index = Path(directory) / "raw.sqlite3", Path(directory) / "chunks.sqlite3"
            source_database(source, [raw_issue()])
            build_index(source, index, "test")
            before = index.read_bytes()
            with closing(sqlite3.connect(source)) as db, db:
                db.execute("UPDATE issues SET payload='invalid json'")
            with self.assertRaises(ValueError):
                build_index(source, index, "test")
            self.assertEqual(index.read_bytes(), before)

    def test_korean_particles_codes_empty_and_fts_syntax_are_safe(self):
        from backend.sources.mantis_chunk_index import build_index, open_index, lexical_chunks
        with tempfile.TemporaryDirectory() as directory:
            source, index = Path(directory) / "raw.sqlite3", Path(directory) / "chunks.sqlite3"
            source_database(source, [raw_issue(body="인증서를 갱신했습니다. ERR_CERT_DATE_INVALID 오류")])
            build_index(source, index, "test")
            with open_index(source, index, "test") as opened:
                db, _, _ = opened
                for query in ['인증서', 'ERR_CERT_DATE_INVALID', '"인증서" OR * (']:
                    self.assertEqual(next(iter(lexical_chunks(db, query)))["issue_id"], 1)
                self.assertEqual(list(lexical_chunks(db, "")), [])

    def test_incremental_rebuild_updates_and_deletes_without_reindexing_unchanged_issue(self):
        from backend.sources.mantis_chunk_index import build_index, open_index, lexical_chunks
        with tempfile.TemporaryDirectory() as directory:
            source, index = Path(directory) / "raw.sqlite3", Path(directory) / "chunks.sqlite3"
            source_database(source, [raw_issue(1, body="보존 내용"), raw_issue(2, body="삭제 내용"), raw_issue(3, body="이전 내용")])
            build_index(source, index, "test")
            source_database(source, [raw_issue(1, body="보존 내용"), raw_issue(3, body="새로운 인증서")],
                            generation="2026-10-15T01:00:00Z")
            stats = build_index(source, index, "test")
            self.assertEqual(stats["reindexed_issues"], 1)
            with open_index(source, index, "test") as opened:
                db, _, _ = opened
                self.assertEqual(db.execute("SELECT DISTINCT issue_id FROM chunks ORDER BY issue_id").fetchall()[0][0], 1)
                self.assertEqual({row["issue_id"] for row in lexical_chunks(db, "인증서")}, {3})
                self.assertEqual(list(lexical_chunks(db, "삭제")), [])


class HybridSearchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "raw.sqlite3"
        self.index = Path(self.directory.name) / "chunks.sqlite3"

    def build(self, issues):
        from backend.sources.mantis_chunk_index import build_index
        source_database(self.source, issues)
        build_index(self.source, self.index, "test")

    def search(self, query, constraints=None, vectors=None, embed=None, limit=5):
        from backend.sources.mantis_hybrid import search_hybrid
        return search_hybrid(query, limit, constraints or {}, lambda *_: (vectors or [], {"ranking": "semantic_vector"}),
                             source=self.source, target=self.index, identity="test", embed=embed)

    def test_explanation_recalls_dotted_identifier_written_as_separate_words(self):
        generic = [raw_issue(i, "[MTS] 차트 라이브러리 Crash 세 건",
                             "NH 차트 라이브러리 Crash 세 건을 수정했습니다. "
                             "뒤에 나온 추가 요청은 어떻게 답했는지 기록합니다.")
                   for i in range(1, 41)]
        target = raw_issue(100, "[MTS] Crash 발생원인 오류 수정 요청",
                           notes=[{"id": 94278, "text": "COMUtil 클래스 내 makeZero 추가 요청을 받았습니다."}])
        self.build([*generic, target])
        query = ("NH MTS에서 차트 라이브러리 Crash 세 건을 고쳤다는 기록을 찾고 있어. "
                 "뒤에 나온 COMUtil.makeZero 추가 요청은 어떻게 답했는지 설명해줘.")
        docs, _ = self.search(query, {"answer_query": query},
                              vectors=[{"issue_id": i} for i in range(1, 31)],
                              embed=lambda texts: [[1.0, 0.0] for _ in texts], limit=3)
        self.assertIn(100, [doc["issue_id"] for doc in docs[:3]])
        self.assertIn("makeZero", next(doc for doc in docs if doc["issue_id"] == 100)["chunk_evidence"]["text"])
        ordinary, _ = self.search(query, vectors=[{"issue_id": i} for i in range(1, 31)],
                                  embed=lambda texts: [[1.0, 0.0] for _ in texts], limit=3)
        self.assertNotIn(100, [doc["issue_id"] for doc in ordinary[:3]])

    def test_explanation_keeps_lexical_leader_in_last_visible_slot(self):
        self.build([raw_issue(1, "GetCodeInfo", "정확한 덤프 위치"),
                    *[raw_issue(i, "일반 종료", "화면 종료") for i in range(2, 8)]])
        vectors = [{"issue_id": i} for i in range(2, 8)]

        def embed(texts):
            return [[1.0, 0.0] if i == 0 or "GetCodeInfo" not in text else [0.0, 1.0]
                    for i, text in enumerate(texts)]

        for limit, expected in [(5, [2, 3, 4, 5, 1]), (1, [1])]:
            with self.subTest(limit=limit):
                docs, _ = self.search("GetCodeInfo", {"answer_query": "GetCodeInfo 종료 원인"},
                                      vectors=vectors, embed=embed, limit=limit)
                self.assertEqual([d["issue_id"] for d in docs[:limit]], expected)
                self.assertEqual(len({d["issue_id"] for d in docs}), len(docs))
        ordinary, _ = self.search("GetCodeInfo", vectors=vectors, embed=embed)
        self.assertEqual([d["issue_id"] for d in ordinary[:5]], [2, 3, 4, 5, 6])

    def test_explanation_keeps_lexical_leader_even_outside_fusion_window(self):
        self.build([raw_issue(1, "GetCodeInfo", "공통"),
                    *[raw_issue(i, "공통") for i in range(2, 31)], raw_issue(32, "별도 검색")])
        docs, _ = self.search("GetCodeInfo 공통", {"answer_query": "GetCodeInfo 공통"},
                              vectors=[{"issue_id": i} for i in [32, *range(2, 31)]],
                              embed=lambda texts: [[1.0, 0.0] for _ in texts])
        self.assertEqual(docs[4]["issue_id"], 1)
        self.assertEqual(len(docs), 30)
        self.assertEqual(docs[4]["fusion_rank"], 31)

    def test_explanation_does_not_restore_lexical_leader_removed_by_focus(self):
        self.build([raw_issue(1, "GetCodeInfo"), raw_issue(2, "메모리 개선")])
        docs, _ = self.search("GetCodeInfo", {"answer_query": "GetCodeInfo 메모리", "semantic_focus_terms": ["메모리"]},
                              vectors=[{"issue_id": 2}], embed=lambda texts: [[1.0, 0.0] for _ in texts])
        self.assertEqual([d["issue_id"] for d in docs], [2])

    def test_lexical_tail_and_vector_candidate_are_fused_and_deduplicated(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.sources.mantis_hybrid"), "결합 검색 구현 필요")
        self.build([raw_issue(1, notes=[{"id": 99, "text": "진행 기록 " * 700 + "인증서 갱신으로 해결"}]),
                    raw_issue(2, "보안 연결 복구", "유효기간이 지난 키를 교체")])
        docs, meta = self.search("인증서 갱신", vectors=[{"issue_id": 2}],
                                 embed=lambda texts: [[1.0, 0.0] for _ in texts])
        self.assertEqual({d["issue_id"] for d in docs}, {1, 2})
        self.assertEqual(len(docs), 2)
        self.assertTrue(any("인증서 갱신" in d["chunk_evidence"]["text"] for d in docs))
        self.assertEqual(meta["ranking"], "hybrid_chunk_rrf")
        self.assertEqual(meta["reranking"], "chunk_embedding_cosine")

    def test_filters_apply_before_candidate_cutoff(self):
        others = [raw_issue(i, "인증서 갱신", "인증서 갱신", project="다른증권") for i in range(1, 101)]
        self.build([*others, raw_issue(101, "접속 복구", "인증서 갱신으로 해결")])
        docs, _ = self.search("인증서 갱신", {"customer_exact": "테스트증권", "platform_aliases": ["MTS"]},
                              embed=lambda texts: [[1.0, 0.0] for _ in texts])
        self.assertEqual([d["issue_id"] for d in docs], [101])

    def test_dense_reranking_promotes_relevant_chunk_over_vector_order(self):
        self.build([raw_issue(1, "문제", "무관한 안내"), raw_issue(2, "문제", "세션 토큰의 기한을 연장")])
        def embed(texts):
            return [[1.0, 0.0] if i == 0 or "세션 토큰" in text else [0.0, 1.0]
                    for i, text in enumerate(texts)]
        docs, meta = self.search("로그인이 자꾸 풀림", vectors=[{"issue_id": 1}, {"issue_id": 2}], embed=embed)
        self.assertEqual([d["issue_id"] for d in docs], [2, 1])
        self.assertEqual(meta["reranking"], "chunk_embedding_cosine")

    def test_lexical_candidates_cannot_reintroduce_an_excluded_followup_topic(self):
        self.build([raw_issue(1, "일반 개선"), raw_issue(2, "메모리 개선")])
        docs, meta = self.search("개선", {"semantic_focus_terms": ["메모리", "개선"]}, vectors=[{"issue_id": 2}],
                                 embed=lambda texts: [[1.0, 0.0] for _ in texts])
        self.assertEqual([d["issue_id"] for d in docs], [2])
        self.assertEqual(meta["semantic_focus_term"], "메모리")

    def test_late_note_is_used_by_deterministic_resolution_report(self):
        from backend.reports.similar import _summarize_resolution
        self.build([raw_issue(1, notes=[{"id": 99, "text": "단순 기록\n" * 800 + "인증서 갱신으로 해결했습니다."}])])
        docs, _ = self.search("인증서 갱신", embed=lambda texts: [[1.0, 0.0] for _ in texts])
        self.assertIn("인증서 갱신", _summarize_resolution(docs[0]["snapshot_issue"]))

    def test_embedding_failure_or_invalid_vectors_preserve_keyword_results(self):
        self.build([raw_issue(1, body="인증서 갱신")])
        for result in (requests.Timeout(), [[float("nan"), 0.0]], [[0.0, 0.0], [1.0]], [[0, 0], [0, 0]]):
            def embed(_texts, result=result):
                if isinstance(result, Exception):
                    raise result
                return result
            docs, meta = self.search("인증서 갱신", embed=embed)
            self.assertEqual([d["issue_id"] for d in docs], [1])
            self.assertEqual(meta["reranking"], "rrf_fallback")


    def test_semantic_pipeline_keeps_late_evidence_when_live_api_is_down(self):
        from backend.sources import chroma, mantis_hybrid
        self.build([raw_issue(1, notes=[{"id": 8, "text": "확인 중 " * 800 + "인증서 갱신으로 해결"}])])
        with (
            patch.object(mantis_hybrid, "store_path", return_value=self.source),
            patch.object(mantis_hybrid, "INDEX_PATH", self.index),
            patch.object(mantis_hybrid, "connection_key", return_value="test"),
            patch.object(mantis_hybrid, "embed_texts", side_effect=requests.Timeout()),
            patch.object(chroma, "CHROMA_DB_PATH", self.source),
            patch.object(chroma, "_rank_legacy_documents_semantically", return_value=([], {})),
            patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=requests.Timeout()),
        ):
            result = chroma.execute_chroma_search("인증서 갱신", 5, {}, semantic=True)
        self.assertEqual([d["issue_id"] for d in result["documents"]], [1])
        self.assertFalse(result["meta"]["search_complete"])
        self.assertEqual(result["meta"]["coverage"], "top_k")
        evidence = result["documents"][0]["latest_issue"]["matching_evidence"]
        self.assertTrue(any("인증서 갱신" in item["text"] for item in evidence))
        self.assertEqual(result["documents"][0]["latest_source"], "mantis_snapshot")

    def test_live_changed_scope_and_deleted_issue_are_not_revived_from_chunks(self):
        from backend.sources import chroma, mantis_hybrid
        self.build([raw_issue(1, body="인증서 갱신"), raw_issue(2, body="인증서 갱신")])
        with (
            patch.object(mantis_hybrid, "store_path", return_value=self.source),
            patch.object(mantis_hybrid, "INDEX_PATH", self.index),
            patch.object(mantis_hybrid, "connection_key", return_value="test"),
            patch.object(mantis_hybrid, "embed_texts", side_effect=requests.Timeout()),
            patch.object(chroma, "CHROMA_DB_PATH", self.source),
            patch.object(chroma, "_rank_legacy_documents_semantically", return_value=([], {})),
            patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=lambda i: raw_issue(i, project="다른증권") if i == 1 else None),
        ):
            result = chroma.execute_chroma_search("인증서 갱신", 5, {"customer_exact": "테스트증권"}, semantic=True)
        self.assertEqual(result["documents"], [])

    def test_changed_live_text_drops_old_excerpt(self):
        from backend.sources.mantis_hybrid import attach_chunk_evidence
        normalized = {}
        attach_chunk_evidence(normalized, raw_issue(body="다른 조치로 해결"),
                              {"field": "description", "start": 0, "end": 6, "text": "인증서 갱신"})
        self.assertNotIn("matching_evidence", normalized)

    def test_cancelled_reranking_does_not_return_a_successful_fallback(self):
        from backend.execution import RequestStopped
        self.build([raw_issue(1, body="인증서 갱신")])
        def embed(_texts):
            raise RequestStopped(cancelled=True)
        with self.assertRaises(RequestStopped):
            self.search("인증서 갱신", embed=embed)

    def test_missing_or_obsolete_index_uses_legacy_path(self):
        self.build([raw_issue(1, body="인증서 갱신")])
        source_database(self.source, [raw_issue(2)], generation="2026-10-15T01:00:00Z")
        with patch("backend.sources.mantis_hybrid.start_index_build"):
            self.assertIsNone(self.search("인증서 갱신"))

    def test_explicit_exclusions_and_late_note_filters_use_raw_fields(self):
        issue = raw_issue(1, body="HTS라는 본문 언급", notes=[{"id": 4, "text": "오래된 기록 " * 800 + "인증서 갱신"}])
        self.build([issue])
        with patch("backend.sources.mantis_hybrid.embed_texts", side_effect=requests.Timeout()):
            docs, _ = self.search("인증서", {"platform_aliases": ["HTS"]})
            self.assertEqual(docs, [])
            docs, _ = self.search("인증서", {"exclude_issue_ids": [1]})
            self.assertEqual(docs, [])
            docs, _ = self.search("인증서", {"filter_clauses": [
                {"field": "keyword", "operator": "contains", "value": "인증서 갱신"}]})
            self.assertEqual([d["issue_id"] for d in docs], [1])
