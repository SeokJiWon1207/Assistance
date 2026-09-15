"""API 키에 연결된 Notion DB 자동 조회를 검증한다."""

import os
import unittest
from unittest.mock import Mock, patch

import requests

from backend import config
from backend.notion import search
from backend.notion.formatting import format_deterministic_notion_answer


def search_response(results, has_more=False, next_cursor=None, status_code=200):
    response = Mock(status_code=status_code, text="")
    response.json.return_value = {"results": results, "has_more": has_more, "next_cursor": next_cursor}
    return response


def database(database_id, **extra):
    return {"object": "database", "id": database_id, **extra}


class NotionDatabaseDiscoveryTests(unittest.TestCase):
    def setUp(self):
        config._notion_discovery_cache.clear()
        self.addCleanup(config._notion_discovery_cache.clear)

    def test_configured_ids_are_used_without_discovery(self):
        with (
            patch.dict(os.environ, {"NOTION_API_TOKEN": "token", "NOTION_DATABASE_IDS": "a-1,b2"}, clear=True),
            patch.object(config.requests, "post") as post,
        ):
            settings = config.get_notion_settings()

        self.assertEqual(settings["database_ids"], ["a1", "b2"])
        self.assertEqual(settings["database_source"], "configured")
        post.assert_not_called()

    def test_missing_token_does_not_call_notion(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(config.requests, "post") as post:
            settings = config.get_notion_settings()

        self.assertEqual(settings["database_ids"], [])
        self.assertEqual(config.notion_unavailable_reason(settings), "NOTION_API_TOKEN이 설정되지 않았습니다.")
        post.assert_not_called()

    def test_token_only_discovers_connected_databases_across_pages_and_caches(self):
        pages = [
            search_response(
                [database("aaaa-1111"), database("bbbb-2222", archived=True), {"object": "page", "id": "cccc"}],
                has_more=True,
                next_cursor="cursor-2",
            ),
            search_response([database("dddd-4444"), database("aaaa-1111"), database("eeee", in_trash=True)]),
        ]
        with (
            patch.dict(os.environ, {"NOTION_API_TOKEN": "token-new"}, clear=True),
            patch.object(config.requests, "post", side_effect=pages) as post,
        ):
            first = config.get_notion_settings()
            second = config.get_notion_settings()

        self.assertEqual(first["database_ids"], ["aaaa1111", "dddd4444"])
        self.assertEqual(first["database_source"], "discovered")
        self.assertEqual(second["database_ids"], first["database_ids"])
        self.assertEqual(post.call_count, 2)
        first_call, second_call = post.call_args_list
        self.assertEqual(first_call.args[0], f"{config.NOTION_API_BASE}/search")
        self.assertEqual(first_call.kwargs["json"]["filter"], {"property": "object", "value": "database"})
        self.assertEqual(first_call.kwargs["headers"]["Authorization"], "Bearer token-new")
        self.assertEqual(second_call.kwargs["json"]["start_cursor"], "cursor-2")

    def test_changed_token_is_discovered_separately(self):
        with (
            patch.dict(os.environ, {"NOTION_API_TOKEN": "old"}, clear=True),
            patch.object(config.requests, "post", return_value=search_response([database("old-db")])),
        ):
            self.assertEqual(config.get_notion_settings()["database_ids"], ["olddb"])
        with (
            patch.dict(os.environ, {"NOTION_API_TOKEN": "new"}, clear=True),
            patch.object(config.requests, "post", return_value=search_response([database("new-db")])),
        ):
            self.assertEqual(config.get_notion_settings()["database_ids"], ["newdb"])

    def test_discovery_failure_is_an_error_not_missing_configuration(self):
        unauthorized = search_response([], status_code=401)
        unauthorized.text = '{"code":"unauthorized"}'
        with (
            patch.dict(os.environ, {"NOTION_API_TOKEN": "bad"}, clear=True),
            patch.object(config.requests, "post", return_value=unauthorized) as post,
        ):
            result = search.execute_notion_search("노션 문서 찾아줘")
            search.execute_notion_search("노션 문서 찾아줘")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_kind"], "dependency")
        self.assertIn("401", result["reason"])
        # 장애 중 요청마다 재시도하지 않도록 실패도 짧게 캐시한다.
        self.assertEqual(post.call_count, 1)

    def test_partial_discovery_is_not_used_as_the_full_database_list(self):
        with (
            patch.dict(os.environ, {"NOTION_API_TOKEN": "token", "NOTION_TOOL_MAX_RETRIES": "0"}, clear=True),
            patch.object(config.requests, "post", side_effect=[
                search_response([database("aaaa")], has_more=True, next_cursor="next"),
                requests.ConnectionError("down"),
            ]),
        ):
            settings = config.get_notion_settings()

        self.assertEqual(settings["database_ids"], [])
        self.assertIn("ConnectionError", settings["database_discovery_error"])

    def test_too_many_connected_databases_are_rejected_instead_of_scanned(self):
        many = [database(f"db-{index:03d}") for index in range(21)]
        with (
            patch.dict(os.environ, {"NOTION_API_TOKEN": "token"}, clear=True),
            patch.object(config.requests, "post", return_value=search_response(many)) as post,
        ):
            result = search.execute_notion_search("노션 문서 찾아줘")
            search.execute_notion_search("노션 문서 찾아줘")

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error_kind"], "internal")
        self.assertIn("21개", result["reason"])
        self.assertIn("NOTION_DATABASE_IDS", result["reason"])
        # 설정 문제는 장애가 아니므로 일반 TTL 동안 다시 조회하지 않는다.
        self.assertEqual(post.call_count, 1)

    def test_database_limit_is_configurable_and_unfinished_pagination_counts_as_over_limit(self):
        with (
            patch.dict(os.environ, {"NOTION_API_TOKEN": "a", "NOTION_DISCOVERY_MAX_DATABASES": "25"}, clear=True),
            patch.object(config.requests, "post", return_value=search_response(
                [database(f"db-{index:03d}") for index in range(21)],
            )),
        ):
            self.assertEqual(len(config.get_notion_settings()["database_ids"]), 21)
        endless = search_response([database("same")], has_more=True, next_cursor="again")
        with (
            patch.dict(os.environ, {"NOTION_API_TOKEN": "b"}, clear=True),
            patch.object(config.requests, "post", return_value=endless) as post,
        ):
            settings = config.get_notion_settings()
        self.assertEqual(settings["database_ids"], [])
        self.assertIn("1개 이상", settings["database_discovery_error"])
        self.assertEqual(post.call_count, config.NOTION_DISCOVERY_MAX_PAGES)

    def test_no_connected_database_explains_how_to_connect(self):
        with (
            patch.dict(os.environ, {"NOTION_API_TOKEN": "token"}, clear=True),
            patch.object(config.requests, "post", return_value=search_response([])),
        ):
            result = search.execute_notion_search("노션 문서 찾아줘")

        self.assertEqual(result["status"], "skipped")
        self.assertIn("연결(Connections)", result["reason"])
        answer = format_deterministic_notion_answer("노션 문서 찾아줘", {"stages": [result]}, 5)
        self.assertIn("연결된 DB는 자동으로 검색합니다", answer)


if __name__ == "__main__":
    unittest.main()
