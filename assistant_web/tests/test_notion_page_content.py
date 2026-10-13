"""Typed block evidence and plain text must share one bounded source traversal."""
import os
import unittest
from unittest.mock import patch

from backend.notion import client
import test_notion_accuracy_regressions as fixtures


class NotionPageContentTests(unittest.TestCase):
    setUp = fixtures.NotionAccuracyRegressionTests.setUp
    search_fixture = fixtures.NotionAccuracyRegressionTests.search_fixture
    paragraph = staticmethod(fixtures.NotionAccuracyRegressionTests.paragraph)

    def test_content_preserves_table_code_types_and_plain_api_reuses_requests(self):
        self.search_fixture([], {
            "root": [self.paragraph("prose", "paragraph_token"),
                     {"id": "code", "type": "code", "has_children": False,
                      "code": {"rich_text": [{"plain_text": "code_token()"}], "language": "cpp"}},
                     {"id": "table", "type": "table", "has_children": True, "table": {"table_width": 1}}],
            "table": [{"id": "row", "type": "table_row", "has_children": False,
                       "table_row": {"cells": [[{"plain_text": "table_token"}]]}}],
        })
        content = client.fetch_notion_page_content("root", max_blocks=200)
        typed = {segment["block_id"]: segment for segment in content["segments"]}
        self.assertEqual({key: row["type"] for key, row in typed.items()},
                         {"prose": "paragraph", "code": "code", "row": "table_row"})
        self.assertEqual(typed["row"]["owner_page_id"], "root")
        self.assertIn("table_token", typed["row"]["text"])
        self.assertIn("code_token()", typed["code"]["text"])
        self.assertEqual(client.fetch_notion_page_blocks("root", max_blocks=200), content["text"])
        self.assertEqual(client.requests.get.call_count, 2)

    def test_nested_child_pages_keep_actual_owner_and_database_properties_are_not_segments(self):
        self.search_fixture([], {
            "root": [{"id": "child", "type": "child_page", "has_children": True,
                      "child_page": {"title": "Child title"}},
                     {"id": "db", "type": "child_database", "has_children": True,
                      "child_database": {"title": "Child database"}}],
            "child": [self.paragraph("child-prose", "child_body")],
            "db-page": [{"id": "db-code", "type": "code", "has_children": False,
                         "code": {"rich_text": [{"plain_text": "database_child_code()"}], "language": "cpp"}}],
        })
        ordinary_get = client.requests.get.side_effect

        def get(url, **kwargs):
            if "/databases/" in url:
                return fixtures.response({"id": "db", "parent": {"type": "page_id", "page_id": "root"}})
            return ordinary_get(url, **kwargs)

        db_page = fixtures.page("db-page", memo="property_only_token")
        with patch.object(client.requests, "get", side_effect=get), patch.object(client, "notion_post_json",
            return_value=fixtures.response({"results": [db_page], "has_more": False})):
            content = client.fetch_notion_page_content("root", max_blocks=200)
        typed = {segment["block_id"]: segment for segment in content["segments"]}
        self.assertEqual(set(typed), {"child-prose", "db-code"})
        self.assertEqual(typed["child-prose"]["page_id"], "child")
        self.assertEqual(typed["child-prose"]["owner_page_id"], "child")
        self.assertEqual(typed["db-code"]["owner_page_id"], "db-page")
        self.assertIn("property_only_token", content["text"])
        self.assertFalse(any("property_only_token" in segment["text"] for segment in content["segments"]))

    def test_cached_evidence_cannot_be_mutated_by_a_caller(self):
        self.search_fixture([], {"root": [self.paragraph("prose", "trusted_token")]})
        first = client.fetch_notion_page_content("root", max_blocks=200)
        first["text"] = "fabricated"
        first["segments"][0]["text"] = "fabricated"
        second = client.fetch_notion_page_content("root", max_blocks=200)
        self.assertEqual(second["text"], "trusted_token")
        self.assertEqual(second["segments"][0]["text"], "trusted_token")
        self.assertEqual(client.requests.get.call_count, 1)

    def test_disabled_content_read_returns_empty_evidence_without_requests(self):
        with patch.dict(os.environ, {"NOTION_FETCH_PAGE_BLOCKS": "false"}), patch.object(client.requests, "get") as get:
            self.assertEqual(client.fetch_notion_page_content("root"), {"text": "", "segments": []})
            self.assertEqual(client.fetch_notion_page_blocks("root"), "")
        get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
