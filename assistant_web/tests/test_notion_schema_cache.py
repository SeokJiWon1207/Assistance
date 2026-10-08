"""디스크 스키마 캐시의 권한 분리·손상·만료와 저장 실패 회귀."""

import tempfile
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.notion import schema_cache


class NotionSchemaCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.runtime = patch.object(schema_cache, "RUNTIME_DIR", self.root)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.headers = {"Authorization": "Bearer private-token", "Notion-Version": "2022-06-28"}
        self.data = {"id": "aabb", "object": "database", "title": [], "properties": {"제목": {"type": "title"}}}

    def test_tokens_and_page_content_are_not_saved_and_api_versions_do_not_share_cache(self):
        schema_cache.save_schema("aa-bb", self.headers, {**self.data, "page_content": "private-body"})
        file = next((self.root / "notion_schemas").iterdir())
        self.assertNotIn("private-token", file.read_text())
        self.assertNotIn("private-body", file.read_text())
        self.assertEqual(schema_cache.load_schema("aabb", self.headers, 60)[1], self.data)
        changed = {**self.headers, "Notion-Version": "different"}
        self.assertIsNone(schema_cache.load_schema("aabb", changed, 60))

    def test_corrupted_or_mismatched_schema_is_not_used(self):
        schema_cache.save_schema("aabb", self.headers, self.data)
        file = next((self.root / "notion_schemas").iterdir())
        for content in ('not-json', '[]', '{"fetchedAt": 0, "schema": null}'):
            file.write_text(content)
            self.assertIsNone(schema_cache.load_schema("aabb", self.headers, 60))
        schema_cache.save_schema("aabb", self.headers, {**self.data, "id": "other"})
        self.assertIsNone(schema_cache.load_schema("aabb", self.headers, 60))
        for properties in (["invalid"], {"상태": "invalid"}):
            file.write_text(json.dumps({"fetchedAt": time.time(), "schema": {**self.data, "properties": properties}}))
            self.assertIsNone(schema_cache.load_schema("aabb", self.headers, 60))

    def test_unwritable_cache_does_not_break_live_schema_retrieval(self):
        with patch.object(Path, "mkdir", side_effect=OSError("read-only")):
            schema_cache.save_schema("aabb", self.headers, self.data)
        self.assertIsNone(schema_cache.load_schema("aabb", self.headers, 60))
