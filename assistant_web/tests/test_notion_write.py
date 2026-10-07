import unittest
from unittest.mock import Mock, patch

from fastapi import HTTPException

from backend.notion import write
from backend.notion.constants import NOTION_WRITE_CONFIRM_HEADER_CREATE
from backend.schemas import ChatMessage, NotionPageUpdateRequest, NotionPageWriteRequest


class NotionWritePropertyTests(unittest.TestCase):
    def test_people_and_write_properties_map_supported_fields(self):
        with patch.object(write, "fetch_notion_users", return_value=(("홍길동", "u1"), ("김개발", "u2"))):
            self.assertEqual(
                write.notion_people_payload({"type": "people"}, "홍길동/없는사람"),
                {"people": [{"id": "u1"}]},
            )
            self.assertIsNone(write.notion_people_payload({"type": "people"}, "없는사람"))

        properties = {
            "제목": {"type": "title"},
            "상태": {"type": "status", "status": {"options": [{"name": "진행 중"}]}},
            "담당자": {"type": "people"},
            "목표일": {"type": "date"},
            "메모": {"type": "rich_text"},
            "사용자 정의": {"type": "number"},
        }
        with (
            patch.object(write, "notion_property_schema", return_value=properties),
            patch.object(write, "notion_people_payload", return_value={"people": [{"id": "u1"}]}),
        ):
            payload = write.build_notion_write_properties(
                "db",
                {
                    "title": "테스트",
                    "status": "진행",
                    "owner": "홍길동",
                    "dueDate": "2026-08-31",
                    "memo": "확인",
                    "properties": {"사용자 정의": 3, "없는 속성": "무시"},
                },
                require_title=True,
            )
        self.assertEqual(payload["제목"]["title"][0]["text"]["content"], "테스트")
        self.assertEqual(payload["상태"], {"status": {"name": "진행 중"}})
        self.assertEqual(payload["담당자"], {"people": [{"id": "u1"}]})
        self.assertEqual(payload["사용자 정의"], {"number": 3.0})

        with patch.object(write, "notion_property_schema", return_value=properties):
            with self.assertRaises(HTTPException) as raised:
                write.build_notion_write_properties("db", {}, require_title=True)
        self.assertEqual(raised.exception.status_code, 400)

    def test_content_becomes_one_bounded_paragraph(self):
        self.assertEqual(write.notion_children_from_content(""), [])
        children = write.notion_children_from_content("가" * 2000)
        content = children[0]["paragraph"]["rich_text"][0]["text"]["content"]
        self.assertEqual(len(content), 1900)


class NotionWriteHttpTests(unittest.TestCase):
    def test_create_page_builds_payload_and_translates_http_failure(self):
        success = Mock(status_code=200)
        success.json.return_value = {"id": "page"}
        normalized = {"id": "page", "title": "새 업무"}
        request = NotionPageWriteRequest(title="새 업무", content="본문")
        with (
            patch.object(write, "select_notion_database_id", return_value="db"),
            patch.object(write, "build_notion_write_properties", return_value={"제목": {}}),
            patch.object(write, "fetch_notion_database_title", return_value="업무 DB"),
            patch.object(write, "normalize_notion_page", return_value=normalized),
            patch.object(write.requests, "post", return_value=success) as post,
            patch.object(write, "clear_notion_query_cache") as clear_cache,
        ):
            self.assertEqual(write.create_notion_page(request), normalized)
        clear_cache.assert_called_once_with()
        self.assertEqual(post.call_args.kwargs["timeout"], 30)
        self.assertIn("children", post.call_args.kwargs["json"])

        failure = Mock(status_code=429, text="rate limited")
        with (
            patch.object(write, "select_notion_database_id", return_value="db"),
            patch.object(write, "build_notion_write_properties", return_value={}),
            patch.object(write.requests, "post", return_value=failure),
        ):
            with self.assertRaises(HTTPException) as raised:
                write.create_notion_page(NotionPageWriteRequest(title="실패"))
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("429", raised.exception.detail)

    def test_update_page_validates_parent_properties_and_http_results(self):
        page = Mock(status_code=200)
        page.json.return_value = {"parent": {"database_id": "db"}}
        updated = Mock(status_code=200)
        updated.json.return_value = {"id": "page"}
        normalized = {"id": "page", "status": "완료"}
        with (
            patch.object(write, "validate_notion_page_id", return_value="page"),
            patch.object(write.requests, "get", return_value=page),
            patch.object(write, "ensure_configured_notion_database") as ensure,
            patch.object(write, "build_notion_write_properties", return_value={"상태": {}}),
            patch.object(write.requests, "patch", return_value=updated),
            patch.object(write, "fetch_notion_database_title", return_value="업무 DB"),
            patch.object(write, "normalize_notion_page", return_value=normalized),
            patch.object(write, "clear_notion_query_cache") as clear_cache,
        ):
            self.assertEqual(
                write.update_notion_page("page", NotionPageUpdateRequest(status="완료")),
                normalized,
            )
        ensure.assert_called_once_with("db")
        clear_cache.assert_called_once_with()

        lookup_failure = Mock(status_code=404, text="missing")
        with (
            patch.object(write, "validate_notion_page_id", return_value="page"),
            patch.object(write.requests, "get", return_value=lookup_failure),
        ):
            with self.assertRaises(HTTPException) as raised:
                write.update_notion_page("page", NotionPageUpdateRequest(status="완료"))
        self.assertEqual(raised.exception.status_code, 502)

        non_database = Mock(status_code=200)
        non_database.json.return_value = {"parent": {}}
        with (
            patch.object(write, "validate_notion_page_id", return_value="page"),
            patch.object(write.requests, "get", return_value=non_database),
        ):
            with self.assertRaises(HTTPException) as raised:
                write.update_notion_page("page", NotionPageUpdateRequest(status="완료"))
        self.assertEqual(raised.exception.status_code, 400)

    def test_update_page_rejects_empty_or_failed_patch(self):
        page = Mock(status_code=200)
        page.json.return_value = {"parent": {"database_id": "db"}}
        common = (
            patch.object(write, "validate_notion_page_id", return_value="page"),
            patch.object(write.requests, "get", return_value=page),
            patch.object(write, "ensure_configured_notion_database"),
        )
        with common[0], common[1], common[2], patch.object(
            write, "build_notion_write_properties", return_value={}
        ):
            with self.assertRaises(HTTPException) as raised:
                write.update_notion_page("page", NotionPageUpdateRequest())
        self.assertEqual(raised.exception.status_code, 400)

        failure = Mock(status_code=500, text="failed")
        with (
            patch.object(write, "validate_notion_page_id", return_value="page"),
            patch.object(write.requests, "get", return_value=page),
            patch.object(write, "ensure_configured_notion_database"),
            patch.object(write, "build_notion_write_properties", return_value={"상태": {}}),
            patch.object(write.requests, "patch", return_value=failure),
        ):
            with self.assertRaises(HTTPException) as raised:
                write.update_notion_page("page", NotionPageUpdateRequest(status="완료"))
        self.assertEqual(raised.exception.status_code, 502)


class NotionWriteCommandTests(unittest.TestCase):
    def test_text_parsing_and_pending_command_contract(self):
        fields = write.extract_notion_write_fields(
            "상태를 완료로 담당자 홍길동 목표일 2026-08-31 메모 점검 필요"
        )
        self.assertEqual(fields["status"], "완료")
        self.assertEqual(fields["owner"], "홍길동")
        self.assertEqual(fields["dueDate"], "2026-08-31")
        self.assertEqual(fields["memo"], "점검 필요")
        self.assertEqual(write.notion_write_intent('노션에 "업무" 추가해줘'), "create")
        self.assertEqual(write.notion_write_intent('Notion에서 "업무" 수정해줘'), "update")
        self.assertIsNone(write.notion_write_intent("Mantis 이슈 수정해줘"))
        self.assertTrue(write.is_notion_write_confirm_phrase("네"))
        self.assertTrue(write.is_notion_write_cancel_phrase("취소"))
        self.assertFalse(write.is_notion_write_confirm_phrase("확인하고 다른 것도 처리해줘"))

        messages = [
            ChatMessage(role="user", content='노션에 "업무" 추가해줘'),
            ChatMessage(role="assistant", content=f"{NOTION_WRITE_CONFIRM_HEADER_CREATE}\n- 제목: 업무"),
            ChatMessage(role="user", content="네"),
        ]
        self.assertEqual(write.find_pending_notion_write_command(messages), messages[0].content)
        self.assertIsNone(write.find_pending_notion_write_command(messages[:2]))

    def test_resolve_update_handles_missing_not_found_ambiguous_and_ready(self):
        missing_fields = write.resolve_notion_write_action('노션에서 "업무" 수정해줘', "update")
        self.assertEqual(missing_fields["response"]["raw"]["mode"], "notion_update_missing_fields")

        with (
            patch.object(write, "extract_notion_write_fields", return_value={"status": "완료"}),
            patch.object(write, "clean_notion_update_target", return_value="업무"),
            patch.object(write, "execute_notion_search", return_value={"notion_pages": []}),
        ):
            not_found = write.resolve_notion_write_action("질문", "update")
        self.assertEqual(not_found["response"]["raw"]["mode"], "notion_update_not_found")

        pages = [{"id": "1", "title": "업무1"}, {"id": "2", "title": "업무2"}]
        with (
            patch.object(write, "extract_notion_write_fields", return_value={"status": "완료"}),
            patch.object(write, "clean_notion_update_target", return_value="업무"),
            patch.object(write, "execute_notion_search", return_value={"notion_pages": pages}),
            patch.object(write, "build_sources_from_notion_pages", return_value=[{"type": "notion"}]),
        ):
            ambiguous = write.resolve_notion_write_action("질문", "update")
        self.assertEqual(ambiguous["response"]["raw"]["mode"], "notion_update_ambiguous")

        with (
            patch.object(write, "extract_notion_write_fields", return_value={"status": "완료"}),
            patch.object(write, "clean_notion_update_target", return_value="업무"),
            patch.object(write, "execute_notion_search", return_value={"notion_pages": pages[:1]}),
        ):
            ready = write.resolve_notion_write_action("질문", "update")
        self.assertTrue(ready["ready"])
        self.assertEqual(ready["target_page"]["id"], "1")

    def test_preview_execute_and_confirmation_paths(self):
        create_action = {
            "ready": True,
            "action": "create",
            "database_label": "업무 DB",
            "title": "새 업무",
            "fields": {"status": "등록됨", "owner": "홍길동"},
        }
        with patch.object(write, "resolve_notion_write_action", return_value=create_action):
            preview = write.build_notion_write_preview("질문", "create")
        self.assertEqual(preview["raw"], {"mode": "notion_write_pending", "action": "create"})

        page = {"id": "1", "title": "새 업무", "status": "등록됨", "owner": "홍길동"}
        with (
            patch.object(write, "resolve_notion_write_action", return_value=create_action),
            patch.object(write, "create_notion_page", return_value=page),
            patch.object(write, "build_sources_from_single_notion_page", return_value=[{"type": "notion"}]),
        ):
            created = write.execute_notion_write_command('노션에 "새 업무" 추가해줘')
        self.assertEqual(created["raw"]["mode"], "notion_create")

        messages = [
            ChatMessage(role="user", content="명령"),
            ChatMessage(role="assistant", content=f"{NOTION_WRITE_CONFIRM_HEADER_CREATE}\n미리보기"),
            ChatMessage(role="user", content="취소"),
        ]
        self.assertEqual(write.run_notion_write_command("네", messages)["raw"]["mode"], "notion_write_forbidden")
        with self.assertRaises(HTTPException) as raised:
            write.run_notion_write_command("네", messages, user_uid="manager")
        self.assertEqual(raised.exception.status_code, 409)
        self.assertIsNone(write.run_notion_write_command("일반 질문", []))


if __name__ == "__main__":
    unittest.main()
