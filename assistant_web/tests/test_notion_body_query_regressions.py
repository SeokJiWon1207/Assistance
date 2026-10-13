"""실제 연결에서 재현된 본문 요청을 플래너 없이 검색한다."""

import os
import unittest
from unittest.mock import patch

from backend.notion import search
from backend.notion.formatting import has_deterministic_notion_list_intent
from backend.notion.query_text import notion_literal_title, split_notion_body_query
from backend.retrieval import run_retrieval_pipeline
import test_notion_accuracy_regressions as fixtures

page, response = fixtures.page, fixtures.response


class NotionBodyQueryTests(unittest.TestCase):
    setUp = fixtures.NotionAccuracyRegressionTests.setUp
    search_fixture = fixtures.NotionAccuracyRegressionTests.search_fixture
    paragraph = staticmethod(fixtures.NotionAccuracyRegressionTests.paragraph)

    def test_natural_body_requests_find_original_blocks_without_a_plan(self):
        self.search_fixture([page("target")], {"target": [
            self.paragraph("block", "MARKETTYPE_FUTOPT"),
            {"id": "code", "type": "code", "code": {"rich_text": [{"plain_text": "MARKETTYPE_FUTOPT"}]}},
            {"id": "row", "type": "table_row", "table_row": {"cells": [[{"plain_text": "MARKETTYPE_FUTOPT"}]]}},
        ]})
        for request in (
            '본문에 MARKETTYPE_FUTOPT 포함한', '본문에 "MARKETTYPE_FUTOPT" 포함한',
            '표에서 MARKETTYPE_FUTOPT 포함한', '코드 블록에 “MARKETTYPE_FUTOPT” 포함된',
            'MARKETTYPE_FUTOPT 내용이 있는',
        ):
            with self.subTest(request=request):
                result = run_retrieval_pipeline(f"노션 {request} 문서를 찾아줘", 5, {"notion": True})
                stage = result["stages"][0]
                self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["target"])
                self.assertEqual(stage["meta"]["constraints"]["exact_title"], "")
                self.assertTrue(stage["meta"]["search_complete"])

    def test_quoted_body_keyword_is_not_a_structured_status(self):
        self.search_fixture([page("target", status="진행중")], {"target": [self.paragraph("block", "완료") ]})
        result = run_retrieval_pipeline('노션 본문에 "완료" 포함한 문서 찾아줘', 5, {"notion": True})
        self.assertEqual([entry["id"] for entry in result["stages"][0]["notion_pages"]], ["target"])

    def test_title_and_separately_quoted_body_keep_independent_meanings(self):
        self.search_fixture([page("target")], {"target": [self.paragraph("block", "needle") ]})
        with patch.object(search, "cached_title_query", return_value=response({"results": [page("target")], "has_more": False})):
            for query in (
                "노션에서 제목이 '운영 문서'인 문서 중 본문에 needle 포함한 문서 찾아줘",
                "노션 '운영 문서' 문서에서 본문에 'needle' 포함된 문서 찾아줘",
            ):
                with self.subTest(query=query):
                    stage = run_retrieval_pipeline(query, 5, {"notion": True})["stages"][0]
                    self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["target"])
                    self.assertEqual(stage["meta"]["constraints"]["exact_title"], "운영 문서")

    def test_negative_body_literal_does_not_become_a_positive_search_term(self):
        self.search_fixture([page("accepted"), page("excluded")], {
            "accepted": [self.paragraph("a", "needle")],
            "excluded": [self.paragraph("b", "needle"),
                         {"id": "code", "type": "code", "code": {"rich_text": [{"plain_text": "deprecated"}]}}],
        })
        stage = run_retrieval_pipeline('노션 본문에 "needle" 포함하고 코드에 "deprecated" 없는 문서 찾아줘', 5, {"notion": True})["stages"][0]
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["accepted"])

    def test_chained_body_condition_inherits_scope_without_using_metadata(self):
        self.search_fixture([page("accepted", memo="deprecated"), page("excluded")], {
            "accepted": [self.paragraph("a", "needle 정상 절차")],
            "excluded": [self.paragraph("b", "needle deprecated 절차")],
        })
        query = '노션 본문에 "needle" 포함하고 "deprecated" 제외한 문서 찾아줘'
        stage = run_retrieval_pipeline(query, 5, {"notion": True})["stages"][0]
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["accepted"])
        self.assertEqual(stage["meta"]["constraints"]["exact_title"], "")

    def test_chained_code_exclusion_does_not_exclude_a_paragraph_match(self):
        self.search_fixture([page("accepted"), page("excluded")], {
            "accepted": [self.paragraph("a", "deprecated 안내"),
                         {"id": "c1", "type": "code", "code": {"rich_text": [{"plain_text": "needle();"}]}}],
            "excluded": [{"id": "c2", "type": "code", "code": {"rich_text": [{"plain_text": "needle(deprecated);"}]}}],
        })
        query = '노션 코드에 "needle" 포함하고 "deprecated" 없는 문서 찾아줘'
        stage = run_retrieval_pipeline(query, 5, {"notion": True})["stages"][0]
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["accepted"])

    def test_multiple_chained_table_conditions_preserve_the_explicit_title(self):
        query = '노션 "운영 규칙" 문서 중 표에 "return_value" 포함하고 "deprecated" 제외하고 "result" 포함한 문서 찾아줘'
        _subject, clauses = split_notion_body_query(query)
        self.assertEqual([(clause["value"], clause["operator"], clause["block_type"]) for clause in clauses], [
            ("return_value", "contains", "table_row"),
            ("deprecated", "not_contains", "table_row"),
            ("result", "contains", "table_row"),
        ])
        self.assertEqual(notion_literal_title(query), "운영 규칙")

    def test_chained_body_words_inside_quoted_title_remain_literal(self):
        title = "본문에 needle 포함하고 deprecated 없는 규칙"
        query = f"노션 '{title}' 찾아줘"
        self.assertEqual(split_notion_body_query(query), (query, []))
        self.assertEqual(notion_literal_title(query), title)

    def test_chained_unassigned_owner_remains_a_property_condition(self):
        self.search_fixture([page("unassigned", owners=[]),
                             page("assigned", owners=[{"id": "person", "name": "김지은"}])], {
            "unassigned": [self.paragraph("a", "needle 담당자 미정")],
            "assigned": [self.paragraph("b", "needle 정상 절차")],
        })
        for owner_phrase in ("담당자 없는", "담당자가 없는"):
            with self.subTest(owner_phrase=owner_phrase):
                query = f'노션 본문에 "needle" 포함하고 {owner_phrase} 문서 찾아줘'
                stage = run_retrieval_pipeline(query, 5, {"notion": True})["stages"][0]
                self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["unassigned"])
                self.assertTrue(stage["meta"]["constraints"]["unassigned_owner"])

    def test_quoted_chained_owner_label_remains_a_body_literal(self):
        self.search_fixture([page("unassigned", owners=[]),
                             page("assigned", owners=[{"id": "person", "name": "김지은"}])], {
            "unassigned": [self.paragraph("a", "needle 담당자 미정")],
            "assigned": [self.paragraph("b", "needle 정상 절차")],
        })
        query = '노션 본문에 "needle" 포함하고 "담당자" 없는 문서 찾아줘'
        stage = run_retrieval_pipeline(query, 5, {"notion": True})["stages"][0]
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["assigned"])
        self.assertFalse(stage["meta"]["constraints"]["unassigned_owner"])

    def test_unquoted_structured_field_labels_do_not_inherit_body_scope(self):
        for field in ("상태", "마감일", "등록일", "제목", "고객사", "플랫폼", "owner", "status"):
            with self.subTest(field=field):
                query = f'노션 본문에 "needle" 포함하고 {field} 없는 문서 찾아줘'
                subject, clauses = split_notion_body_query(query)
                self.assertIn(f"{field} 없는", subject)
                self.assertEqual([clause["value"] for clause in clauses], ["needle"])

    def test_explicit_body_scope_still_accepts_unquoted_field_label(self):
        _subject, clauses = split_notion_body_query("노션 본문에 담당자 없는 문서 찾아줘")
        self.assertEqual([(clause["value"], clause["operator"]) for clause in clauses],
                         [("담당자", "not_contains")])

    def test_quoted_absence_word_in_code_is_preserved_alongside_owner_absence(self):
        self.search_fixture([page("wanted", owners=[]), page("wrong-code", owners=[]),
                             page("assigned", owners=[{"id": "person", "name": "김지은"}])], {
            identifier: [{"id": f"{identifier}-code", "type": "code",
                          "code": {"rich_text": [{"plain_text": text}]}}]
            for identifier, text in (("wanted", "// 없는 경우 처리"),
                                     ("wrong-code", "// 정상 처리"),
                                     ("assigned", "// 없는 경우 처리"))
        })
        query = '노션 코드에 "없는" 포함하고 담당자 없는 문서 찾아줘'
        stage = run_retrieval_pipeline(query, 5, {"notion": True})["stages"][0]
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["wanted"])

    def test_body_filter_count_is_deterministic_but_body_summary_is_not(self):
        self.assertTrue(has_deterministic_notion_list_intent('노션 본문에 "needle" 포함된 문서는 몇 개야'))
        self.assertTrue(has_deterministic_notion_list_intent('노션 needle 내용이 있는 문서 찾아줘'))
        self.assertFalse(has_deterministic_notion_list_intent('노션 본문에 "needle" 포함된 문서 내용을 요약해줘'))

    def test_body_words_inside_a_literal_title_remain_the_title(self):
        self.assertEqual(search.extract_notion_constraints("노션 '본문에 needle 포함한 가이드' 찾아줘")["exact_title"], "본문에 needle 포함한 가이드")

    def test_body_and_typed_conditions_reject_metadata_and_other_block_types(self):
        self.search_fixture([page("metadata", memo="needle"), page("paragraph"), page("code"), page("table")], {
            "metadata": [self.paragraph("a", "unrelated")],
            "paragraph": [self.paragraph("b", "needle")],
            "code": [{"id": "c", "type": "code", "code": {"rich_text": [{"plain_text": "needle"}]}}],
            "table": [{"id": "d", "type": "table_row", "table_row": {"cells": [[{"plain_text": "needle"}]]}}],
        })
        for scope, expected in (("본문", {"paragraph", "code", "table"}), ("코드", {"code"}), ("표", {"table"})):
            with self.subTest(scope=scope):
                stage = run_retrieval_pipeline(f"노션 {scope}에 needle 포함한 문서 찾아줘", 5, {"notion": True})["stages"][0]
                self.assertEqual({entry["id"] for entry in stage["notion_pages"]}, expected)

    def test_negative_body_does_not_exclude_a_metadata_only_match(self):
        self.search_fixture([page("target", memo="deprecated")], {"target": [self.paragraph("a", "needle")]})
        stage = run_retrieval_pipeline("노션 본문에 deprecated 없는 문서 찾아줘", 5, {"notion": True})["stages"][0]
        self.assertEqual([entry["id"] for entry in stage["notion_pages"]], ["target"])

    def test_body_literal_does_not_change_the_date_field(self):
        self.search_fixture([], {})
        for literal in ("마감", '"마감"'):
            stage = run_retrieval_pipeline(f"노션 본문에 {literal} 포함한 2026년 문서 찾아줘", 5, {"notion": True})["stages"][0]
            clauses = stage["meta"]["constraints"]["common_filter_clauses"]
            self.assertTrue(any(clause["field"] == "created_at" for clause in clauses))
            self.assertFalse(any(clause["field"] == "due_at" for clause in clauses))

    def test_body_filter_preserves_grounded_planner_owner_choices(self):
        self.search_fixture([page("first", owners=[{"id": "a", "name": "김지은"}]),
                             page("second", owners=[{"id": "b", "name": "홍길동"}])], {
            "first": [self.paragraph("a", "needle")], "second": [self.paragraph("b", "needle")],
        })
        query = "노션 담당자가 김지은 또는 홍길동인 문서 중 본문에 needle 포함한 문서 찾아줘"
        stage = run_retrieval_pipeline(query, 5, {"notion": True}, {
            "source": "notion", "grounding_text": query,
            "filter_clauses": [{"field": "owner", "operator": "in", "value": ["김지은", "홍길동"]}],
        })["stages"][0]
        self.assertEqual({entry["id"] for entry in stage["notion_pages"]}, {"first", "second"})

    def test_disabled_body_reads_cannot_establish_an_absence(self):
        self.search_fixture([page("target")], {"target": [self.paragraph("a", "needle")]})
        with patch.dict(os.environ, {"NOTION_FETCH_PAGE_BLOCKS": "false"}):
            stage = run_retrieval_pipeline("노션 본문에 needle 없는 문서 찾아줘", 5, {"notion": True})["stages"][0]
        self.assertEqual(stage["notion_pages"], [])
        self.assertFalse(stage["meta"]["search_complete"])
        self.assertIn("body_disabled", stage["meta"]["incomplete_reasons"])
