import json
import sqlite3
import tempfile
import unittest
from collections import Counter
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend import common, retrieval
from backend.intent.entities import _distinct_chroma_header_values
from backend.sources import chroma, mantis, mantis_store
from backend.sources.mantis_collection import IssueBatch


QUESTION = "HTS에서 VB스크립트를 자바스크립트로 교체 문의 들어온거 있어?"
TOPIC = "VB스크립트 자바스크립트 교체"
HTS = {"platform_key": "hts", "platform_aliases": ["HTS"],
       "filter_clauses": [{"field": "platform", "operator": "equals", "value": "hts"}]}


def issue(number, text):
    return {"id": number, "summary": "스크립트 문의", "description": text,
            "project": {"name": "112KBStar(KB증권)"}, "status": {"name": "confirmed"}, "handler": {"real_name": "석지원"},
            "created_at": "2025-01-01", "custom_fields": [], "notes": []}


class SearchLatencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.raw = self.root / "raw.sqlite3"
        self.index = self.root / "chroma.sqlite3"
        with sqlite3.connect(self.index) as db:
            db.execute("CREATE TABLE embedding_metadata(key TEXT, string_value TEXT)")
            db.execute("INSERT INTO embedding_metadata VALUES ('chroma:document', ?)", (
                "[Mantis 이슈 정보]\n- 이슈 ID: #99\n- 프로젝트(고객사): 112KBStar(KB증권)\n"
                "- 매체구분: HTS\n- 담당자: 석지원\n- 현재 상태: confirmed\n- 등록일: 2025-01-01\n\n"
                "[제목]\nVB스크립트 자바스크립트 교체\n\n[고객사 본문 요청 내용]\nHTS 교체",))
        for target, name, value in ((mantis_store, "store_path", self.raw),
                                     (mantis_store, "connection_key", "test"),
                                     (chroma, "CHROMA_DB_PATH", self.index)):
            mock = patch.object(target, name, return_value=value) if callable(getattr(target, name)) else patch.object(target, name, value)
            mock.start()
            self.addCleanup(mock.stop)

    def write_store(self, rows):
        with sqlite3.connect(self.raw) as db:
            db.execute("CREATE TABLE issues(id INTEGER PRIMARY KEY,payload TEXT)")
            db.execute("CREATE TABLE metadata(value TEXT)")
            db.execute("INSERT INTO metadata VALUES (?)", (json.dumps({
                "connection": "test", "count": len(rows), "fetchedAt": datetime.now(timezone.utc).isoformat(),
            }),))
            db.executemany("INSERT INTO issues VALUES (?,?)", [(row["id"], json.dumps(row)) for row in rows])

    def pipeline(self):
        with patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=HTTPException(status_code=503, detail="오프라인 검증")):
            return retrieval.run_retrieval_pipeline(QUESTION, 5, {"mantis": True, "vectorDb": True}, {"source": "mantis"})

    def test_each_body_is_built_once_without_changing_notes_or_legacy_platform_matching(self):
        rows = [issue(1, ""), issue(2, "HTS VBScript"), issue(3, "MTS VBScript JavaScript 교체")]
        rows[0]["notes"] = [{"text": "HTS VB Script를 JScript로 교체"}]
        self.write_store(rows)
        builds = Counter()
        original = mantis.issue_to_search_text

        def build(row):
            builds[row["id"]] += 1
            return original(row)

        with patch.object(mantis, "issue_to_search_text", side_effect=build):
            result = mantis.execute_mantis_page_scan(TOPIC, 5, HTS)
        self.assertEqual([row["id"] for row in result["issues_list"]], [1])
        self.assertEqual(result["meta"]["scanned_issue_ids"], [1, 2, 3])
        self.assertLessEqual(max(builds.values()), 1, "행마다 큰 본문과 날짜를 반복 조립하지 않는다")

    def test_complete_empty_search_cannot_resurrect_a_match_from_an_older_index(self):
        self.write_store([issue(1, "HTS 일반 문의")])
        result = self.pipeline()
        self.assertEqual(retrieval.collect_retrieved_issues(result, 5), [])
        self.assertEqual(result["keyword"], TOPIC)
        self.assertTrue(result["stages"][0]["meta"]["search_complete"])

    def test_complete_search_keeps_only_matches_in_its_pinned_generation(self):
        self.write_store([issue(1, "HTS VB Script JavaScript 교체")])
        result = self.pipeline()
        self.assertEqual([row["id"] for row in retrieval.collect_retrieved_issues(result, 5)], [1])

    def test_incomplete_and_unknown_sources_still_use_the_complementary_index(self):
        for meta in ({}, {"status": "snapshot", "search_complete": False, "snapshot_complete": True},
                     {"status": "snapshot", "search_complete": True, "snapshot_complete": False},
                     {"search_complete": True}):
            with self.subTest(meta=meta), patch.object(mantis, "stream_complete_store", return_value=nullcontext(IssueBatch([], meta))):
                result = self.pipeline()
            self.assertEqual([row["id"] for row in retrieval.collect_retrieved_issues(result, 5)], [99])

    def test_utc_custom_dates_keep_seconds_milliseconds_and_korean_search_forms(self):
        for value in (1735689600, 1735689600000, "20250101", "2025-01-01"):
            self.assertEqual(common.format_mantis_custom_date(value), "2025-01-01")
            variants = mantis.mantis_date_search_variants(value)
            self.assertIn("2025년 1월 1일", variants)
            self.assertIn("20250101", variants)
        row = issue(1, "HTS")
        row["custom_fields"] = [{"field": {"name": "고객사요청일"}, "value": 1735689600000}]
        self.write_store([row])
        result = mantis.execute_mantis_page_scan("2025년 1월", 5, HTS)
        self.assertEqual(result["meta"]["matched_issue_ids"], [1])

    def test_header_catalog_preserves_empty_and_whitespace_and_last_line_rules(self):
        values = ["- 담당자: 홍길동\n본문", "- 담당자:  김영희  ", "- 담당자: \n본문", "다른 헤더",
                  "- 담당자:\t팀장\r\n본문", "- 담당자: 첫째\n- 담당자: 둘째", "- 담당자: 홍길동\n중복"]
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE embedding_metadata(key TEXT,string_value TEXT)")
            db.executemany("INSERT INTO embedding_metadata VALUES ('chroma:document',?)", [(value,) for value in values])
            self.assertEqual(set(_distinct_chroma_header_values(db, "담당자")), {"홍길동", "김영희", "팀장", "첫째"})
