import sqlite3
import tempfile
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from backend import retrieval
from backend.answers import format_deterministic_issue_answer
from backend.sources import chroma, mantis
from backend.understanding import extract_query_constraints


QUESTION = "HTS에서 VB스크립트를 자바스크립트로 교체 문의 들어온거 있어?"
HTS = {"platform_key": "hts", "platform_aliases": ["HTS"]}


def raw_issue(issue_id, title, *, description="", notes=None):
    return {
        "id": issue_id,
        "summary": title,
        "description": description,
        "project": {"name": "112KBStar(KB증권)"},
        "status": {"name": "confirmed"},
        "handler": {"real_name": "석지원"},
        "created_at": "2025-05-01T09:00:00+09:00",
        "custom_fields": [{"field": {"name": "매체구분"}, "value": "HTS"}],
        "notes": notes or [],
    }


def document(issue):
    return "\n".join([
        "[Mantis 이슈 정보]",
        f"- 이슈 ID: #{issue['id']}",
        "- 프로젝트(고객사): 112KBStar(KB증권)",
        "- 매체구분: HTS",
        "- 현재 상태: confirmed",
        "- 등록일: 2025-05-01",
        "", "[제목]", issue["summary"],
        "", "[고객사 본문 요청 내용]", issue["description"],
    ])


class KeywordTopicIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "chroma.sqlite3"
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("CREATE TABLE embedding_metadata (key TEXT, string_value TEXT)")
        self.chroma_path = patch.object(chroma, "CHROMA_DB_PATH", self.database)
        self.chroma_path.start()
        self.addCleanup(self.chroma_path.stop)

    def insert(self, *issues):
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.executemany(
                "INSERT INTO embedding_metadata VALUES ('chroma:document', ?)",
                [(document(issue),) for issue in issues],
            )

    def test_chroma_missing_topic_does_not_become_platform_only_search(self):
        self.insert(raw_issue(31402, "HTS 종목 조회 오류"), raw_issue(31397, "화면 교체"))
        with patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=HTTPException(status_code=503, detail="오프라인 검증")):
            result = chroma.execute_chroma_search(QUESTION, constraints=HTS)
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["meta"]["candidate_count"], 0)
        self.assertEqual(result["meta"]["terms"], ["VB스크립트", "자바스크립트", "교체"])
        self.assertTrue(result["meta"]["search_complete"])

    def test_chroma_unscoped_topic_keeps_every_requested_keyword(self):
        self.insert(raw_issue(31402, "VB스크립트 오류"), raw_issue(31397, "일반 화면 교체"))
        with patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=HTTPException(status_code=503, detail="오프라인 검증")):
            result = chroma.execute_chroma_search("VB스크립트 자바스크립트 교체")
        self.assertEqual(result["documents"], [])

    def test_script_aliases_match_without_accepting_partial_topics(self):
        self.insert(
            raw_issue(301, "VBScript JavaScript 교체"),
            raw_issue(302, "VB Script JScript 교체"),
            raw_issue(303, "VB스크립트 자바스크립트 교체"),
            raw_issue(400, "JavaScript 화면 교체"),
        )
        for question in (QUESTION, "VBScript JavaScript 교체", "VB Script JScript 교체"):
            with self.subTest(question=question), patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=HTTPException(status_code=503, detail="오프라인 검증")):
                result = chroma.execute_chroma_search(question, constraints=HTS)
            self.assertEqual([row["issue_id"] for row in result["documents"]], [303, 302, 301])

    def test_live_refresh_rejects_an_issue_that_no_longer_matches_the_topic(self):
        self.insert(raw_issue(301, "VB스크립트 자바스크립트 교체"))
        for constraints in ({}, HTS):
            with self.subTest(constraints=constraints), patch.object(
                chroma, "fetch_mantis_issue_by_id", return_value=raw_issue(301, "HTS 주문 오류"),
            ):
                result = chroma.execute_chroma_search("VB스크립트 자바스크립트 교체", constraints=constraints)
            self.assertEqual(result["documents"], [])
            self.assertEqual(result["meta"]["skipped_after_refresh"], 1)

    def test_spaced_script_names_with_korean_particles_keep_the_whole_topic(self):
        self.insert(raw_issue(301, "VB스크립트 자바스크립트 교체"), raw_issue(400, "JavaScript 문의"))
        with patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=HTTPException(status_code=503, detail="오프라인 검증")):
            result = chroma.execute_chroma_search("HTS에서 VB Script를 Java Script로 교체", constraints=HTS)
        self.assertEqual([row["issue_id"] for row in result["documents"]], [301])
        self.assertEqual(result["meta"]["terms"], ["VBScript", "JavaScript", "교체"])

    def test_live_refresh_matches_aliases_in_notes(self):
        self.insert(raw_issue(301, "VB스크립트 자바스크립트 교체"))
        live = raw_issue(301, "스크립트 문의", notes=[{"text": "VB Script를 JScript로 교체 요청"}])
        with patch.object(chroma, "fetch_mantis_issue_by_id", return_value=live):
            result = chroma.execute_chroma_search(QUESTION, constraints=HTS)
        self.assertEqual([row["issue_id"] for row in result["documents"]], [301])
        self.assertEqual(result["documents"][0]["latest_source"], "mantis")

    def test_pure_platform_search_still_returns_matching_issues(self):
        self.insert(raw_issue(301, "종목 조회 오류"), raw_issue(302, "주문 오류"))
        with patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=HTTPException(status_code=503, detail="오프라인 검증")):
            result = chroma.execute_chroma_search("HTS 이슈 보여줘", constraints=HTS)
        self.assertEqual([row["issue_id"] for row in result["documents"]], [302, 301])

    def test_mantis_scan_does_not_fall_back_to_first_keyword(self):
        with patch.object(mantis, "stream_complete_store", return_value=nullcontext([raw_issue(301, "VB스크립트 오류")])):
            result = mantis.execute_mantis_page_scan("VB스크립트 자바스크립트 교체")
        self.assertEqual(result["issues_list"], [])
        self.assertEqual(result["meta"]["found_count"], 0)

    def test_mantis_scan_accepts_script_aliases_in_body_and_notes(self):
        issues = [
            raw_issue(301, "언어 교체", description="VBScript 대신 JavaScript 사용"),
            raw_issue(302, "교체 문의", notes=[{"text": "VB Script에서 JScript"}]),
            raw_issue(400, "JavaScript 화면 교체"),
        ]
        with patch.object(mantis, "stream_complete_store", return_value=nullcontext(issues)):
            result = mantis.execute_mantis_page_scan("VB스크립트 자바스크립트 교체", constraints=HTS)
        self.assertEqual({row["id"] for row in result["issues_list"]}, {301, 302})

    def test_scoped_topic_pipeline_does_not_mix_in_structure_only_results(self):
        issue = raw_issue(301, "일반 주문 오류")
        self.insert(issue)
        questions = [
            "KB HTS VB스크립트 자바스크립트 교체",
            "2025년에 등록된 HTS VB스크립트 자바스크립트 교체",
            QUESTION,
        ]
        for question in questions:
            with (
                self.subTest(question=question),
                patch.object(mantis, "CHROMA_DB_PATH", self.database),
                patch.object(mantis, "read_complete_store", return_value=[issue]),
                patch.object(mantis, "stream_complete_store", return_value=nullcontext([issue])),
                patch.object(mantis, "find_matching_mantis_filter", return_value={"filter_id": 1}),
                patch.object(mantis, "fetch_mantis_issues", return_value=[issue]),
                patch.object(mantis, "fetch_mantis_issue_by_id", return_value=issue),
                patch.object(chroma, "fetch_mantis_issue_by_id", return_value=issue),
            ):
                result = retrieval.run_retrieval_pipeline(
                    question, 5, {"mantis": True, "vectorDb": True}, {"source": "mantis"},
                )
            self.assertEqual(retrieval.collect_retrieved_issues(result, 5), [])
            self.assertEqual(result["keyword"], "VB스크립트 자바스크립트 교체")

    def test_empty_answer_states_the_topic_in_intro_and_search_criteria(self):
        constraints = extract_query_constraints(QUESTION)
        result = {"keyword": "VB스크립트 자바스크립트 교체", "stages": []}
        answer = format_deterministic_issue_answer(QUESTION, constraints, result, 5)
        intro = answer.splitlines()[0]
        criteria = next(line for line in answer.splitlines() if line.startswith("검색 기준:"))
        for term in ("VB스크립트", "자바스크립트", "교체"):
            self.assertIn(term, intro)
            self.assertIn(term, criteria)

    def test_partial_planner_terms_cannot_remove_topics_from_the_question(self):
        issues = [
            raw_issue(301, "일반 화면 교체"),
            raw_issue(302, "VB Script JavaScript 교체"),
            raw_issue(303, "자바스크립트 화면 교체"),
        ]
        for terms in (["교체"], ["스크립트", "교체"], ["VBScript", "JavaScript", "교체"]):
            with self.subTest(planned_terms=terms):
                with patch.object(mantis, "stream_complete_store", return_value=nullcontext(issues)):
                    result = retrieval.run_retrieval_pipeline(
                        QUESTION, 5, {"mantis": True}, {"source": "mantis", "terms": terms},
                    )
                self.assertEqual([row["id"] for row in retrieval.collect_retrieved_issues(result, 5)], [302])
                self.assertEqual(result["keyword"], "VB스크립트 자바스크립트 교체")
