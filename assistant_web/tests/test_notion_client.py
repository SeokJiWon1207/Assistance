import os
import unittest
from unittest.mock import Mock, call, patch

import requests
from fastapi import HTTPException

from backend import config
from backend.notion import client


class NotionClientTests(unittest.TestCase):
    def setUp(self):
        client.fetch_notion_relation_title.cache_clear()
        client.fetch_notion_user_name.cache_clear()
        client.clear_notion_database_cache()
        client._schema_seeds.clear()
        client._fetch_notion_users_cached.cache_clear()
        client._page_content_cached.cache_clear()

    def run(self, result=None):
        # 시계를 고정하는 테스트가 전역 요청 간격 제한에서 대기하지 않게 한다.
        with patch.object(client, "notion_request_slot"):
            return super().run(result)

    def tearDown(self):
        self.setUp()

    def test_relation_title_and_user_name_cache_successful_requests(self):
        page_response = Mock(status_code=200)
        page_response.json.return_value = {"properties": {"제목": {}}}
        user_response = Mock(status_code=200)
        user_response.json.return_value = {"id": "user-1", "name": "홍길동"}

        with (
            patch.object(client.requests, "get", side_effect=[page_response, user_response]) as get,
            patch.object(client, "notion_title", return_value="연결된 업무"),
            patch.object(client, "notion_user_name", return_value="홍길동"),
        ):
            self.assertEqual(client.fetch_notion_relation_title("page-1"), "연결된 업무")
            self.assertEqual(client.fetch_notion_relation_title("page-1"), "연결된 업무")
            self.assertEqual(client.fetch_notion_user_name("user-1"), "홍길동")
            self.assertEqual(client.fetch_notion_user_name("user-1"), "홍길동")

        self.assertEqual(get.call_count, 2)

    def test_relation_and_user_lookup_fail_closed_on_network_or_http_errors(self):
        non_success = Mock(status_code=404)
        with patch.object(
            client.requests,
            "get",
            side_effect=[requests.Timeout("timeout"), non_success],
        ):
            self.assertEqual(client.fetch_notion_relation_title("page-timeout"), "")
            self.assertEqual(client.fetch_notion_user_name("user-missing"), "")
        self.assertEqual(client.fetch_notion_relation_title(""), "")
        self.assertEqual(client.fetch_notion_user_name(""), "")

    def test_page_blocks_follow_pagination_and_respect_feature_switch(self):
        first = Mock(status_code=200)
        first.json.return_value = {
            "results": [{"id": "1"}],
            "has_more": True,
            "next_cursor": "cursor-2",
        }
        second = Mock(status_code=200)
        second.json.return_value = {
            "results": [{"id": "2"}],
            "has_more": False,
        }
        with (
            patch.dict(os.environ, {"NOTION_FETCH_PAGE_BLOCKS": "true"}),
            patch.object(client.requests, "get", side_effect=[first, second]) as get,
            patch.object(client, "notion_block_text", side_effect=lambda block: f"본문 {block['id']}"),
        ):
            content = client.fetch_notion_page_blocks("page-1", max_blocks=2)

        self.assertEqual(content, "본문 1\n본문 2")
        self.assertEqual(get.call_args_list[1].kwargs["params"]["start_cursor"], "cursor-2")
        with (
            patch.dict(os.environ, {"NOTION_FETCH_PAGE_BLOCKS": "false"}),
            patch.object(client.requests, "get") as disabled_get,
        ):
            self.assertEqual(client.fetch_notion_page_blocks("page-1"), "")
        disabled_get.assert_not_called()

    def test_database_title_uses_ttl_cache_and_fallback_label(self):
        success = Mock(status_code=200)
        success.json.return_value = {"title": [{"plain_text": "업무 DB"}]}
        failure = Mock(status_code=503, text="unavailable")
        with (
            patch.object(client.requests, "get", side_effect=[success, failure]) as get,
            patch.object(client, "notion_rich_text_to_plain", return_value="업무 DB"),
        ):
            self.assertEqual(client.fetch_notion_database_title("database-12345678"), "업무 DB")
            self.assertEqual(client.fetch_notion_database_title("database-12345678"), "업무 DB")
            self.assertEqual(client.fetch_notion_database_title("database-87654321"), "87654321")

        self.assertEqual(get.call_count, 2)

    def test_database_schema_caches_success_and_translates_http_failure(self):
        success = Mock(status_code=200)
        success.json.return_value = {"properties": {"상태": {"type": "status"}}}
        failure = Mock(status_code=429, text="rate limited")
        with patch.object(client.requests, "get", side_effect=[success, failure]) as get:
            first = client.fetch_notion_database_schema("db-ok")
            second = client.fetch_notion_database_schema("db-ok")
            with self.assertRaises(HTTPException) as raised:
                client.fetch_notion_database_schema("db-fail")

        self.assertEqual(first, second)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(raised.exception.status_code, 502)
        self.assertIn("429", raised.exception.detail)

    def test_title_and_schema_share_one_database_request(self):
        response = Mock(status_code=200)
        response.json.return_value = {"title": [], "properties": {"상태": {"type": "status"}}}
        with (
            patch.object(client.requests, "get", return_value=response) as get,
            patch.object(client, "notion_rich_text_to_plain", return_value="업무 DB"),
        ):
            self.assertEqual(client.fetch_notion_database_title("aaaa-bbbb"), "업무 DB")
            self.assertIn("상태", client.fetch_notion_database_schema("aaaabbbb")["properties"])

        get.assert_called_once()

    def test_expired_schema_is_served_while_refreshing_in_background(self):
        old = Mock(status_code=200)
        old.json.return_value = {"properties": {"이전": {}}}
        new = Mock(status_code=200)
        new.json.return_value = {"properties": {"새 속성": {}}}
        clock = [100.0]
        started = []
        with (
            patch.dict(os.environ, {"NOTION_CACHE_TTL_SECONDS": "600", "NOTION_SCHEMA_MAX_STALE_SECONDS": "86400"}),
            patch.object(client.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(client.requests, "get", side_effect=[old, new]) as get,
            patch.object(client, "Thread") as thread,
        ):
            thread.side_effect = lambda target, **_kwargs: started.append(target) or Mock()
            self.assertIn("이전", client.fetch_notion_database_schema("db")["properties"])
            clock[0] += 601
            # 만료된 값을 즉시 돌려주고 갱신은 한 번만 예약한다.
            self.assertIn("이전", client.fetch_notion_database_schema("db")["properties"])
            self.assertIn("이전", client.fetch_notion_database_schema("db")["properties"])
            self.assertEqual(len(started), 1)
            self.assertEqual(get.call_count, 1)
            started[0]()
            self.assertIn("새 속성", client.fetch_notion_database_schema("db")["properties"])

        self.assertEqual(get.call_count, 2)

    def test_schema_older_than_stale_limit_is_fetched_synchronously(self):
        old = Mock(status_code=200)
        old.json.return_value = {"properties": {"이전": {}}}
        new = Mock(status_code=200)
        new.json.return_value = {"properties": {"새 속성": {}}}
        clock = [100.0]
        with (
            patch.dict(os.environ, {"NOTION_CACHE_TTL_SECONDS": "600", "NOTION_SCHEMA_MAX_STALE_SECONDS": "3600"}),
            patch.object(client.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(client.requests, "get", side_effect=[old, new]),
            patch.object(client, "Thread") as thread,
        ):
            client.fetch_notion_database_schema("db")
            clock[0] += 3601
            self.assertIn("새 속성", client.fetch_notion_database_schema("db")["properties"])
        thread.assert_not_called()

    def test_stale_cache_refresh_failure_obeys_backoff_and_then_recovers(self):
        old = {"properties": {"이전": {}}}
        new = {"properties": {"새 속성": {}}}
        clock = [100.0]
        started = []
        with (
            patch.dict(os.environ, {"NOTION_CACHE_TTL_SECONDS": "600", "NOTION_SCHEMA_MAX_STALE_SECONDS": "86400"}),
            patch.object(client.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(client, "_request_notion_database", side_effect=[old, requests.Timeout("장애"), new]) as fetch,
            patch.object(client, "Thread") as thread,
            patch.object(client, "LOGGER"),
        ):
            thread.side_effect = lambda target, **_kwargs: started.append(target) or Mock()
            self.assertEqual(client.fetch_notion_database_schema("db"), old)
            clock[0] += 601
            self.assertEqual(client.fetch_notion_database_schema("db"), old)
            started.pop(0)()
            for seconds in (0, 1, 28):
                clock[0] += seconds
                self.assertEqual(client.fetch_notion_database_schema("db"), old)
                client.fetch_notion_database_title("db")
                self.assertEqual(started, [])
                self.assertEqual(fetch.call_count, 2)
            clock[0] += 1
            self.assertEqual(client.fetch_notion_database_schema("db"), old)
            self.assertEqual(len(started), 1)
            started.pop(0)()
            self.assertEqual(client.fetch_notion_database_schema("db"), new)
            self.assertEqual(fetch.call_count, 3)

    def test_recent_failure_is_not_retried_for_each_lookup(self):
        clock = [100.0]
        success = Mock(status_code=200)
        success.json.return_value = {"properties": {}}
        with (
            patch.object(client.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(client.requests, "get", side_effect=[requests.Timeout("slow"), success]) as get,
        ):
            self.assertEqual(client.fetch_notion_database_title("database-12345678"), "12345678")
            with self.assertRaises(requests.Timeout):
                client.fetch_notion_database_schema("database-12345678")
            self.assertEqual(get.call_count, 1)
            clock[0] += 31
            self.assertEqual(client.fetch_notion_database_schema("database-12345678"), {"properties": {}})

        self.assertEqual(get.call_count, 2)

    def test_warm_cache_loads_every_database_in_read_scope_once(self):
        with (
            patch.object(client, "get_notion_settings", return_value={"token": "secret", "database_ids": ["root-1"]}),
            patch(
                "backend.notion.read_scope.resolve_notion_read_scope",
                return_value=({"root1": "", "child1": "root1"}, True),
            ),
            patch.object(
                client,
                "fetch_notion_database_schema",
                side_effect=[{}, HTTPException(status_code=502, detail="fail")],
            ) as fetch,
        ):
            self.assertEqual(client.warm_notion_database_cache(), 1)

        self.assertEqual([item.args[0] for item in fetch.call_args_list], ["root1", "child1"])

    def test_warm_cache_skips_unconfigured_notion(self):
        with (
            patch.object(client, "get_notion_settings", return_value={"token": "", "database_ids": []}),
            patch.object(client, "fetch_notion_database_schema") as fetch,
        ):
            self.assertEqual(client.warm_notion_database_cache(), 0)
        fetch.assert_not_called()

    def test_user_list_follows_pagination_and_caches_the_result(self):
        first = Mock(status_code=200)
        first.json.return_value = {
            "results": [{"id": "u1", "name": "사용자1"}],
            "has_more": True,
            "next_cursor": "next",
        }
        second = Mock(status_code=200)
        second.json.return_value = {
            "results": [{"id": "u2", "name": "사용자2"}],
            "has_more": False,
        }
        with (
            patch.object(client, "_notion_cache_bucket", return_value=5),
            patch.object(client.requests, "get", side_effect=[first, second]) as get,
            patch.object(client, "notion_user_name", side_effect=lambda user: user["name"]),
        ):
            users = client.fetch_notion_users()
            cached = client.fetch_notion_users()

        self.assertEqual(users, (("사용자1", "u1"), ("사용자2", "u2")))
        self.assertEqual(cached, users)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].kwargs["params"]["start_cursor"], "next")


class NotionPostJsonTests(unittest.TestCase):
    def test_post_retries_network_failure_with_bounded_timeout(self):
        success = Mock(status_code=200)
        with (
            patch.dict(
                os.environ,
                {"NOTION_TOOL_TIMEOUT_SECONDS": "5", "NOTION_TOOL_MAX_RETRIES": "2",
                 "NOTION_MIN_REQUEST_INTERVAL_SECONDS": "0"},
            ),
            patch.object(config.requests, "post", side_effect=[requests.Timeout("slow"), success]) as post,
            patch.object(config.time, "sleep") as sleep,
            patch.object(config.random, "uniform", return_value=0),
        ):
            response = config.notion_post_json(
                "https://api.notion.test/query",
                {"query": "업무"},
                timeout=45,
                retries=2,
            )

        self.assertIs(response, success)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args_list[0].kwargs["timeout"], 5)
        self.assertEqual(post.call_args_list[1].kwargs["timeout"], 5)
        self.assertEqual(sleep.call_args_list, [call(0.5)])

    def test_post_raises_last_error_after_retry_budget_is_exhausted(self):
        with (
            patch.dict(
                os.environ,
                {"NOTION_TOOL_TIMEOUT_SECONDS": "3", "NOTION_TOOL_MAX_RETRIES": "1",
                 "NOTION_MIN_REQUEST_INTERVAL_SECONDS": "0"},
            ),
            patch.object(
                config.requests,
                "post",
                side_effect=[requests.ConnectionError("first"), requests.Timeout("last")],
            ) as post,
            patch.object(config.time, "sleep"),
            patch.object(config.random, "uniform", return_value=0),
        ):
            with self.assertRaisesRegex(requests.Timeout, "last"):
                config.notion_post_json("https://api.notion.test/query", {}, retries=2)

        self.assertEqual(post.call_count, 2)

    def test_post_retries_transient_http_status(self):
        throttled = Mock(status_code=429)
        success = Mock(status_code=200)
        with (
            patch.dict(
                os.environ,
                {"NOTION_TOOL_TIMEOUT_SECONDS": "6", "NOTION_TOOL_MAX_RETRIES": "1",
                 "NOTION_MIN_REQUEST_INTERVAL_SECONDS": "0"},
            ),
            patch.object(config.requests, "post", side_effect=[throttled, success]) as post,
            patch.object(config.time, "sleep") as sleep,
            patch.object(config.random, "uniform", return_value=0),
        ):
            response = config.notion_post_json("https://api.notion.test/query", {}, retries=2)

        self.assertIs(response, success)
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(0.5)

    def test_post_respects_retry_after_header(self):
        throttled = Mock(status_code=429, headers={"Retry-After": "2"})
        success = Mock(status_code=200)
        with (
            patch.dict(os.environ, {"NOTION_TOOL_MAX_RETRIES": "1",
                                    "NOTION_MIN_REQUEST_INTERVAL_SECONDS": "0"}),
            patch.object(config.requests, "post", side_effect=[throttled, success]),
            patch.object(config.time, "sleep") as sleep,
            patch.object(config.random, "uniform", return_value=0),
        ):
            self.assertIs(config.notion_post_json("https://api.notion.test/query", {}), success)
        sleep.assert_called_once_with(2.0)

    def test_post_does_not_retry_non_transient_http_status(self):
        forbidden = Mock(status_code=403)
        with (
            patch.dict(
                os.environ,
                {"NOTION_TOOL_TIMEOUT_SECONDS": "6", "NOTION_TOOL_MAX_RETRIES": "1",
                 "NOTION_MIN_REQUEST_INTERVAL_SECONDS": "0"},
            ),
            patch.object(config.requests, "post", return_value=forbidden) as post,
        ):
            response = config.notion_post_json("https://api.notion.test/query", {}, retries=2)

        self.assertIs(response, forbidden)
        post.assert_called_once()


if __name__ == "__main__":
    unittest.main()

class NotionNameRecoveryTests(unittest.TestCase):
    def test_failed_lookup_can_recover_and_credentials_and_ttl_isolate_success(self):
        client.fetch_notion_user_name.cache_clear()
        self.addCleanup(client.fetch_notion_user_name.cache_clear)
        response = Mock(status_code=200)
        response.json.return_value = {"name": "김지원"}
        with (
            patch.object(client.requests, 'get', side_effect=[requests.Timeout(), response, response, response]) as get,
            patch.object(client, 'get_notion_headers', return_value={'Authorization': 'a'}) as headers,
            # 캐시 시계를 고정해도 실제 요청 제한기의 시계/대기는 실행하지 않는다.
            patch.object(client, 'notion_request_slot'),
            patch.object(client.time, 'monotonic', return_value=100) as clock,
        ):
            self.assertEqual(client.fetch_notion_user_name('u'), '')
            self.assertEqual(client.fetch_notion_user_name('u'), '김지원')
            self.assertEqual(client.fetch_notion_user_name('u'), '김지원')
            self.assertEqual(get.call_count, 2)
            headers.return_value = {'Authorization': 'b'}
            client.fetch_notion_user_name('u')
            clock.return_value = 400
            client.fetch_notion_user_name('u')
            self.assertEqual(get.call_count, 4)
