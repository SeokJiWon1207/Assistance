"""중복 제목 조회 제거, 권한 분리, 만료 및 쓰기 이후 재조회 회귀."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock, patch

import requests

from backend.notion import query_cache


def response(status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps({"results": [{"id": "p1"}], "has_more": False}).encode()
    return result


class NotionQueryCacheTests(unittest.TestCase):
    def setUp(self):
        query_cache.clear_notion_query_cache()
        self.addCleanup(query_cache.clear_notion_query_cache)
        self.headers = patch.object(query_cache, "get_notion_headers", return_value={"Authorization": "Bearer a"})
        self.headers_mock = self.headers.start()
        self.addCleanup(self.headers.stop)
        self.env = patch.dict("os.environ", {"NOTION_TITLE_QUERY_CACHE_SECONDS": "60"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_repeated_query_reuses_an_independent_response_and_expires(self):
        send = Mock(side_effect=lambda *a, **kw: response())
        with patch.object(query_cache.time, "monotonic", return_value=100) as clock:
            first = query_cache.cached_title_query("/db/query", {"title": "계획"}, send)
            cached = query_cache.cached_title_query("/db/query", {"title": "계획"}, send)
            self.assertEqual(send.call_count, 1)
            self.assertEqual(first.json(), cached.json())
            self.assertIn("X-Assistance-Cache-Age", cached.headers)
            clock.return_value = 160
            query_cache.cached_title_query("/db/query", {"title": "계획"}, send)
            self.assertEqual(send.call_count, 2)

    def test_auth_cursor_and_write_invalidation_require_a_new_query(self):
        send = Mock(side_effect=lambda *a, **kw: response())
        query_cache.cached_title_query("/db/query", {}, send)
        self.headers_mock.return_value = {"Authorization": "Bearer b"}
        query_cache.cached_title_query("/db/query", {}, send)
        query_cache.cached_title_query("/db/query", {"start_cursor": "p2"}, send)
        query_cache.clear_notion_query_cache()
        query_cache.cached_title_query("/db/query", {}, send)
        self.assertEqual(send.call_count, 4)

    def test_failed_queries_are_not_cached(self):
        send = Mock(side_effect=[response(503), response()])
        self.assertEqual(query_cache.cached_title_query("/db/query", {}, send).status_code, 503)
        self.assertEqual(query_cache.cached_title_query("/db/query", {}, send).status_code, 200)
        self.assertEqual(send.call_count, 2)

    def test_concurrent_identical_queries_share_one_request(self):
        started, release = Event(), Event()

        def send(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(2))
            return response()

        send_mock = Mock(side_effect=send)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(query_cache.cached_title_query, "/db/query", {}, send_mock)
            self.assertTrue(started.wait(2))
            second = pool.submit(query_cache.cached_title_query, "/db/query", {}, send_mock)
            release.set()
            self.assertEqual(first.result().json(), second.result().json())
        self.assertEqual(send_mock.call_count, 1)
