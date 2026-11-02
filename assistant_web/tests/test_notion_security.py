from backend.notion import schema as notion_schema
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from backend.api import notion as notion_api
from backend.notion import schema, write
from backend.schemas import ChatMessage, NotionPageUpdateRequest


class NotionSecurityTests(unittest.TestCase):
    def test_page_id_requires_uuid_shape(self):
        self.assertEqual(schema.validate_notion_page_id("a" * 32), "a" * 32)
        with self.assertRaises(HTTPException) as raised:
            schema.validate_notion_page_id("../users")
        self.assertEqual(raised.exception.status_code, 422)

    @patch.object(notion_schema, "resolve_notion_settings")
    def test_database_must_be_configured(self, settings):
        settings.return_value = {"database_ids": ["a" * 32]}
        self.assertEqual(schema.ensure_configured_notion_database("a" * 32), "a" * 32)
        with self.assertRaises(HTTPException) as raised:
            schema.ensure_configured_notion_database("b" * 32)
        self.assertEqual(raised.exception.status_code, 403)

    def test_member_cannot_write_notion(self):
        request = SimpleNamespace(state=SimpleNamespace(user={"role": "member"}))
        with self.assertRaises(HTTPException) as raised:
            notion_api._require_notion_writer(request)
        self.assertEqual(raised.exception.status_code, 403)

    @patch.object(write, "consume_action")
    @patch.object(write, "is_notion_write_confirm_phrase", return_value=True)
    @patch.object(write, "find_pending_notion_write_command", return_value="페이지 수정해줘")
    def test_chat_confirmation_denies_member_write(self, pending, confirm, consume):
        messages = [ChatMessage(role="user", content="확인")]
        result = write.run_notion_write_command(
            "확인",
            messages,
            allow_write=False,
        )
        self.assertEqual(result["raw"]["mode"], "notion_write_forbidden")
        pending.assert_called_once_with(messages)
        confirm.assert_called_once_with("확인")
        consume.assert_not_called()

    @patch.object(write.requests, "patch")
    @patch.object(write.requests, "get")
    @patch.object(notion_schema, "resolve_notion_settings")
    def test_update_rejects_page_from_other_database(self, settings, get, patch_request):
        settings.return_value = {"database_ids": ["a" * 32]}
        response = Mock(status_code=200)
        response.json.return_value = {"parent": {"database_id": "b" * 32}}
        get.return_value = response
        with self.assertRaises(HTTPException) as raised:
            write.update_notion_page("c" * 32, NotionPageUpdateRequest(title="변경"))
        self.assertEqual(raised.exception.status_code, 403)
        settings.assert_called_once_with()
        get.assert_called_once()
        self.assertEqual(get.call_args.args[0], f"https://api.notion.com/v1/pages/{'c' * 32}")
        patch_request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
