"""검색 건수와 본문·담당자 필터의 실제 데이터 계약을 검증한다."""

import json
import os
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import requests

from backend.notion import client, mapping, search
from backend.retrieval import run_retrieval_pipeline
from backend.intent.constraints import extract_query_constraints, merge_query_plan_constraints


def response(data):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps(data, ensure_ascii=False).encode()
    result.encoding = "utf-8"
    return result


def page(identifier, *, memo="", owners=None, status=""):
    properties = {
        "제목": {"type": "title", "title": [{"plain_text": "운영 문서"}]},
        "메모": {"type": "rich_text", "rich_text": [{"plain_text": memo}]},
    }
    if owners is not None:
        properties["담당자"] = {"type": "people", "people": owners}
    if status:
        properties["상태"] = {"type": "select", "select": {"name": status}}
    return {
        "object": "page", "id": identifier, "properties": properties,
        "created_time": "2026-09-20T00:00:00Z",
        "last_edited_time": "2026-09-21T00:00:00Z",
        "parent": {"type": "database_id", "database_id": "db"},
        "url": f"https://www.notion.so/{identifier}",
    }


class NotionAccuracyRegressionTests(unittest.TestCase):
    def setUp(self):
        client._page_content_cached.cache_clear()
        search._NOTION_USER_ID_HINTS.clear()
        self.addCleanup(client._page_content_cached.cache_clear)
        self.addCleanup(search._NOTION_USER_ID_HINTS.clear)

    def search_fixture(self, rows, blocks):
        """외부 DB·블록 응답만 대체하고 정규화·필터·집계는 그대로 실행한다."""
        stack = ExitStack()
        self.addCleanup(stack.close)
        properties = {"제목": {"type": "title"}, "메모": {"type": "rich_text"}}
        stack.enter_context(patch.object(search, "get_notion_settings", return_value={
            "token": "test-token", "database_ids": ["db"],
        }))
        stack.enter_context(patch.object(search, "resolve_notion_read_scope", return_value=({"db": "db"}, True)))
        stack.enter_context(patch.object(search, "fetch_notion_database_title", return_value="문서 DB"))
        stack.enter_context(patch.object(search, "fetch_notion_database_schema", return_value={"properties": properties}))
        stack.enter_context(patch.object(search, "notion_property_schema", return_value=properties))
        stack.enter_context(patch.object(search, "indexed_title_term_database_ids", return_value=[]))
        stack.enter_context(patch.dict(os.environ, {"NOTION_FETCH_PAGE_BLOCKS": "true", "NOTION_SEARCH_MAX_WORKERS": "1"}))
        stack.enter_context(patch.object(search, "notion_post_json", side_effect=lambda _url, payload, **_kw: response({
            "results": [] if "filter" in payload else rows, "has_more": False,
        })))
        stack.enter_context(patch.object(client, "notion_request_slot"))

        def block_response(url, *, params, **_kwargs):
            rows = blocks.get(url.split("/blocks/")[1].split("/")[0], [])
            start = int(params.get("start_cursor", "0"))
            end = start + params["page_size"]
            return response({"results": rows[start:end], "has_more": len(rows) > end,
                             "next_cursor": str(end) if len(rows) > end else None})

        stack.enter_context(patch.object(client.requests, "get", side_effect=block_response))
        stack.enter_context(patch.object(search, "fetch_notion_page_blocks", side_effect=client.fetch_notion_page_blocks))

    @staticmethod
    def paragraph(identifier, text):
        return {"id": identifier, "type": "paragraph", "has_children": False,
                "paragraph": {"rich_text": [{"plain_text": text}]}}

    def test_count_includes_body_matches_after_display_limit(self):
        rows = [page(f"property-{i}", memo="needle") for i in range(5)] + [page("body-only")]
        self.search_fixture(rows, {"body-only": [self.paragraph("text", "needle 본문")]})
        result = search.execute_notion_search("노션 needle 문서 몇 개야", limit=5,
                                             planned_terms=["needle"], include_blocks=False)
        self.assertEqual(result["meta"]["candidate_count"], 6)
        self.assertEqual(result["meta"]["displayed_count"], 5)
        self.assertTrue(result["meta"]["candidate_count_complete"])
        self.assertTrue(result["meta"]["search_complete"])

    def test_count_reports_incomplete_when_body_budget_is_exhausted(self):
        rows = [page(f"property-{i}", memo="needle") for i in range(5)]
        rows += [page(f"body-{i}") for i in range(31)]
        self.search_fixture(rows, {f"body-{i}": [self.paragraph(f"text-{i}", "needle")] for i in range(31)})
        result = search.execute_notion_search("노션 needle 문서 몇 개야", limit=5,
                                             planned_terms=["needle"], include_blocks=False)
        self.assertEqual(result["meta"]["candidate_count"], 35)
        self.assertFalse(result["meta"]["candidate_count_complete"])
        self.assertFalse(result["meta"]["search_complete"])
        self.assertIn("body_scan_limit", result["meta"]["incomplete_reasons"])

    def test_count_is_complete_when_last_candidate_uses_last_body_slot(self):
        rows = [page(f"property-{i}", memo="needle") for i in range(5)]
        rows += [page(f"body-{i}") for i in range(30)]
        self.search_fixture(rows, {f"body-{i}": [self.paragraph(f"text-{i}", "needle")] for i in range(30)})
        result = search.execute_notion_search("노션 needle 문서 몇 개야", limit=5,
                                             planned_terms=["needle"], include_blocks=False)
        self.assertEqual(result["meta"]["candidate_count"], 35)
        self.assertTrue(result["meta"]["candidate_count_complete"])
        self.assertTrue(result["meta"]["search_complete"])
        self.assertNotIn("body_scan_limit", result["meta"]["incomplete_reasons"])

    def test_count_is_incomplete_when_candidate_body_is_truncated(self):
        self.search_fixture([page("body-only")], {
            "body-only": [self.paragraph(f"text-{i}", "운영 안내") for i in range(20)]
            + [self.paragraph("last", "needle")],
        })
        result = search.execute_notion_search("노션 needle 문서 몇 개야", limit=5,
                                             planned_terms=["needle"], include_blocks=False)
        self.assertEqual(result["meta"]["candidate_count"], 0)
        self.assertFalse(result["meta"]["search_complete"])
        self.assertFalse(result["meta"]["candidate_count_complete"])
        self.assertIn("body_limit", result["meta"]["incomplete_reasons"])

    def test_matched_truncated_body_does_not_claim_complete_content(self):
        self.search_fixture([page("body-only")], {
            "body-only": [self.paragraph(f"text-{i}", "needle") for i in range(201)],
        })
        result = search.execute_notion_search("노션 needle 문서 몇 개야", limit=5,
                                             planned_terms=["needle"], include_blocks=True)
        self.assertEqual(result["meta"]["candidate_count"], 1)
        self.assertFalse(result["meta"]["content_complete"])
        self.assertEqual(result["meta"]["content_incomplete_page_ids"], ["body-only"])

    def test_nested_code_and_table_content_are_searchable(self):
        self.search_fixture([page("body-only")], {
            "body-only": [{"id": "toggle", "type": "toggle", "has_children": True,
                           "toggle": {"rich_text": [{"plain_text": "운영 안내"}]}}],
            "toggle": [{"id": "code", "type": "code", "has_children": False,
                        "code": {"rich_text": [{"plain_text": "배포 체크리스트"}], "language": "plain text"}},
                       {"id": "table", "type": "table", "has_children": True,
                        "table": {"table_width": 2, "has_column_header": False, "has_row_header": False}}],
            "table": [{"id": "row", "type": "table_row", "has_children": False,
                       "table_row": {"cells": [[{"plain_text": "장소"}], [{"plain_text": "서울 회의실"}]]}}],
        })
        result = search.execute_notion_search("노션 배포 서울 문서 몇 개야", limit=5,
                                             planned_terms=["배포", "서울"], include_blocks=False)
        self.assertEqual(result["meta"]["candidate_count"], 1)
        self.assertIn("배포 체크리스트", result["notion_pages"][0]["content"])
        self.assertIn("서울 회의실", result["notion_pages"][0]["content"])
        self.assertTrue(result["meta"]["search_complete"])

    def test_multiple_owner_choices_keep_or_semantics(self):
        clauses = [{"field": "owner", "operator": "in", "value": ["김지은", "홍길동"]}]
        for native in ({}, {"owner": "김지은"}):
            constraints = search.merge_common_notion_constraints(native, {"filter_clauses": clauses})
            for name, expected in [("김지은", True), ("홍길동", True), ("김보람", False)]:
                with self.subTest(native=native, owner=name):
                    normalized = mapping.normalize_notion_page(page("p", owners=[{"id": "u", "name": name}]), include_blocks=False)
                    self.assertEqual(search.notion_page_matches_and_enrich(normalized, constraints), expected)

    def test_multiple_status_choices_keep_or_semantics(self):
        clauses = [{"field": "status", "operator": "in", "value": ["검토중", "승인대기"]}]
        for native in ({}, {"status": "검토중", "native_status_constraint": True}):
            constraints = search.merge_common_notion_constraints(native, {"filter_clauses": clauses})
            for status, expected in [("검토중", True), ("승인대기", True), ("완료", False)]:
                with self.subTest(native=native, status=status):
                    normalized = mapping.normalize_notion_page(page("p", status=status), include_blocks=False)
                    self.assertEqual(search.notion_page_matches_and_enrich(normalized, constraints), expected)

    def test_search_returns_both_owner_and_status_choices(self):
        self.search_fixture([
            page("accepted-1", owners=[{"id": "u1", "name": "김지은"}], status="검토중"),
            page("accepted-2", owners=[{"id": "u2", "name": "홍길동"}], status="승인대기"),
            page("rejected", owners=[{"id": "u3", "name": "김보람"}], status="완료"),
        ], {})
        result = search.execute_notion_search("노션 항목 보여줘", constraints={"filter_clauses": [
            {"field": "owner", "operator": "in", "value": ["김지은", "홍길동"]},
            {"field": "status", "operator": "in", "value": ["검토중", "승인대기"]},
        ]}, include_blocks=False)
        self.assertEqual({entry["id"] for entry in result["notion_pages"]}, {"accepted-1", "accepted-2"})
        self.assertTrue(result["meta"]["search_complete"])

    def test_named_owner_does_not_hide_second_uuid_owner(self):
        known_id, unknown_id = "1" * 32, "22222222-2222-2222-2222-222222222222"
        normalized = mapping.normalize_notion_page(page("p", owners=[
            {"id": known_id, "name": "김지은"}, {"id": unknown_id},
        ]), include_blocks=False)
        with patch.object(mapping, "fetch_notion_user_name", side_effect=lambda identifier: "홍길동" if identifier == unknown_id else ""):
            self.assertTrue(search.notion_page_matches_and_enrich(normalized, {"owner": "홍길동"}))
        self.assertEqual(set(normalized["owner"].split(", ")), {"김지은", "홍길동"})

    def test_mixed_people_relation_owners_are_not_filtered_to_people_only(self):
        with patch.object(search, "notion_property_schema", return_value={
            "담당자": {"type": "people"}, "개발담당자": {"type": "relation"},
        }), patch.object(search, "fetch_notion_users", return_value=(("홍길동", "user-2"),)):
            decisive, api_filter = search.notion_database_owner_filter("db", "홍길동")
        self.assertFalse(decisive)
        self.assertIsNone(api_filter)

    def test_owner_presence_uses_assignment_instead_of_display_placeholder(self):
        for owners, expected in [([], False), ([{"id": "user-1", "name": "김지은"}], True)]:
            normalized = mapping.normalize_notion_page(page("p", owners=owners), include_blocks=False)
            for operator, want in [("exists", expected), ("not_exists", not expected)]:
                with self.subTest(owners=owners, operator=operator):
                    self.assertEqual(search.notion_page_matches_and_enrich(normalized, {
                        "common_filter_clauses": [{"field": "owner", "operator": operator, "value": True}],
                    }), want)

    def test_pipeline_keyword_filter_reads_body_only_candidates(self):
        self.search_fixture([page("body-only")], {
            "body-only": [self.paragraph("text", "needle 본문")],
        })
        question = "노션 본문에 needle 포함한 문서는 몇 개야"
        result = run_retrieval_pipeline(question, 5, {"notion": True}, {
            "source": "notion", "grounding_text": question, "terms": ["needle"],
            "filters": [{"field": "keyword", "operator": "contains", "value": "needle"}],
        })
        stage = result["stages"][0]
        self.assertEqual(stage["meta"]["candidate_count"], 1)
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["body-only"])
        self.assertTrue(stage["meta"]["search_complete"])

    def test_pipeline_positive_and_negative_keywords_are_checked_after_body_read(self):
        self.search_fixture([page("accepted"), page("excluded", memo="needle"), page("unrelated")], {
            "accepted": [self.paragraph("text-1", "needle 운영 절차")],
            "excluded": [self.paragraph("text-2", "deprecated 운영 절차")],
            "unrelated": [self.paragraph("text-3", "운영 절차")],
        })
        question = "노션 needle 포함하고 deprecated 제외한 문서는 몇 개야"
        result = run_retrieval_pipeline(question, 5, {"notion": True}, {
            "source": "notion", "grounding_text": question, "terms": ["needle"],
            "filters": [
                {"field": "keyword", "operator": "contains", "value": "needle"},
                {"field": "keyword", "operator": "not_contains", "value": "deprecated"},
            ],
        })
        stage = result["stages"][0]
        self.assertEqual(stage["meta"]["candidate_count"], 1)
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["accepted"])
        self.assertTrue(stage["meta"]["search_complete"])

    def test_negative_keyword_without_free_terms_checks_body(self):
        self.search_fixture([page("accepted"), page("excluded")], {
            "accepted": [self.paragraph("text-1", "운영 절차")],
            "excluded": [self.paragraph("text-2", "deprecated 절차")],
        })
        stage = search.execute_notion_search("노션 문서 몇 개야", constraints={"filter_clauses": [
            {"field": "keyword", "operator": "not_contains", "value": "deprecated"},
        ]}, include_blocks=False)
        self.assertEqual(stage["meta"]["candidate_count"], 1)
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["accepted"])

    def test_negative_keyword_does_not_accept_unread_truncated_body(self):
        self.search_fixture([page("unknown")], {
            "unknown": [self.paragraph(f"text-{i}", "운영 절차") for i in range(20)]
            + [self.paragraph("last", "deprecated 절차")],
        })
        stage = search.execute_notion_search("노션 문서 몇 개야", constraints={"filter_clauses": [
            {"field": "keyword", "operator": "not_contains", "value": "deprecated"},
        ]}, include_blocks=False)
        self.assertEqual(stage["meta"]["candidate_count"], 0)
        self.assertFalse(stage["meta"]["search_complete"])
        self.assertFalse(stage["meta"]["candidate_count_complete"])

    def test_exact_title_keyword_filter_is_checked_after_body_read(self):
        rows = [page("exact-body")]
        self.search_fixture(rows, {"exact-body": [self.paragraph("text", "needle 절차")]})
        with patch.object(search, "notion_post_json", return_value=response({"results": rows, "has_more": False})):
            stage = search.execute_notion_search('노션 "운영 문서" 찾아줘', constraints={"filter_clauses": [
                {"field": "keyword", "operator": "contains", "value": "needle"},
            ]}, include_blocks=False)
        self.assertEqual(stage["meta"]["candidate_count"], 1)
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["exact-body"])
        self.assertTrue(stage["meta"]["search_complete"])

    def test_keyword_only_body_scan_keeps_global_budget_and_incomplete_count(self):
        self.search_fixture([page(f"body-{i}") for i in range(31)], {
            f"body-{i}": [self.paragraph(f"text-{i}", "needle 절차")] for i in range(31)
        })
        stage = search.execute_notion_search("노션 문서 몇 개야", constraints={"filter_clauses": [
            {"field": "keyword", "operator": "contains", "value": "needle"},
        ]}, include_blocks=False)
        self.assertEqual(stage["meta"]["candidate_count"], 30)
        self.assertEqual(stage["meta"]["body_scan_attempted_count"], 30)
        self.assertFalse(stage["meta"]["candidate_count_complete"])
        self.assertFalse(stage["meta"]["search_complete"])
        self.assertIn("body_scan_limit", stage["meta"]["incomplete_reasons"])

    def test_pipeline_negative_keyword_question_does_not_create_positive_terms(self):
        self.search_fixture([page("accepted"), page("excluded")], {
            "accepted": [self.paragraph("text-1", "운영 절차")],
            "excluded": [self.paragraph("text-2", "deprecated 절차")],
        })
        question = "노션 deprecated 제외한 문서는 몇 개야"
        result = run_retrieval_pipeline(question, 5, {"notion": True}, {
            "source": "notion", "grounding_text": question, "terms": [],
            "filters": [{"field": "keyword", "operator": "not_contains", "value": "deprecated"}],
        })
        stage = result["stages"][0]
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["accepted"])
        self.assertEqual(stage["meta"]["candidate_count"], 1)
        self.assertTrue(stage["meta"]["search_complete"])

    def test_negative_keyword_removal_keeps_positive_free_terms(self):
        self.search_fixture([page("accepted"), page("excluded"), page("unrelated")], {
            "accepted": [self.paragraph("text-1", "needle 운영 절차")],
            "excluded": [self.paragraph("text-2", "needle deprecated 절차")],
            "unrelated": [self.paragraph("text-3", "운영 절차")],
        })
        for ending in ["제외한", "빼고", "말고", "아닌"]:
            for terms in [[], ["needle", "deprecated", ending]]:
                with self.subTest(ending=ending, planned_terms=terms):
                    question = f"노션 needle deprecated는 {ending} 문서는 몇 개야"
                    result = run_retrieval_pipeline(question, 5, {"notion": True}, {
                        "source": "notion", "grounding_text": question, "terms": terms,
                        "filters": [{"field": "keyword", "operator": "not_contains", "value": "deprecated"}],
                    })
                    stage = result["stages"][0]
                    self.assertEqual(stage["meta"]["terms"], ["needle"])
                    self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["accepted"])
                    self.assertEqual(stage["meta"]["candidate_count"], 1)

    def test_keyword_exclusion_requires_adjacent_value_evidence(self):
        for question in ["노션 문서 빼고 항목은 몇 개야", "노션 deprecated 문서와 MTS 빼고 항목은 몇 개야"]:
            with self.subTest(question=question):
                merged = merge_query_plan_constraints(extract_query_constraints(question), {
                    "source": "notion", "grounding_text": question,
                    "filters": [{"field": "keyword", "operator": "not_contains", "value": "deprecated"}],
                })
                self.assertEqual([clause for clause in merged["filter_clauses"] if clause["field"] == "keyword"], [])

    def test_positive_keyword_without_keyword_intent_is_still_rejected(self):
        question = "노션 deprecated 문서는 몇 개야"
        merged = merge_query_plan_constraints(extract_query_constraints(question), {
            "source": "notion", "grounding_text": question,
            "filters": [{"field": "keyword", "operator": "contains", "value": "deprecated"}],
        })
        self.assertEqual([clause for clause in merged["filter_clauses"] if clause["field"] == "keyword"], [])
