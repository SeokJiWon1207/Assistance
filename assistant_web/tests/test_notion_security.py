import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from backend.api import notion as notion_api
from backend.schemas import ChatMessage, NotionPageUpdateRequest
from backend.sources import notion


class NotionSecurityTests(unittest.TestCase):
    def test_page_id_requires_uuid_shape(self):
        self.assertEqual(notion.validate_notion_page_id("a" * 32), "a" * 32)
        with self.assertRaises(HTTPException) as raised:
            notion.validate_notion_page_id("../users")
        self.assertEqual(raised.exception.status_code, 422)

    @patch.object(notion, "get_notion_settings")
    def test_database_must_be_configured(self, settings):
        settings.return_value = {"database_ids": ["a" * 32]}
        self.assertEqual(notion.ensure_configured_notion_database("a" * 32), "a" * 32)
        with self.assertRaises(HTTPException) as raised:
            notion.ensure_configured_notion_database("b" * 32)
        self.assertEqual(raised.exception.status_code, 403)

    def test_member_cannot_write_notion(self):
        request = SimpleNamespace(state=SimpleNamespace(user={"role": "member"}))
        with self.assertRaises(HTTPException) as raised:
            notion_api._require_notion_writer(request)
        self.assertEqual(raised.exception.status_code, 403)

    @patch.object(notion, "execute_notion_write_command")
    @patch.object(notion, "is_notion_write_confirm_phrase", return_value=True)
    @patch.object(notion, "find_pending_notion_write_command", return_value="페이지 수정해줘")
    def test_chat_confirmation_denies_member_write(self, _pending, _confirm, execute):
        result = notion.run_notion_write_command(
            "확인",
            [ChatMessage(role="user", content="확인")],
            allow_write=False,
        )
        self.assertEqual(result["raw"]["mode"], "notion_write_forbidden")
        execute.assert_not_called()

    @patch.object(notion.requests, "patch")
    @patch.object(notion.requests, "get")
    @patch.object(notion, "get_notion_settings")
    def test_update_rejects_page_from_other_database(self, settings, get, patch_request):
        settings.return_value = {"database_ids": ["a" * 32]}
        response = Mock(status_code=200)
        response.json.return_value = {"parent": {"database_id": "b" * 32}}
        get.return_value = response
        with self.assertRaises(HTTPException) as raised:
            notion.update_notion_page("c" * 32, NotionPageUpdateRequest(title="변경"))
        self.assertEqual(raised.exception.status_code, 403)
        patch_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
