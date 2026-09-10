import tempfile
import unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from fastapi import HTTPException
from backend.notion import actions, write
from backend.schemas import ChatMessage


class NotionActionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        p = patch.object(actions, "RUNTIME_DIR", Path(temporary.name))
        p.start()
        self.addCleanup(p.stop)

    def test_confirmation_keeps_target_and_replays_without_second_write(self):
        page = {"id": "a" * 32, "title": "업무", "database_id": "b" * 32}
        schema = {"상태": {"id": "status-id", "type": "status", "status": {"options": [{"name": "완료"}]}}}
        with (
            patch.object(write, "execute_notion_search", return_value={"notion_pages": [page]}) as search,
            patch.object(write, "ensure_configured_notion_database", return_value="b" * 32),
            patch.object(write, "notion_property_schema", return_value=schema),
        ):
            preview = write.run_notion_write_command('노션에서 "업무" 상태를 완료로 바꿔줘', [], user_uid="manager")
        search.assert_called_once()
        messages = [
            ChatMessage(role="assistant", content=preview["content"], notionActionId=preview["notionActionId"]),
            ChatMessage(role="user", content="네"),
        ]
        with (
            patch.object(write, "execute_notion_search", side_effect=AssertionError("must not search again")),
            patch.object(write, "update_notion_page", return_value=page) as update,
        ):
            first = write.run_notion_write_command("네", messages, user_uid="manager")
            second = write.run_notion_write_command("네", messages, user_uid="manager")
        self.assertEqual(first, second)
        self.assertEqual(update.call_args.args[0], page["id"])
        update.assert_called_once()
        self.assertEqual(update.call_args.kwargs["prepared_properties"], {"status-id": {"status": {"name": "완료"}}})

    def test_cross_user_expiry_cancel_and_uncertain_execution(self):
        key = actions.save_action("a", {"x": 1})
        with self.assertRaises(HTTPException):
            actions.consume_action(key, "b", lambda _: {})
        with patch.object(actions.time, "time", return_value=10**12), self.assertRaises(HTTPException):
            actions.consume_action(key, "a", lambda _: {})
        actions.consume_action(key, "a", lambda _: {}, cancel=True)
        with self.assertRaises(HTTPException):
            actions.consume_action(key, "a", lambda _: {})
        key = actions.save_action("a", {})
        with self.assertRaises(TimeoutError):
            actions.consume_action(key, "a", lambda _: (_ for _ in ()).throw(TimeoutError()))
        with self.assertRaises(HTTPException):
            actions.consume_action(key, "a", lambda _: self.fail("must not retry"))

    def test_concurrent_confirmation_executes_once(self):
        key = actions.save_action("a", {})
        calls = []

        def submit(_):
            try:
                return actions.consume_action(key, "a", lambda _: calls.append(1) or {"done": True})
            except HTTPException as exc:
                return exc.status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(submit, range(8)))
        self.assertEqual(calls, [1])
        self.assertTrue(all(result == {"done": True} or result == 409 for result in results))
