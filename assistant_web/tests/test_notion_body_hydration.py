"""Exercise bounded body scanning/hydration against paginated raw Notion blocks."""
import unittest
from unittest.mock import patch

from backend.notion import client, search
import test_notion_accuracy_regressions as fixtures


class NotionBodyHydrationTests(unittest.TestCase):
    setUp = fixtures.NotionAccuracyRegressionTests.setUp
    search_fixture = fixtures.NotionAccuracyRegressionTests.search_fixture
    paragraph = staticmethod(fixtures.NotionAccuracyRegressionTests.paragraph)

    def test_exact_title_finds_code_after_initial_twenty_blocks(self):
        rows = [fixtures.page("body-only")]
        rows[0]["properties"]["제목"]["title"] = [{"plain_text": "후반 코드 문서"}]
        self.search_fixture(rows, {
            "body-only": [self.paragraph(f"p-{i}", "운영 안내") for i in range(20)] + [
                {"id": "late-code", "type": "code", "has_children": False,
                 "code": {"rich_text": [{"plain_text": "latecode_token()"}], "language": "cpp"}}],
        })
        with patch.object(search, "notion_post_json", return_value=fixtures.response({"results": rows, "has_more": False})):
            result = search.execute_notion_search("노션 '후반 코드 문서' 찾아줘", limit=5,
                constraints={"filter_clauses": [{"field": "keyword", "operator": "contains", "value": "latecode_token"}]},
                include_blocks=False)
        self.assertEqual([page["id"] for page in result["notion_pages"]], ["body-only"])
        self.assertIn("latecode_token()", result["notion_pages"][0]["content"])
        self.assertTrue(result["meta"]["search_complete"])

    def test_partial_body_match_is_hydrated_and_only_content_completeness_is_repaired(self):
        self.search_fixture([fixtures.page("body-only")], {
            "body-only": [self.paragraph(f"p-{i}", "needle") for i in range(20)]
            + [self.paragraph("last", "later_context")],
        })
        result = search.execute_notion_search("노션 needle 문서 몇 개야", limit=5,
                                             planned_terms=["needle"], include_blocks=True)
        self.assertIn("later_context", result["notion_pages"][0]["content"])
        self.assertTrue(result["meta"]["content_complete"])
        self.assertEqual(result["meta"]["content_incomplete_page_ids"], [])
        self.assertFalse(result["notion_pages"][0].get("content_incomplete", False))
        self.assertNotIn(client.NOTION_CONTENT_INCOMPLETE, result["notion_pages"][0]["content"])
        self.assertFalse(result["meta"]["search_complete"])
        self.assertIn("body_limit", result["meta"]["incomplete_reasons"])

    def test_hydration_keeps_incomplete_when_result_body_budget_is_exhausted(self):
        self.search_fixture([fixtures.page("body-only")], {
            "body-only": [self.paragraph(f"p-{i}", "needle") for i in range(200)]
            + [self.paragraph("outside", "never_visible")],
        })
        result = search.execute_notion_search("노션 needle 문서 몇 개야", limit=5,
                                             planned_terms=["needle"], include_blocks=True)
        self.assertFalse(result["meta"]["content_complete"])
        self.assertEqual(result["meta"]["content_incomplete_page_ids"], ["body-only"])
        self.assertIn(client.NOTION_CONTENT_INCOMPLETE, result["notion_pages"][0]["content"])
        self.assertNotIn("never_visible", result["notion_pages"][0]["content"])

    def test_result_body_reads_fragmented_sections_beyond_twenty_requests(self):
        blocks = {"body-only": [
            {"id": f"section-{i}", "type": "toggle", "has_children": True,
             "toggle": {"rich_text": [{"plain_text": f"Section {i}"}]}} for i in range(30)]}
        blocks.update({f"section-{i}": [self.paragraph(f"text-{i}", "section_text")] for i in range(29)})
        blocks["section-29"] = [{"id": "table", "type": "table", "has_children": True,
                                 "table": {"table_width": 1}}]
        blocks["table"] = [{"id": "late-row", "type": "table_row", "has_children": False,
                            "table_row": {"cells": [[{"plain_text": "deptName"}]]}}]
        self.search_fixture([fixtures.page("body-only")], blocks)
        text = client.fetch_notion_page_blocks("body-only", max_blocks=200)
        self.assertIn("deptName", text)
        self.assertNotIn(client.NOTION_CONTENT_INCOMPLETE, text)

    def test_fragmented_result_request_cap_remains_incomplete(self):
        blocks = {"body-only": [
            {"id": f"section-{i}", "type": "toggle", "has_children": True,
             "toggle": {"rich_text": [{"plain_text": f"Section {i}"}]}} for i in range(70)]}
        blocks.update({f"section-{i}": [self.paragraph(f"text-{i}", f"section_text_{i}")] for i in range(70)})
        self.search_fixture([fixtures.page("body-only")], blocks)
        text = client.fetch_notion_page_blocks("body-only", max_blocks=200)
        self.assertIn(client.NOTION_CONTENT_INCOMPLETE, text)
        self.assertNotIn("section_text_69", text)
        self.assertLessEqual(client.requests.get.call_count, 60)

    def test_content_beyond_supported_depth_stays_incomplete(self):
        blocks = {"body-only": [{"id": "section-0", "type": "toggle", "has_children": True,
                                 "toggle": {"rich_text": [{"plain_text": "Start"}]}}]}
        blocks.update({f"section-{i}": [{"id": f"section-{i+1}", "type": "toggle", "has_children": True,
                        "toggle": {"rich_text": [{"plain_text": "Nested"}]}}] for i in range(6)})
        blocks["section-6"] = [self.paragraph("too-deep", "never_visible")]
        self.search_fixture([fixtures.page("body-only")], blocks)
        text = client.fetch_notion_page_blocks("body-only", max_blocks=200)
        self.assertIn(client.NOTION_CONTENT_INCOMPLETE, text)
        self.assertNotIn("never_visible", text)

    def test_short_complete_body_scan_keeps_its_content_without_hydration(self):
        self.search_fixture([fixtures.page("body-only")], {"body-only": [self.paragraph("p", "needle complete")]})
        result = search.execute_notion_search("노션 needle 문서 몇 개야", limit=5,
                                             planned_terms=["needle"], include_blocks=True)
        self.assertTrue(result["meta"]["content_complete"])
        self.assertIn("needle complete", result["notion_pages"][0]["content"])
        self.assertEqual(client.requests.get.call_count, 1)


if __name__ == "__main__":
    unittest.main()
