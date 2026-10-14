"""검색 조건과 현재 원본이 다른 후보를 답변·집계에 포함하지 않는 회귀 검사."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import requests
from fastapi import HTTPException

from backend import retrieval
from backend.evaluation.comparison import evaluate_ab_variant
from backend.evaluation.verification import build_verification_report
from backend.intent.matching import chroma_text_matches_constraints
from backend.query_planning import build_query_plan
from backend.sources import chroma, mantis, mantis_store
from backend.sources.mantis_collection import IssueBatch
from backend.understanding import extract_query_constraints


def raw_issue(issue_id, title, **values):
    return {
        "id": issue_id, "summary": title, "description": "",
        "project": {"name": "120NH(NH증권)"},
        "created_at": "2025-07-01T09:00:00+09:00",
        "reporter": {"real_name": "다른요청자"},
        "handler": {"real_name": "석지원"}, **values,
    }


def complete_batch(rows):
    return IssueBatch(rows, {
        "status": "snapshot", "search_complete": True, "snapshot_complete": True,
        "fetchedAt": "2026-10-12T00:00:00+00:00",
    })


def create_database(path, rows):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("CREATE TABLE embedding_metadata (key TEXT, string_value TEXT)")
        db.executemany("INSERT INTO embedding_metadata VALUES ('chroma:document', ?)",
                       [(mantis_store.render_document(row),) for row in rows])


class HistoricalKeywordTests(unittest.TestCase):
    def search(self, rows, *, legacy=False, query="2025년 NH증권 로그인 오류 이슈 보여줘"):
        if legacy:
            directory = self.enterContext(tempfile.TemporaryDirectory())
            path = Path(directory) / "chroma.sqlite3"
            create_database(path, rows)
            self.enterContext(patch.object(mantis, "CHROMA_DB_PATH", path))
            self.enterContext(patch.dict("os.environ", {"MANTIS_INDEX_MODE": "off"}))
            stored = None
        else:
            stored = complete_batch(rows)
        self.enterContext(patch.object(mantis_store, "read_complete_store", return_value=stored))
        self.enterContext(patch.object(mantis, "get_mantis_snapshot_freshness", return_value="2026-10-12"))
        self.enterContext(patch.object(retrieval, "get_mantis_snapshot_freshness", return_value="2026-10-12"))
        self.enterContext(patch.object(mantis, "fetch_mantis_issue_by_id",
                                      side_effect=lambda issue_id: next(row for row in rows if row["id"] == issue_id)))
        plan = build_query_plan(query, {"source": "mantis"}, extract_query_constraints(query))
        return retrieval.run_retrieval_pipeline(query, 2, {"mantis": True, "vectorDb": True}, plan)

    def test_historical_keywords_filter_full_source_before_display_limit_and_count(self):
        rows = [raw_issue(i, "차트 색상 변경") for i in range(30010, 30002, -1)]
        rows += [raw_issue(30001, "로그인 오류"),
                 raw_issue(29999, "확인 요청", notes=[{"text": "로그인 과정의 오류 확인"}])]
        result = self.search(rows)
        self.assertEqual([row["id"] for row in retrieval.collect_retrieved_issues(result, None)], [30001, 29999])
        self.assertEqual(retrieval.count_retrieved_issues(result), 2)
        self.assertEqual(result["stages"][0]["meta"]["found_count"], 2)
        self.assertTrue(result["stages"][0]["meta"]["search_complete"])

    def test_historical_keyword_absence_is_a_complete_empty_result(self):
        result = self.search([raw_issue(30001, "차트 색상 변경")])
        self.assertEqual(retrieval.collect_retrieved_issues(result, None), [])
        self.assertEqual(retrieval.count_retrieved_issues(result), 0)
        self.assertTrue(result["stages"][0]["meta"]["search_complete"])

    def test_historical_keywords_also_filter_legacy_snapshot(self):
        result = self.search([raw_issue(30002, "차트 색상 변경"), raw_issue(30001, "로그인 오류")], legacy=True)
        self.assertEqual([row["id"] for row in retrieval.collect_retrieved_issues(result, None)], [30001])
        self.assertEqual(retrieval.count_retrieved_issues(result), 1)
        # 구형 벡터 저장본의 과거 전체 범위를 최신 원본으로 오인하지 않는다.
        self.assertFalse(result["stages"][0]["meta"]["search_complete"])

    def test_historical_query_without_keywords_keeps_all_structural_matches(self):
        result = self.search([raw_issue(30002, "차트 색상 변경"), raw_issue(30001, "로그인 오류")],
                             query="2025년 NH증권 이슈 보여줘")
        self.assertEqual([row["id"] for row in retrieval.collect_retrieved_issues(result, None)], [30002, 30001])
        self.assertEqual(retrieval.count_retrieved_issues(result), 2)

    def test_screen_existence_phrase_is_not_an_issue_content_keyword(self):
        row = raw_issue(30001, "[6110] 차트 조회 오류", custom_fields=[
            {"field": {"name": "화면번호"}, "value": "6110"},
        ])
        result = self.search([row], query="화면번호 6110 관련 Mantis 이슈가 있는지 확인해줘")
        self.assertEqual(result["keyword"], "")
        self.assertEqual([issue["id"] for issue in retrieval.collect_retrieved_issues(result, None)], [30001])
        self.assertEqual(retrieval.count_retrieved_issues(result), 1)


class ReporterRoleTests(unittest.TestCase):
    def test_reporter_filter_uses_reporter_or_explicit_sender_not_handler_or_body_name(self):
        constraints = {"people": {"names": ["홍길동"], "role": "reporter"}}
        cases = [
            (raw_issue(30001, "요청", reporter={"real_name": "홍길동"}), True),
            (raw_issue(30002, "요청"), False),
            (raw_issue(30003, "홍길동에게 확인 요청", description="홍길동이 처리할 예정"), False),
            (raw_issue(30004, "요청", reporter={"real_name": "DRFN_NEXT"}, description="From. 홍길동\n로그인 오류 문의"), True),
            (raw_issue(30005, "요청", description="From. 홍길동2\n로그인 오류 문의"), False),
        ]
        for issue, expected in cases:
            with self.subTest(issue_id=issue["id"]):
                self.assertEqual(mantis.issue_matches_constraints(issue, constraints), expected)
                self.assertEqual(chroma_text_matches_constraints(mantis_store.render_document(issue), constraints), expected)

    def test_handler_and_any_roles_keep_their_established_scope(self):
        issue = raw_issue(30001, "요청", handler={"real_name": "홍길동"})
        for role, name, expected in [("handler", "홍길동", True), ("handler", "다른요청자", False),
                                      ("any", "홍길동", True), ("any", "다른요청자", True)]:
            with self.subTest(role=role, name=name):
                constraints = {"people": {"names": [name], "role": role}}
                self.assertEqual(mantis.issue_matches_constraints(issue, constraints), expected)

    def test_reporter_grading_uses_the_same_structured_author_as_search(self):
        question = "홍길동이 요청한 이슈 보여줘"
        cases = [(raw_issue(30001, "요청", reporter={"real_name": "홍길동"}), True),
                 (raw_issue(30002, "요청"), False)]
        with patch("backend.intent.entities.load_known_people", return_value=(["홍길동"], ["다른요청자"])):
            constraints = extract_query_constraints(question)
            for issue, expected in cases:
                with self.subTest(issue_id=issue["id"]):
                    sources = mantis.build_sources_from_issue_rows([mantis.normalize_issue(issue)])
                    content = f"#{issue['id']} 요청 이슈입니다."
                    report = build_verification_report(question, content, sources, {
                        "query": question, "constraints": constraints,
                        "stages": [{"source": "mantis_snapshot", "status": "ok",
                                    "meta": {"search_complete": True}}],
                    }, "deterministic_issue_list")
                    checks = report["checks"]
                    self.assertEqual(next(check["status"] for check in checks if check["id"] == "constraints"),
                                     "pass" if expected else "warning")
                    self.assertEqual(evaluate_ab_variant(question, content, sources)["metrics"]["constraints"],
                                     100 if expected else 0)

    def test_reporter_marker_does_not_match_account_suffix_or_ordinary_body_headers(self):
        constraints = {"people": {"names": ["홍길동"], "role": "reporter"}}
        for description in ("notFrom. 홍길동", "요청자: 홍길동\n처리 담당자에게 전달"):
            with self.subTest(description=description):
                issue = raw_issue(30001, "요청", reporter={}, handler={}, description=description)
                self.assertFalse(mantis.issue_matches_constraints(issue, constraints))

    def test_explicit_customer_sender_in_later_note_is_supported_by_grading(self):
        question = "홍길동이 요청한 이슈 보여줘"
        issue = raw_issue(30001, "요청", reporter={"real_name": "DRFN_NEXT"}, handler={},
                          notes=[{"text": "접수 메일\nFrom. 홍길동\n로그인 오류"}])
        with patch("backend.intent.entities.load_known_people", return_value=(["홍길동"], ["DRFN_NEXT"])):
            sources = mantis.build_sources_from_issue_rows([mantis.normalize_issue(issue)])
            self.assertEqual(evaluate_ab_variant(question, "#30001 요청 이슈입니다.", sources)["metrics"]["constraints"], 100)

    def test_snapshot_and_offline_vector_sources_preserve_reporter_for_grading(self):
        question = "홍길동이 요청한 이슈 보여줘"
        issue = raw_issue(30001, "요청", reporter={"real_name": "홍길동"}, handler={})
        with (patch("backend.intent.entities.load_known_people", return_value=([], ["홍길동"])),
              patch.object(mantis_store, "read_complete_store", return_value=complete_batch([issue])),
              patch.object(mantis, "get_mantis_snapshot_freshness", return_value="2026-10-12")):
            constraints = extract_query_constraints(question)
            stages = [mantis.execute_mantis_snapshot_search(2, constraints), {
                "source": "chroma_db", "status": "ok", "documents": [
                    {"issue_id": 30001, "content": mantis_store.render_document(issue), "latest_source": "chroma_snapshot"},
                ],
            }]
            for stage in stages:
                with self.subTest(source=stage["source"]):
                    sources = retrieval.build_sources({"stages": [stage]})
                    self.assertEqual(sources[0]["detail"].get("reporter"), "홍길동")
                    self.assertEqual(evaluate_ab_variant(question, "#30001 요청 이슈입니다.", sources)["metrics"]["constraints"], 100)

    def test_reporter_filter_clauses_share_structured_and_customer_sender_scope(self):
        cases = [(raw_issue(30001, "요청", reporter={"real_name": "홍길동"}), True),
                 (raw_issue(30002, "요청", reporter={"real_name": "DRFN_NEXT"},
                            description="From. 홍길동\n문의"), True),
                 (raw_issue(30003, "홍길동에게 전달", reporter={"real_name": "DRFN_NEXT"}), False),
                 (raw_issue(30004, "요청", reporter={"real_name": "DRFN_NEXT"},
                            description="From. 홍길동2\n문의"), False)]
        for issue, expected in cases:
            for operator in ("equals", "in"):
                for with_people in (True, False):
                    with self.subTest(issue_id=issue["id"], operator=operator, with_people=with_people):
                        constraints = {"people": {"names": ["홍길동"], "role": "reporter"} if with_people else {},
                            "filter_clauses": [{"field": "reporter", "operator": operator,
                                                "value": ["홍길동"] if operator == "in" else "홍길동"}]}
                        self.assertEqual(mantis.issue_matches_constraints(issue, constraints), expected)
                        self.assertEqual(chroma_text_matches_constraints(mantis_store.render_document(issue), constraints), expected)


class ChromaCurrentPayloadTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "chroma.sqlite3"
        create_database(self.path, [raw_issue(30001, "로그인 오류")])
        self.enterContext(patch.object(chroma, "CHROMA_DB_PATH", self.path))

    def test_current_lexical_mismatch_cannot_reappear_after_complete_scan_excluded_it(self):
        current = raw_issue(30001, "차트 색상 변경")
        stored = complete_batch([current])
        with (patch.object(mantis_store, "read_complete_store", return_value=stored),
              patch.object(mantis, "read_complete_store", return_value=stored),
              patch.object(chroma, "fetch_mantis_issue_by_id", return_value=current)):
            result = retrieval.run_retrieval_pipeline("멘티스 로그인 오류 이슈 보여줘", 2,
                                                     {"mantis": True, "vectorDb": True}, {"source": "mantis"})
        self.assertEqual(retrieval.collect_retrieved_issues(result, None), [])
        self.assertEqual(retrieval.count_retrieved_issues(result), 0)
        self.assertEqual(result["stages"][-1]["meta"]["scanned_issue_ids"], [30001])
        self.assertEqual(result["stages"][-1]["meta"]["matched_issue_ids"], [])

    def test_current_body_keyword_match_remains_eligible(self):
        current = raw_issue(30001, "확인 요청", notes=[{"text": "로그인 오류 조치"}])
        with patch.object(chroma, "fetch_mantis_issue_by_id", return_value=current):
            result = chroma.execute_chroma_search("로그인 오류", 2)
        self.assertEqual([document["issue_id"] for document in result["documents"]], [30001])
        self.assertEqual(result["documents"][0]["latest_source"], "mantis")

    def test_confirmed_not_found_candidate_is_removed_from_rows_and_counts(self):
        with patch.object(chroma, "fetch_mantis_issue_by_id", return_value=None):
            result = chroma.execute_chroma_search("로그인 오류", 2)
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["meta"]["scanned_issue_ids"], [30001])
        self.assertEqual(result["meta"]["matched_issue_ids"], [])
        self.assertTrue(result["meta"]["search_complete"])

    def test_access_denied_candidate_is_not_used_as_offline_fallback(self):
        with patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=HTTPException(403, "denied")):
            result = chroma.execute_chroma_search("로그인 오류", 2)
        self.assertEqual(result["documents"], [])
        self.assertEqual(result["meta"]["scanned_issue_ids"], [30001])

    def test_network_outage_keeps_snapshot_with_incomplete_signal(self):
        with patch.object(chroma, "fetch_mantis_issue_by_id", side_effect=requests.Timeout("offline")):
            result = chroma.execute_chroma_search("로그인 오류", 2)
        self.assertEqual([document["issue_id"] for document in result["documents"]], [30001])
        self.assertEqual(result["documents"][0]["latest_source"], "chroma_snapshot")
        self.assertFalse(result["meta"]["search_complete"])
        self.assertFalse(result["meta"]["result_window_complete"])

    def test_semantic_candidate_does_not_require_literal_query_terms(self):
        current = raw_issue(30001, "인증 실패 해결")
        with (patch.object(chroma, "rank_chroma_documents_semantically", return_value=(
                [{"issue_id": 30001, "content": "문서", "similarity_rank": 1}], {"ranking": "semantic_vector"})),
              patch.object(chroma, "fetch_mantis_issue_by_id", return_value=current)):
            result = chroma.execute_chroma_search("로그인 오류", 2, semantic=True)
        self.assertEqual([document["issue_id"] for document in result["documents"]], [30001])
        self.assertFalse(result["meta"]["search_complete"])
