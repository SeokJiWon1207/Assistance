"""본문 색인의 전체 순회, 재개 및 실시간 재검증 계약."""

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from backend.notion import body_index_sync as sync
from backend.notion import body_index
from backend.notion.body_index import BodyIndex


def page(identifier="page", *, revision="2026-10-01T00:00:00Z", database="db", title=""):
    return {
        "id": identifier, "object": "page", "parent": {"type": "database_id", "database_id": database},
        "last_edited_time": revision, "archived": False, "in_trash": False,
        "properties": {"제목": {"type": "title", "title": [{"plain_text": title}] if title else []}},
    }


def paragraph(identifier, text):
    return {"id": identifier, "type": "paragraph", "has_children": False,
            "paragraph": {"rich_text": [{"plain_text": text}]}}


def response(data, status=200):
    result = Mock(status_code=status, headers={})
    result.json.return_value = data
    return result


class BodyIndexSyncTests(unittest.TestCase):
    def setUp(self):
        self.now = sync.time.monotonic()
        clock = patch.object(sync.time, "monotonic", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        cooldown = patch.object(sync, "_retry_after_until", 0.0, create=True)
        cooldown.start()
        self.addCleanup(cooldown.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.index = BodyIndex(Path(self.directory.name) / "bodies.sqlite3", fingerprint="test")
        self.rows = [page()]
        self.blocks = {"page": [paragraph("p", "본문") ]}
        self.revisions = {}
        self.parent_objects = {}
        self.events = []
        self.catalog_cursors = []
        self.catalog_has_more = False
        self.get_errors = []
        for name, value in (
            ("get_notion_settings", {"token": "unit", "database_ids": ["db"]}),
            ("resolve_notion_read_scope", ({"db": "db"}, True)),
            ("get_notion_headers", {"Authorization": "Bearer unit"}),
        ):
            patcher = patch.object(sync, name, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, side_effect in (("requests.get", self.get), ("requests.post", self.post)):
            patcher = patch.object(sync.requests, target.split(".")[-1], side_effect=side_effect)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ("notion_request_slot", "interruptible_sleep"):
            patcher = patch.object(sync, name)
            mocked = patcher.start()
            if name == "interruptible_sleep":
                mocked.side_effect = self.advance_clock
            self.addCleanup(patcher.stop)

    def advance_clock(self, seconds):
        self.now += seconds

    def get(self, url, **kwargs):
        path = url.split("/v1/")[-1]
        self.events.append(path)
        if self.get_errors:
            return self.get_errors.pop(0)
        if path.startswith("pages/"):
            identifier = path.split("/")[1]
            revisions = self.revisions.get(identifier)
            if revisions:
                return response(revisions.pop(0) if len(revisions) > 1 else revisions[0])
            return response(next(row for row in self.rows if row["id"] == identifier))
        identifier = path.split("/")[1]
        if not path.endswith("/children"):
            return response(self.parent_objects.get(identifier, {}))
        start = int((kwargs.get("params") or {}).get("start_cursor") or 0)
        all_blocks = self.blocks.get(identifier, [])
        batch = all_blocks[start:start + 100]
        return response({"results": batch, "has_more": start + 100 < len(all_blocks),
                         "next_cursor": str(start + 100)})

    def post(self, url, **kwargs):
        self.events.append(url.split("/v1/")[-1])
        payload = kwargs["json"]
        self.catalog_cursors.append(payload.get("start_cursor"))
        start = int(payload.get("start_cursor") or 0)
        return response({"results": self.rows[start:start + 100],
                         "has_more": self.catalog_has_more or start + 100 < len(self.rows),
                         "next_cursor": str(start if self.catalog_has_more else start + 100)})

    def test_button_is_separate_from_supported_text_completion(self):
        self.blocks["page"] += [
            {"id": "button", "type": "unsupported", "unsupported": {"block_type": "button"}},
            {"id": "code", "type": "code", "code": {"rich_text": [{"plain_text": "run()"}]}},
            {"id": "row", "type": "table_row", "table_row": {"cells": [[{"plain_text": "cell"}]]}},
        ]
        self.index.upsert_catalog("db", self.rows, complete=True)
        result = sync.validate_index_candidate({"page_id": "page", "database_id": "db"}, ["db"], index=self.index)
        self.assertTrue(result["supported_content_complete"])
        self.assertFalse(result["complete"])
        status = self.index.status(["db"])
        self.assertEqual(status["supported_collected_page_count"], 1)
        self.assertEqual(status["unsupported_element_page_count"], 1)
        self.assertEqual(status["unsupported_element_counts"], {"button": 1})
        self.assertEqual(status["supported_segment_counts"], {"body": 1, "table": 1, "code": 1})
        self.assertEqual(status["indexed_page_count"], 0)
        self.assertEqual(status["api_limitation_only_page_count"], 1)
        self.assertEqual(status["collection_error_page_count"], 0)
        # A later failed attempt must not leave a stale successful coverage assertion.
        self.index.record_failure("page", "http_429")
        self.assertEqual(self.index.status(["db"])["supported_collected_page_count"], 0)
        self.assertEqual(self.index.status(["db"])["transient_error_page_count"], 1)

    def test_button_with_traversal_error_is_not_supported_complete(self):
        self.blocks["page"] = [{"id": "button", "type": "unsupported",
            "unsupported": {"block_type": "button"},
            "parent": {"type": "page_id", "page_id": "other"}}]
        content = sync.crawl_notion_page_content("page")
        self.assertFalse(content["supported_content_complete"])

    def test_unsupported_container_does_not_certify_unread_children(self):
        self.blocks["page"] = [{"id": "opaque", "type": "unsupported", "has_children": True,
                                 "unsupported": {"block_type": "form"}}]
        result = sync.crawl_notion_page_content("page")
        self.assertFalse(result["supported_content_complete"])
        self.assertIn("unsupported_child_content", result["incomplete_reasons"])

    def _synced_source(self, database="db"):
        self.rows.append(page("sourcepage", database=database))
        copy = {"id": "copy", "object": "block", "type": "synced_block", "has_children": True,
                "parent": {"type": "page_id", "page_id": "page"},
                "synced_block": {"synced_from": {"block_id": "source"}}}
        self.blocks["page"] = [copy]
        self.parent_objects["copy"] = copy
        self.parent_objects["source"] = {
            "id": "source", "object": "block", "type": "synced_block", "has_children": True,
            "last_edited_time": "2026-10-01T00:00:00Z",
            "parent": {"type": "page_id", "page_id": "sourcepage"},
            "synced_block": {"synced_from": None}}
        self.blocks["source"] = [{**paragraph("sourcebody", "synced_unique_token"),
                                  "parent": {"type": "block_id", "block_id": "source"}}]

    def test_allowed_synced_source_is_searchable_with_real_owner_provenance(self):
        self._synced_source()
        self.index.upsert_catalog("db", self.rows, complete=True)
        result = sync.validate_index_candidate({"page_id": "page", "database_id": "db"}, ["db"], index=self.index)
        self.assertTrue(result["complete"])
        evidence = result["segments"][0]
        self.assertEqual(evidence["owner_page_id"], "sourcepage")
        self.assertEqual(evidence["display_page_id"], "page")
        self.assertEqual(evidence["synced_source_id"], "source")
        self.assertEqual(evidence["synced_copy_id"], "copy")
        candidates = self.index.search(["db"], ["synced_unique_token"])
        self.assertEqual([row["page_id"] for row in candidates], ["page"])
        self.assertEqual(candidates[0]["segments"][0]["owner_page_id"], "sourcepage")
        from backend.notion.body_index import segment_belongs_to_page
        self.assertTrue(segment_belongs_to_page(evidence, "page"))
        self.assertFalse(segment_belongs_to_page({**evidence, "synced_source_id": ""}, "page"))

    def test_synced_source_outside_scope_is_not_read(self):
        self._synced_source(database="outside")
        result = sync.validate_index_candidate({"page_id": "page", "database_id": "db"}, ["db"])
        self.assertFalse(result["complete"])
        self.assertNotIn("synced_unique_token", result["content"])
        self.assertNotIn("blocks/source/children", self.events)
        self.assertTrue(result["accessible_content_complete"])

    def test_synced_source_moved_or_changed_during_read_is_discarded(self):
        self._synced_source()
        self.revisions["sourcepage"] = [page("sourcepage"), page("sourcepage", database="outside")]
        result = sync.validate_index_candidate({"page_id": "page", "database_id": "db"}, ["db"])
        self.assertFalse(result["complete"])
        self.assertNotIn("synced_unique_token", result["content"])

    def test_synced_source_children_require_matching_parent_proof(self):
        self._synced_source()
        self.blocks["source"][0].pop("parent")
        result = sync.validate_index_candidate({"page_id": "page", "database_id": "db"}, ["db"])
        self.assertFalse(result["complete"])
        self.assertNotIn("synced_unique_token", result["content"])

    def test_synced_copy_changed_during_read_discards_original_text(self):
        self._synced_source()
        self.parent_objects["copy"] = {**self.parent_objects["copy"],
                                      "synced_block": {"synced_from": {"block_id": "other"}}}
        result = sync.validate_index_candidate({"page_id": "page", "database_id": "db"}, ["db"])
        self.assertIn("synced_copy_changed", result["incomplete_reasons"])
        self.assertNotIn("synced_unique_token", result["content"])

    def test_synced_source_permission_denied_preserves_local_text(self):
        self._synced_source()
        self.blocks["page"].append(paragraph("local", "local_token"))
        original_get = self.get
        def get(url, **kwargs):
            return response({}, 404) if url.endswith("/blocks/source") else original_get(url, **kwargs)
        with patch.object(sync.requests, "get", side_effect=get):
            result = sync.validate_index_candidate({"page_id": "page", "database_id": "db"}, ["db"])
        self.assertIn("http_404", result["incomplete_reasons"])
        self.assertEqual(result["content"], "local_token")
        self.assertTrue(result["accessible_content_complete"])
        self.assertFalse(result["supported_content_complete"])

    def test_copy_container_moved_without_copy_parent_change_is_rejected(self):
        self._synced_source()
        copy = self.blocks["page"][0]
        copy["parent"] = {"type": "block_id", "block_id": "container"}
        self.blocks["page"] = [{"id": "container", "type": "toggle", "has_children": True,
                                 "parent": {"type": "page_id", "page_id": "page"}}]
        self.blocks["container"] = [copy]
        self.parent_objects["container"] = {"id": "container", "object": "block",
            "parent": {"type": "page_id", "page_id": "sourcepage"}}
        result = sync.validate_index_candidate({"page_id": "page", "database_id": "db"}, ["db"])
        self.assertFalse(result["complete"])
        self.assertNotIn("synced_unique_token", result["content"])

    def test_synced_source_permission_lost_after_traversal_discards_text(self):
        self._synced_source()
        original_get = self.get
        calls = 0
        def get(url, **kwargs):
            nonlocal calls
            if url.endswith("/blocks/source"):
                calls += 1
                if calls == 2:
                    return response({}, 403)
            return original_get(url, **kwargs)
        with patch.object(sync.requests, "get", side_effect=get):
            result = sync.validate_index_candidate({"page_id": "page", "database_id": "db"}, ["db"])
        self.assertFalse(result["complete"])
        self.assertNotIn("synced_unique_token", result["content"])

    def test_synced_copy_without_parent_proof_is_not_followed(self):
        self._synced_source()
        self.blocks["page"][0].pop("parent")
        result = sync.validate_index_candidate({"page_id": "page", "database_id": "db"}, ["db"])
        self.assertFalse(result["complete"])
        self.assertNotIn("blocks/source/children", self.events)

    def test_retry_after_is_not_truncated_to_ten_seconds(self):
        limited = response({}, 429)
        limited.headers = {"Retry-After": "30"}
        with patch.object(sync.requests, "get", side_effect=[limited, response({"ok": True})]):
            result = sync._request("pages/page", sync._Budget(sync.time.monotonic() + 60, 3))
        self.assertEqual(result, {"ok": True})
        sync.interruptible_sleep.assert_called_once_with(30)

    def test_retry_after_beyond_budget_does_not_retry_early(self):
        limited = response({}, 429)
        limited.headers = {"Retry-After": "30"}
        with patch.object(sync.requests, "get", return_value=limited) as get:
            with self.assertRaisesRegex(sync._ReadFailure, "http_429"):
                sync._request("pages/page", sync._Budget(sync.time.monotonic() + 20, 3))
        self.assertEqual(get.call_count, 1)
        sync.interruptible_sleep.assert_not_called()

    def test_retry_after_survives_page_deadline_and_delays_next_page(self):
        limited = response({}, 429)
        limited.headers = {"Retry-After": "30"}
        sent = []
        start = self.now

        def server(url, **kwargs):
            sent.append((url.rsplit("/", 1)[-1], self.now - start))
            return limited if len(sent) == 1 else response({"ok": True})

        with patch.object(sync.requests, "get", side_effect=server):
            with self.assertRaisesRegex(sync._ReadFailure, "http_429"):
                sync._request("pages/first", sync._Budget(start + 10, 3))
            with self.assertRaisesRegex(sync._ReadFailure, "http_429"):
                sync._request("pages/second", sync._Budget(start + 20, 3))
            result = sync._request("pages/second", sync._Budget(start + 60, 3))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(sent, [("first", 0), ("second", 30)])

    def test_final_rate_limited_attempt_preserves_retry_after_for_next_page(self):
        sent = []
        start = self.now

        def server(url, **kwargs):
            sent.append((url.rsplit("/", 1)[-1], self.now - start))
            if len(sent) <= 3:
                limited = response({}, 429)
                limited.headers = {"Retry-After": "30" if len(sent) == 3 else "1"}
                return limited
            return response({"ok": True})

        with patch.object(sync.requests, "get", side_effect=server):
            with self.assertRaisesRegex(sync._ReadFailure, "http_429"):
                sync._request("pages/first", sync._Budget(start + 100, 10))
            result = sync._request("pages/second", sync._Budget(start + 100, 3))
        self.assertEqual(result, {"ok": True})
        self.assertEqual(sent, [("first", 0), ("first", 1), ("first", 2), ("second", 32)])

    def test_batch_leaves_unattempted_pages_pending_when_cooldown_exceeds_deadline(self):
        from backend.notion.snapshot_maintenance import refresh_snapshot
        self.rows = [page(identifier) for identifier in ("first", "second", "third")]
        self.index.upsert_catalog("db", self.rows, complete=True)
        limited = response({}, 429)
        limited.headers = {"Retry-After": "30"}
        self.get_errors = [limited]
        result = refresh_snapshot(self.index, ["db"], max_seconds=10, refresh_catalog=False)
        self.assertEqual(result["attempted_page_count"], 1)
        self.assertEqual(result["pending_page_count"], 3)
        with self.index._database() as db:
            failures = db.execute("SELECT COUNT(*) FROM pages WHERE failure_count > 0").fetchone()[0]
        self.assertEqual(failures, 1)

    def test_crawler_reads_more_than_two_hundred_blocks_and_nested_table(self):
        self.blocks["page"] = [paragraph(str(i), "ordinary") for i in range(250)] + [
            {"id": "toggle", "type": "toggle", "has_children": True,
             "toggle": {"rich_text": [{"plain_text": "Details"}]}}]
        self.blocks["toggle"] = [{"id": "table", "type": "table", "has_children": True, "table": {}}]
        self.blocks["table"] = [{"id": "row", "type": "table_row", "has_children": False,
                                  "table_row": {"cells": [[{"plain_text": "late_table_token"}]]}}]
        content = sync.crawl_notion_page_content("page")
        self.assertTrue(content["complete"])
        self.assertIn("late_table_token", content["text"])
        self.assertEqual(content["segments"][-1]["owner_page_id"], "page")
        self.assertEqual(content["segments"][-1]["type"], "table_row")

    def test_child_pages_and_foreign_synced_blocks_are_not_parent_evidence(self):
        self.blocks["page"] = [
            {"id": "child", "type": "child_page", "has_children": True, "child_page": {"title": "secret"}},
            {"id": "db-child", "type": "child_database", "has_children": True, "child_database": {"title": "secret"}},
            {"id": "synced", "type": "synced_block", "has_children": True,
             "synced_block": {"synced_from": {"block_id": "external"}}},
            paragraph("own", "own text"),
        ]
        content = sync.crawl_notion_page_content("page")
        self.assertEqual(content["text"], "own text")
        self.assertFalse(content["complete"])
        self.assertIn("unsupported_child_pages", content["incomplete_reasons"])
        self.assertIn("foreign_synced_block", content["incomplete_reasons"])
        self.assertEqual(content["unsupported_child_page_ids"], ["child"])
        self.assertEqual(self.events, ["blocks/page/children"])

    def test_block_limit_and_repeated_cursor_mark_content_incomplete(self):
        self.blocks["page"] = [paragraph(str(i), "text") for i in range(5)]
        content = sync.crawl_notion_page_content("page", limits=sync.CrawlLimits(max_blocks=2))
        self.assertFalse(content["complete"])
        self.assertEqual(len(content["segments"]), 2)
        with patch.object(sync.requests, "get", return_value=response({
            "results": [paragraph("x", "text")], "has_more": True, "next_cursor": "loop",
        })):
            content = sync.crawl_notion_page_content("page")
        self.assertFalse(content["complete"])
        self.assertIn("invalid_cursor", content["incomplete_reasons"])

    def test_same_page_synced_reference_to_collected_original_is_complete(self):
        self.blocks["page"] = [
            {"id": "copy", "type": "synced_block", "has_children": True,
             "synced_block": {"synced_from": {"block_id": "original"}}},
            {"id": "original", "type": "synced_block", "has_children": True,
             "parent": {"type": "page_id", "page_id": "page"},
             "synced_block": {"synced_from": None}},
        ]
        self.blocks["original"] = [paragraph("text", "local_synced_evidence")]
        content = sync.crawl_notion_page_content("page")
        self.assertTrue(content["complete"])
        self.assertEqual(content["text"], "local_synced_evidence")
        self.assertNotIn("blocks/copy/children", self.events)
        # A source outside this page's validated tree must remain incomplete.
        self.blocks["page"][1]["parent"]["page_id"] = "outside"
        content = sync.crawl_notion_page_content("page")
        self.assertFalse(content["complete"])
        self.assertIn("foreign_synced_block", content["incomplete_reasons"])
        self.assertNotIn("local_synced_evidence", content["text"])

    def test_same_page_synced_original_read_failure_stays_incomplete(self):
        self.blocks["page"] = [
            {"id": "copy", "type": "synced_block", "has_children": True,
             "synced_block": {"synced_from": {"block_id": "original"}}},
            {"id": "original", "type": "synced_block", "has_children": True,
             "synced_block": {"synced_from": None}},
        ]
        content = sync.crawl_notion_page_content("page", limits=sync.CrawlLimits(max_depth=0))
        self.assertFalse(content["complete"])
        self.assertIn("depth_limit", content["incomplete_reasons"])

    def test_unsupported_block_cannot_certify_complete_body_or_negative(self):
        self.blocks["page"] = [paragraph("own", "known text"), {
            "id": "opaque", "object": "block", "type": "unsupported",
            "parent": {"type": "page_id", "page_id": "page"},
            "has_children": False, "unsupported": {},
        }]
        result = sync.sync_notion_body_index(index=self.index)
        self.assertFalse(result["content_complete"])
        self.assertEqual(result["failed_page_count"], 1)
        self.assertIn("unsupported_block", result["incomplete_reasons"])
        content = sync.crawl_notion_page_content("page")
        self.assertEqual(content["text"], "known text")
        self.assertFalse(content["complete"])

    def test_unsupported_leaf_alias_parent_is_not_a_supported_body_failure(self):
        self.blocks["page"] = [paragraph("own", "known text"), {
            "id": "alias", "object": "block", "type": "unsupported",
            "parent": {"type": "page_id", "page_id": "other"},
            "has_children": False, "unsupported": {"block_type": "alias"},
        }]
        content = sync.crawl_notion_page_content("page")
        self.assertTrue(content["supported_content_complete"])
        self.assertFalse(content["complete"])
        self.assertEqual(content["incomplete_reasons"], ["unsupported_block"])
        self.assertEqual(content["text"], "known text")
        self.assertNotIn("blocks/other/children", self.events)
        self.blocks["page"][1]["has_children"] = True
        self.assertFalse(sync.crawl_notion_page_content("page")["supported_content_complete"])

    def test_unsupported_moved_block_keeps_both_incomplete_reasons(self):
        self.blocks["page"] = [{
            "id": "opaque", "object": "block", "type": "unsupported",
            "parent": {"type": "page_id", "page_id": "other"},
            "has_children": False, "unsupported": {},
        }]
        content = sync.crawl_notion_page_content("page")
        self.assertEqual(set(content["incomplete_reasons"]),
                         {"unsupported_block", "block_parent_mismatch"})

    def test_catalog_keeps_blank_titles_and_batch_resumes_pending_pages(self):
        self.rows = [page("first"), page("second")]
        self.blocks = {"first": [paragraph("a", "alpha")], "second": [paragraph("b", "beta")]}
        first = sync.sync_notion_body_index(index=self.index, max_pages=1)
        self.assertEqual(first["page_count"], 2)
        self.assertEqual(first["indexed_page_count"], 1)
        self.assertEqual(first["pending_page_count"], 1)
        second = sync.sync_notion_body_index(index=self.index, max_pages=1)
        self.assertEqual(second["indexed_page_count"], 2)
        self.assertEqual(second["pending_page_count"], 0)
        self.assertTrue(second["complete"])
        self.assertEqual(self.events.count("blocks/first/children"), 1)
        self.assertEqual(self.events.count("blocks/second/children"), 1)

    def test_catalog_paginates_before_reading_any_bodies(self):
        self.rows = [page(f"p{i}") for i in range(101)]
        result = sync.sync_notion_body_index(index=self.index, max_pages=1)
        self.assertEqual(result["page_count"], 101)
        self.assertEqual(self.catalog_cursors, [None, "100"])
        self.assertEqual(self.events[:2], ["databases/db/query", "databases/db/query"])

    def test_wiki_catalog_indexes_nested_page_with_verified_ancestry(self):
        child = page("child")
        child["parent"] = {"type": "page_id", "page_id": "parent"}
        self.rows = [child, page("parent")]
        self.blocks = {"child": [paragraph("nested-block", "nested_wiki_token")]}
        result = sync.sync_notion_body_index(index=self.index, max_pages=10)
        self.assertTrue(result["catalog_complete"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["indexed_page_count"], 2)
        matches = self.index.search(["db"], ["nested_wiki_token"])
        self.assertEqual([row["page_id"] for row in matches], ["child"])
        self.assertEqual(matches[0]["raw_page"]["parent"], child["parent"])
        self.assertEqual(matches[0]["segments"][0]["owner_page_id"], "child")

    def test_wiki_candidate_rechecks_ancestor_after_body_read(self):
        child = page("child")
        child["parent"] = {"type": "page_id", "page_id": "parent"}
        self.rows = [child, page("parent")]
        self.blocks = {"child": [paragraph("nested-block", "nested_wiki_token")]}
        record = {"page_id": "child", "database_id": "db", "raw_page": child}
        valid = sync.validate_index_candidate(record, ["db"], index=self.index)
        self.assertIsNotNone(valid)
        self.revisions["parent"] = [page("parent"), page("parent", database="outside")]
        rejected = sync.validate_index_candidate(record, ["db"], index=self.index)
        self.assertIsNone(rejected)
        self.assertFalse(self.index.read_page("child")["trusted"])

    def test_wiki_outside_cyclic_and_archived_ancestors_fail_closed(self):
        child = page("child")
        child["parent"] = {"type": "page_id", "page_id": "parent"}
        cycle = {**page("parent"), "parent": {"type": "page_id", "page_id": "child"}}
        for ancestor in (page("parent", database="outside"), cycle,
                         {**page("parent"), "archived": True}):
            with self.subTest(ancestor=ancestor):
                self.rows = [child, ancestor]
                result = sync.sync_notion_body_index(index=self.index, max_pages=0)
                self.assertFalse(result["catalog_complete"])
                self.assertIsNone(self.index.read_page("child"))
                self.assertIsNone(sync.validate_index_candidate(
                    {"page_id": "child", "database_id": "db"}, ["db"], index=self.index))

    def test_wiki_catalog_skips_separately_allowed_child_database_objects(self):
        self.rows = [page(), {"object": "database", "id": "nesteddb",
                              "parent": {"type": "page_id", "page_id": "page"}}]
        with patch.object(sync, "resolve_notion_read_scope", return_value=({"db": "db", "nesteddb": "db"}, True)):
            result = sync.sync_notion_body_index(["db"], index=self.index, max_pages=0)
        self.assertTrue(result["catalog_complete"])
        self.assertEqual(result["page_count"], 1)
        self.assertIsNone(self.index.read_page("nesteddb"))

    def test_wiki_unknown_child_database_does_not_claim_complete_catalog(self):
        self.rows = [page(), {"object": "database", "id": "undiscovered",
                      "parent": {"type": "page_id", "page_id": "page"}}]
        result = sync.sync_notion_body_index(index=self.index, max_pages=0)
        self.assertFalse(result["catalog_complete"])
        self.assertIn("child_database_discovery", result["incomplete_reasons"])

    def test_nested_children_become_independent_pending_documents(self):
        child = {**page("child"), "parent": {"type": "page_id", "page_id": "page"}}
        self.revisions["child"] = [child]
        self.blocks["page"] = [paragraph("own", "parent_content"),
            {"id": "child", "type": "child_page", "has_children": True, "child_page": {"title": "Child"}}]
        self.blocks["child"] = [paragraph("child-block", "child_content")]
        first = sync.sync_notion_body_index(index=self.index)
        self.assertTrue(self.index.read_page("page")["trusted"])
        self.assertEqual(first["page_count"], 2)
        self.assertEqual(first["pending_page_count"], 1)
        self.assertEqual(self.index.search(["db"], ["child_content"]), [])
        second = sync.sync_notion_body_index(index=self.index, refresh_catalog=False)
        self.assertEqual(second["indexed_page_count"], 2)
        self.assertEqual(second["pending_page_count"], 0)
        self.assertEqual([x["page_id"] for x in self.index.search(["db"], ["child_content"])], ["child"])
        self.assertNotIn("child_content", self.index.read_page("page")["content"])

    def test_child_registration_rejects_foreign_parent_and_leaves_incomplete(self):
        self.revisions["child"] = [page("child", database="outside")]
        self.blocks["page"] = [{"id": "child", "type": "child_page", "has_children": True,
                                 "child_page": {"title": "Child"}}]
        result = sync.sync_notion_body_index(index=self.index)
        self.assertFalse(result["content_complete"])
        self.assertIsNone(self.index.read_page("child"))

    def test_catalog_refresh_preserves_discovered_child_until_ancestor_disappears(self):
        child = {**page("child"), "parent": {"type": "page_id", "page_id": "page"}}
        self.revisions["child"] = [child]
        self.blocks["page"] = [{"id": "child", "type": "child_page", "has_children": True,
                                 "child_page": {"title": "Child"}}]
        self.blocks["child"] = [paragraph("child-block", "child_content")]
        sync.sync_notion_body_index(index=self.index)
        sync.sync_notion_body_index(index=self.index, refresh_catalog=False)
        sync.sync_notion_body_index(index=self.index, max_pages=0)
        child_record = self.index.read_page("child")
        self.assertIsNotNone(child_record)
        self.assertTrue(child_record["trusted"])
        self.rows = []
        sync.sync_notion_body_index(index=self.index, max_pages=0)
        self.assertIsNone(self.index.read_page("child"))

    def test_wiki_pages_under_blocks_validate_full_parent_chain(self):
        child = {**page("child"), "parent": {"type": "block_id", "block_id": "callout"}}
        self.rows = [child, page("page")]
        self.parent_objects = {"callout": {"object": "block", "id": "callout", "type": "callout",
                                          "parent": {"type": "page_id", "page_id": "page"}}}
        self.blocks["child"] = [paragraph("nested", "block_nested_token")]
        result = sync.sync_notion_body_index(index=self.index)
        self.assertTrue(result["complete"])
        self.assertEqual([r["page_id"] for r in self.index.search(["db"], ["block_nested_token"])], ["child"])

    def test_discovered_page_under_block_survives_catalog_refresh(self):
        child = {**page("child"), "parent": {"type": "block_id", "block_id": "callout"}}
        self.revisions["child"] = [child]
        self.parent_objects = {"callout": {"object": "block", "id": "callout", "type": "callout",
                                          "parent": {"type": "page_id", "page_id": "page"}}}
        self.blocks["page"] = [{**self.parent_objects["callout"], "has_children": True, "callout": {}}]
        self.blocks["callout"] = [{"id": "child", "type": "child_page", "has_children": True,
                                    "child_page": {"title": "Child"}}]
        self.blocks["child"] = [paragraph("nested", "block_nested_token")]
        sync.sync_notion_body_index(index=self.index)
        sync.sync_notion_body_index(index=self.index, refresh_catalog=False)
        result = sync.sync_notion_body_index(index=self.index, max_pages=0)
        self.assertTrue(result["complete"])
        self.assertEqual([r["page_id"] for r in self.index.search(["db"], ["block_nested_token"])], ["child"])
        self.rows = []
        sync.sync_notion_body_index(index=self.index, max_pages=0)
        self.assertIsNone(self.index.read_page("child"))

    def test_incomplete_catalog_does_not_prune_existing_pages(self):
        self.index.upsert_catalog("db", [page("old")], complete=True)
        self.catalog_has_more = True
        result = sync.sync_notion_body_index(index=self.index, max_pages=0)
        self.assertIsNotNone(self.index.read_page("old"))
        self.assertFalse(result["catalog_complete"])
        self.assertIn("invalid_cursor", result["incomplete_reasons"])

    def test_unconfigured_database_is_rejected_before_any_request(self):
        with self.assertRaises(ValueError):
            sync.sync_notion_body_index(["outside"], index=self.index)
        self.assertEqual(self.events, [])

    def test_same_revision_candidate_still_reads_current_body(self):
        self.index.upsert_catalog("db", self.rows, complete=True)
        self.index.replace_content("page", [
            {"block_id": "late", "type": "code", "text": "beyond_200()", "owner_page_id": "page"},
        ], complete=True, expected_edited_time=self.rows[0]["last_edited_time"])
        self.blocks["page"] = [paragraph("now", "same_minute_current_text")]
        result = sync.validate_index_candidate(self.index.read_page("page"), ["db"], index=self.index)
        self.assertEqual(result["content"], "same_minute_current_text")
        self.assertTrue(result["complete"])
        self.assertEqual(self.events, ["pages/page", "blocks/page/children", "pages/page"])

    def test_changed_candidate_recrawls_and_replaces_stale_content(self):
        self.index.upsert_catalog("db", self.rows, complete=True)
        self.index.replace_content("page", [
            {"block_id": "old", "type": "paragraph", "text": "stale", "owner_page_id": "page"},
        ], complete=True, expected_edited_time=self.rows[0]["last_edited_time"])
        record = self.index.read_page("page")
        self.rows = [page(revision="2026-10-02T00:00:00Z")]
        self.blocks["page"] = [paragraph("new", "current")]
        result = sync.validate_index_candidate(record, ["db"], index=self.index)
        self.assertEqual(result["content"], "current")
        self.assertTrue(result["complete"])
        self.assertEqual(self.index.read_page("page")["content"], "current")

    def test_archived_moved_and_inaccessible_candidates_are_rejected(self):
        self.index.upsert_catalog("db", self.rows, complete=True)
        record = self.index.read_page("page")
        for raw in ({**page(), "archived": True}, page(database="outside")):
            with self.subTest(raw=raw):
                self.rows = [raw]
                self.assertIsNone(sync.validate_index_candidate(record, ["db"], index=self.index))
        self.get_errors = [response({}, 404)]
        self.assertIsNone(sync.validate_index_candidate(record, ["db"], index=self.index))

    def test_revision_changes_during_crawl_cannot_be_complete_evidence(self):
        self.index.upsert_catalog("db", self.rows, complete=True)
        self.revisions["page"] = [page(), page(revision="2026-10-03T00:00:00Z")]
        result = sync.validate_index_candidate(self.index.read_page("page"), ["db"], index=self.index)
        self.assertIsNone(result)
        self.assertFalse(self.index.read_page("page")["trusted"])

    def test_post_crawl_page_read_failure_discards_unverifiable_evidence(self):
        self.index.upsert_catalog("db", self.rows, complete=True)
        real_get = self.get

        def fail_after(url, **kwargs):
            if url.endswith("/pages/page") and "blocks/page/children" in self.events:
                return response({}, 403)
            return real_get(url, **kwargs)

        with patch.object(sync.requests, "get", side_effect=fail_after):
            result = sync.validate_index_candidate(self.index.read_page("page"), ["db"], index=self.index)
        self.assertIsNone(result)
        self.assertFalse(self.index.read_page("page")["trusted"])

    def test_request_deadline_and_depth_limit_do_not_claim_complete_content(self):
        content = sync.crawl_notion_page_content("page", deadline=sync.time.monotonic() - 1)
        self.assertFalse(content["complete"])
        self.assertIn("time_limit", content["incomplete_reasons"])
        self.assertEqual(self.events, [])
        self.blocks["page"] = [{"id": "toggle", "type": "toggle", "has_children": True,
                                 "toggle": {"rich_text": []}}]
        content = sync.crawl_notion_page_content("page", limits=sync.CrawlLimits(max_depth=0))
        self.assertFalse(content["complete"])
        self.assertIn("depth_limit", content["incomplete_reasons"])

    def test_successful_catalog_prunes_only_absent_pages(self):
        self.index.upsert_catalog("db", [page("deleted"), page()], complete=True)
        result = sync.sync_notion_body_index(index=self.index, max_pages=0)
        self.assertIsNone(self.index.read_page("deleted"))
        self.assertIsNotNone(self.index.read_page("page"))
        self.assertTrue(result["catalog_complete"])

    def test_resume_uses_existing_catalog_without_claiming_fresh_complete_scope(self):
        self.index.upsert_catalog("db", self.rows, complete=True)
        result = sync.sync_notion_body_index(index=self.index, refresh_catalog=False)
        self.assertEqual(result["indexed_page_count"], 1)
        self.assertFalse(result["complete"])
        self.assertFalse(result["catalog_refreshed"])
        self.assertIn("catalog_not_refreshed", result["incomplete_reasons"])
        self.assertEqual(self.catalog_cursors, [])

    def test_resume_rejects_missing_or_incomplete_catalog(self):
        with self.assertRaises(ValueError):
            sync.sync_notion_body_index(index=self.index, refresh_catalog=False)
        self.assertEqual(self.events, [])

    def test_force_refresh_finds_same_revision_new_content(self):
        sync.sync_notion_body_index(index=self.index)
        self.blocks["page"] = [paragraph("new", "added_same_minute")]
        result = sync.sync_notion_body_index(index=self.index, refresh_content=True)
        self.assertEqual(result["completed_page_count"], 1)
        self.assertEqual(self.index.read_page("page")["content"], "added_same_minute")

    def test_cli_resume_outputs_statistics_without_document_text(self):
        self.index.upsert_catalog("db", self.rows, complete=True)
        spec = importlib.util.spec_from_file_location(
            "notion_body_sync_cli", Path(__file__).resolve().parents[1] / "scripts" / "sync_notion_body_index.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["sync", "--database-id", "db", "--resume-catalog", "--max-pages", "1"]),
            patch.object(cli, "sync_notion_body_index", side_effect=lambda *args, **kwargs:
                         sync.sync_notion_body_index(*args, index=self.index, **kwargs)),
            redirect_stdout(output),
        ):
            exit_code = cli.main()
        stats = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stats["completed_page_count"], 1)
        self.assertFalse(stats["catalog_refreshed"])
        self.assertNotIn("본문", output.getvalue())
        self.assertNotIn("Bearer", output.getvalue())
        self.assertEqual(self.catalog_cursors, [])

    def test_status_reports_pending_failures_without_network(self):
        self.index.upsert_catalog("db", [page("first"), page("second")], complete=True)
        self.index.record_failure("first")
        with patch.object(sync, "parse_notion_database_ids", return_value=["db"]):
            stats = sync.notion_body_index_status(index=self.index)
        self.assertEqual(stats["page_count"], 2)
        self.assertEqual(stats["pending_page_count"], 2)
        self.assertEqual(stats["failed_page_count"], 1)
        self.assertEqual(self.events, [])

    def test_status_does_not_create_missing_index_or_discover_database_scope(self):
        missing = Path(self.directory.name) / "does-not-exist.sqlite3"
        with (
            patch.object(sync, "parse_notion_database_ids", return_value=["db"]),
            patch.object(body_index, "body_index_path", return_value=missing),
        ):
            stats = sync.notion_body_index_status()
        self.assertFalse(stats["available"])
        self.assertFalse(missing.exists())
        with (
            patch.object(sync, "parse_notion_database_ids", return_value=[]),
            patch.object(sync, "get_notion_settings", side_effect=AssertionError("must not auto-discover")),
        ):
            stats = sync.notion_body_index_status()
        self.assertFalse(stats["available"])
        self.assertEqual(self.events, [])

    def test_cli_status_only_reads_coverage_for_requested_database(self):
        self.index.upsert_catalog("db", self.rows, complete=True)
        spec = importlib.util.spec_from_file_location(
            "notion_body_status_cli", Path(__file__).resolve().parents[1] / "scripts" / "sync_notion_body_index.py",
        )
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        output = io.StringIO()
        with (
            patch.object(sys, "argv", ["sync", "--status", "--database-id", "db"]),
            patch.object(sync, "parse_notion_database_ids", return_value=["db"]),
            patch.object(cli, "notion_body_index_status", side_effect=lambda *args, **kwargs:
                         sync.notion_body_index_status(*args, index=self.index, **kwargs)),
            redirect_stdout(output),
        ):
            exit_code = cli.main()
        stats = json.loads(output.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(stats["page_count"], 1)
        self.assertEqual(self.events, [])

    def test_transient_rate_limit_is_retried_but_permanent_failure_is_partial(self):
        self.get_errors = [response({}, 429)]
        content = sync.crawl_notion_page_content("page")
        self.assertTrue(content["complete"])
        self.assertEqual(content["text"], "본문")
        self.get_errors = [response({}, 403)]
        content = sync.crawl_notion_page_content("page")
        self.assertFalse(content["complete"])
        self.assertIn("http_403", content["incomplete_reasons"])

    def test_verified_catalog_avoids_duplicate_before_get_but_checks_after_body(self):
        self.index.upsert_catalog('db', self.rows, complete=True)
        result = sync.validate_index_candidate({'page_id': 'page', 'database_id': 'db'}, ['db'],
                                               index=self.index, catalog_page=self.rows[0])
        self.assertTrue(result['complete'])
        self.assertEqual(self.events.count('pages/page'), 1)
        self.assertLess(self.events.index('blocks/page/children'), self.events.index('pages/page'))

    def test_api_captions_and_equations_are_collected(self):
        self.blocks['page'] = [
            {'id': 'image', 'type': 'image', 'image': {'caption': [{'plain_text': 'image-caption'}]}},
            {'id': 'file', 'type': 'file', 'file': {'caption': [{'plain_text': 'file-caption'}], 'name': 'manual.pdf'}},
            {'id': 'eq', 'type': 'equation', 'equation': {'expression': 'x=y+1'}},
            {'id': 'code', 'type': 'code', 'code': {'rich_text': [{'plain_text': 'print(x)'}],
                                                  'caption': [{'plain_text': 'code-caption'}]}},
        ]
        result = sync.crawl_notion_page_content('page')
        for text in ['image-caption', 'file-caption', 'manual.pdf', 'x=y+1', 'code-caption', 'print(x)']:
            self.assertIn(text, result['text'])

    def test_empty_synced_source_keeps_a_dependency_without_text_segments(self):
        self._synced_source()
        self.blocks['source'] = []
        result = sync.crawl_notion_page_content('page', allowed_database_ids={'db'})
        self.assertTrue(result['complete'])
        self.assertEqual(result['segments'], [])
        self.assertEqual(result['synced_reference_ids'], ['source'])


if __name__ == "__main__":
    unittest.main()
