import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.notion import schema


class NotionSchemaTests(unittest.TestCase):
    def test_database_selection_requires_configuration_and_matches_label(self):
        with patch.object(schema, "get_notion_settings", return_value={"token": "", "database_ids": []}):
            with self.assertRaises(HTTPException) as raised:
                schema.select_notion_database_id()
        self.assertEqual(raised.exception.status_code, 503)

        database_ids = ["a" * 32, "b" * 32]
        with (
            patch.object(
                schema,
                "get_notion_settings",
                return_value={"token": "token", "database_ids": database_ids},
            ),
            patch.object(
                schema,
                "fetch_notion_database_title",
                side_effect=["업무 기록", "주요이슈 현황"],
            ),
        ):
            self.assertEqual(schema.select_notion_database_id("주요이슈"), database_ids[1])

    def test_database_selection_prefers_single_or_primary_database(self):
        with patch.object(
            schema,
            "get_notion_settings",
            return_value={"token": "token", "database_ids": ["a" * 32]},
        ):
            self.assertEqual(schema.select_notion_database_id(), "a" * 32)

        database_ids = ["a" * 32, "b" * 32]
        with (
            patch.object(
                schema,
                "get_notion_settings",
                return_value={"token": "token", "database_ids": database_ids},
            ),
            patch.object(
                schema,
                "fetch_notion_database_title",
                side_effect=["일반 업무", "주요이슈 현황"],
            ),
        ):
            self.assertEqual(schema.select_notion_database_id(), database_ids[1])

    def test_ids_are_normalized_and_checked_against_allowlist(self):
        dashed = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        self.assertEqual(schema.normalize_notion_id(dashed), "a" * 32)
        self.assertEqual(schema.validate_notion_page_id(dashed), "a" * 32)
        with self.assertRaises(HTTPException) as raised:
            schema.validate_notion_page_id("invalid")
        self.assertEqual(raised.exception.status_code, 422)

        with patch.object(
            schema,
            "get_notion_settings",
            return_value={"database_ids": [dashed]},
        ):
            self.assertEqual(schema.ensure_configured_notion_database("a" * 32), "a" * 32)
            with self.assertRaises(HTTPException) as forbidden:
                schema.ensure_configured_notion_database("b" * 32)
        self.assertEqual(forbidden.exception.status_code, 403)

    def test_property_lookup_respects_name_type_and_option_fit(self):
        properties = {
            "요청 제목": {"type": "title"},
            "업무 상태": {
                "type": "status",
                "status": {"options": [{"name": "진행 중"}, {"name": "완료"}]},
            },
            "구상태": {
                "type": "select",
                "select": {"options": [{"name": "보관됨"}]},
            },
        }
        self.assertEqual(
            schema.find_notion_property(properties, ["제목"], {"title"})[0],
            "요청 제목",
        )
        self.assertIsNone(schema.find_notion_property(properties, ["제목"], {"date"}))
        self.assertEqual(
            schema.find_notion_option_property(properties, ["상태", "구상태"], "완료")[0],
            "업무 상태",
        )
        self.assertIsNone(schema.find_notion_option_property(properties, ["담당"], "홍길동"))
        self.assertEqual(schema.notion_title_property(properties), "요청 제목")
        with self.assertRaises(HTTPException) as raised:
            schema.notion_title_property({"상태": {"type": "status"}})
        self.assertEqual(raised.exception.status_code, 502)

    def test_option_alias_and_property_payload_cover_supported_types(self):
        status = {
            "type": "status",
            "status": {"options": [{"name": "결재완료(팀장)"}, {"name": "진행 중"}]},
        }
        self.assertEqual(schema.notion_option_name(status, "완료"), "결재완료(팀장)")
        self.assertEqual(schema.notion_option_name(status, "진행"), "진행 중")

        cases = [
            ({"type": "title"}, "제목", {"title": [{"type": "text", "text": {"content": "제목"}}]}),
            ({"type": "rich_text"}, "본문", {"rich_text": [{"type": "text", "text": {"content": "본문"}}]}),
            ({"type": "select", "select": {"options": []}}, "신규", {"select": {"name": "신규"}}),
            (status, "진행", {"status": {"name": "진행 중"}}),
            ({"type": "multi_select", "multi_select": {"options": []}}, "MTS/HTS", {"multi_select": [{"name": "MTS"}, {"name": "HTS"}]}),
            ({"type": "date"}, "목표 2026-08-31", {"date": {"start": "2026-08-31"}}),
            ({"type": "checkbox"}, 1, {"checkbox": True}),
            ({"type": "number"}, "1.5", {"number": 1.5}),
            ({"type": "url"}, "https://example.com", {"url": "https://example.com"}),
            ({"type": "email"}, "a@example.com", {"email": "a@example.com"}),
            ({"type": "phone_number"}, "010", {"phone_number": "010"}),
        ]
        for prop, value, expected in cases:
            with self.subTest(prop_type=prop["type"]):
                self.assertEqual(schema.notion_property_payload(prop, value), expected)

        self.assertIsNone(schema.notion_property_payload({"type": "date"}, "내일"))
        self.assertIsNone(schema.notion_property_payload({"type": "number"}, "숫자 아님"))
        self.assertIsNone(schema.notion_property_payload({"type": "formula"}, "값"))
        self.assertIsNone(schema.notion_property_payload({"type": "title"}, None))


if __name__ == "__main__":
    unittest.main()
