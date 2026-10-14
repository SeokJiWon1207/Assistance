import os
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock
from unittest.mock import Mock, patch

import requests
from fastapi import HTTPException

from backend.sources import mantis, mantis_collection


class MantisCollectionTests(unittest.TestCase):
    def setUp(self):
        mantis.clear_mantis_recent_cache()
        self.settings = {"url": "https://mantis.test/issues", "token": "first"}
        self.enterContext(patch.object(mantis, "get_mantis_settings", return_value=self.settings))
        self.enterContext(patch.dict(os.environ, {
            "MANTIS_LIVE_MAX_WORKERS": "6", "MANTIS_LIVE_CACHE_TTL_SECONDS": "30",
        }))

    def tearDown(self):
        mantis.clear_mantis_recent_cache()

    @staticmethod
    def page(params, **kwargs):
        return [{"id": params["page"] * 100 + n, "handler": {"name": "original"}}
                for n in range(params["page_size"])]

    def test_six_pages_run_concurrently_and_keep_the_original_unsorted_window(self):
        barrier = Barrier(6)

        def fetch(params, **kwargs):
            if params["page"] <= 6:
                barrier.wait(timeout=3)
            return self.page(params)

        with patch.object(mantis, "fetch_mantis_issues", side_effect=fetch) as api:
            rows = mantis.fetch_mantis_recent_issues(750)
        expected = [row for page in range(1, 9) for row in self.page({"page": page, "page_size": 100})][:750]
        self.assertEqual(rows, sorted(expected, key=lambda row: row["id"], reverse=True))
        self.assertEqual(api.call_count, 8)

    def test_cache_is_copied_and_ttl_refresh_credentials_and_range_are_respected(self):
        with (
            patch.object(mantis, "fetch_mantis_issues", side_effect=self.page) as api,
            patch.object(mantis_collection.time, "monotonic", return_value=100) as clock,
        ):
            rows = mantis.fetch_mantis_recent_issues(100)
            rows[0]["handler"]["name"] = "mutated"
            self.assertEqual(mantis.fetch_mantis_recent_issues(100)[0]["handler"]["name"], "original")
            self.assertEqual(api.call_count, 1)
            mantis.fetch_mantis_recent_issues(200)
            self.assertEqual(api.call_count, 3)
            clock.return_value = 131
            mantis.fetch_mantis_recent_issues(100)
            self.assertEqual(api.call_count, 4)
            mantis.fetch_mantis_recent_issues(100, force_refresh=True)
            self.assertEqual(api.call_count, 5)
            self.settings["token"] = "second"
            mantis.fetch_mantis_recent_issues(100)
            self.assertEqual(api.call_count, 6)
            self.settings["url"] = "https://other.test/issues"
            mantis.fetch_mantis_recent_issues(100)
            self.assertEqual(api.call_count, 7)

    def test_disabled_cache_always_fetches(self):
        with patch.dict(os.environ, {"MANTIS_LIVE_CACHE_TTL_SECONDS": "0"}), patch.object(
            mantis, "fetch_mantis_issues", side_effect=self.page,
        ) as api:
            mantis.fetch_mantis_recent_issues(100)
            mantis.fetch_mantis_recent_issues(100)
        self.assertEqual(api.call_count, 2)

    def test_disabled_cache_does_not_serialize_simultaneous_requests(self):
        barrier = Barrier(3)

        def fetch(params, **kwargs):
            barrier.wait(timeout=3)
            return [{"id": 1}]

        with (
            patch.dict(os.environ, {"MANTIS_LIVE_CACHE_TTL_SECONDS": "0"}),
            patch.object(mantis, "fetch_mantis_issues", side_effect=fetch) as api,
            ThreadPoolExecutor(3) as pool,
        ):
            results = list(pool.map(lambda _: mantis.fetch_mantis_recent_issues(1), range(3)))
        self.assertEqual(results, [[{"id": 1}]] * 3)
        self.assertEqual(api.call_count, 3)

    def test_page_scan_collects_all_pages_once_then_filters_cached_raw_rows(self):
        def fetch(params, **kwargs):
            page = params["page"]
            self.assertEqual(params["page_size"], 50)
            self.assertEqual(kwargs["timeout"], mantis.MANTIS_PAGE_SCAN_TIMEOUT_SECONDS)
            return [
                {"id": page * 100 + n, "summary": "차트" if n % 2 == 0 else "주문",
                 "project": {"name": "112KBStar(KB증권)" if n % 2 == 0 else "동부증권"},
                 "status": {"name": "confirmed"}, "handler": {"real_name": "담당자"}}
                for n in range(50)
            ]

        with patch.object(mantis, "fetch_mantis_issues", side_effect=fetch) as api:
            first = mantis.execute_mantis_page_scan("차트", 5, {"customer_exact": "112KBStar(KB증권)"})
            second = mantis.execute_mantis_page_scan("주문", 3, {"customer_exact": "동부증권"})
            raw = mantis._fetch_mantis_raw_issues(800, 50, mantis.MANTIS_PAGE_SCAN_TIMEOUT_SECONDS)
        self.assertEqual(api.call_count, 16)
        self.assertEqual(first["meta"]["found_count"], 400)
        self.assertEqual(second["meta"]["found_count"], 400)
        self.assertEqual([row["id"] for row in first["issues_list"]], [1648, 1646, 1644, 1642, 1640])
        self.assertEqual([row["id"] for row in second["issues_list"]], [1649, 1647, 1645])
        self.assertEqual(first["meta"]["searched_pages"], 16)
        self.assertEqual(len(first["meta"]["matched_customer_groups"]), 400)
        self.assertTrue(all("_assistant_match_score" not in row for row in raw))

    def test_keyword_fallback_reuses_only_first_api_page_not_highest_ids(self):
        def fetch(params, **kwargs):
            page = params["page"]
            if page == 1:
                return [{"id": n + 1, "summary": "차트"} for n in range(50)]
            if page == 2:
                return [{"id": 9999, "summary": "차트"}]
            return []

        with patch.object(mantis, "fetch_mantis_issues", side_effect=fetch) as api:
            stage = mantis.execute_mantis_page_scan("차트 없는검색어", 5, {})
            before = api.call_count
            repeated = mantis.execute_mantis_page_scan("차트 없는검색어", 5, {})
        self.assertEqual(api.call_count, before)
        self.assertEqual(stage, repeated)
        self.assertEqual([row["id"] for row in stage["issues_list"]], [50, 49, 48, 47, 46])
        self.assertEqual(stage["meta"]["found_count"], 50)
        self.assertEqual(stage["meta"]["searched_pages"], 2)

    def test_retrieval_pipeline_uses_shared_page_cache_for_followup_limit(self):
        from backend import retrieval

        def fetch(params, **kwargs):
            return [
                {**row, "summary": "차트 오류", "handler": {"real_name": "석지원"}, "project": {"name": "112KBStar(KB증권)"},
                 "custom_fields": [{"field": {"name": "매체구분"}, "value": "HTS"}]}
                for row in self.page(params)
            ]

        skipped = {"status": "skipped", "issues_list": [], "meta": {}}
        with (
            patch.object(mantis, "fetch_mantis_issues", side_effect=fetch) as api,
            patch.object(retrieval, "execute_mantis_snapshot_search", return_value=skipped),
            patch.object(retrieval, "execute_mantis_filter_search", return_value=skipped),
        ):
            first = retrieval.run_retrieval_pipeline("KB증권 HTS 최근 이슈 5개 보여줘", 5, {"mantis": True})
            second = retrieval.run_retrieval_pipeline("KB증권 HTS 최근 이슈 3개 보여줘", 3, {"mantis": True})
        stages = [next(s for s in result["stages"] if s.get("source") == "mantis_page_scan")
                  for result in (first, second)]
        self.assertEqual(api.call_count, 16)
        self.assertEqual([s["meta"]["found_count"] for s in stages], [800, 800])
        self.assertEqual([len(s["issues_list"]) for s in stages], [5, 3])

    def test_page_scan_failure_does_not_return_a_partial_success_or_cache_it(self):
        def fetch(params, **kwargs):
            if params["page"] == 2:
                raise requests.Timeout("required page failed")
            return self.page(params)

        with patch.object(mantis, "fetch_mantis_issues", side_effect=fetch) as api:
            with self.assertRaises(HTTPException) as raised:
                mantis.execute_mantis_page_scan("", 5, {})
            self.assertEqual(raised.exception.status_code, 502)
            api.side_effect = self.page
            stage = mantis.execute_mantis_page_scan("", 5, {})
        self.assertEqual(stage["meta"]["found_count"], 800)
        self.assertEqual(sum(call.args[0]["page"] == 1 for call in api.call_args_list), 2)

    def test_empty_page_after_full_page_and_status_excluded_counts(self):
        def fetch(params, **kwargs):
            if params["page"] != 1:
                return []
            return [{"id": n + 1, "summary": "차트", "status": {"name": "closed"}}
                    for n in range(50)]

        with patch.object(mantis, "fetch_mantis_issues", side_effect=fetch):
            stage = mantis.execute_mantis_page_scan("차트", 5, {"statuses": ["confirmed"]})
        self.assertEqual(stage["meta"]["found_count"], 0)
        self.assertEqual(stage["meta"]["status_excluded_count"], 50)
        self.assertEqual(stage["meta"]["searched_pages"], 2)

    def test_partial_failure_does_not_cache_and_retries_the_entire_range(self):
        def fail(params, **kwargs):
            if params["page"] == 2:
                raise requests.Timeout("slow")
            return self.page(params)

        with patch.object(mantis, "fetch_mantis_issues", side_effect=fail) as api:
            with self.assertRaises(requests.Timeout):
                mantis.fetch_mantis_recent_issues(200)
            api.side_effect = self.page
            self.assertEqual(len(mantis.fetch_mantis_recent_issues(200)), 200)
            self.assertEqual(api.call_count, 4)

    def test_end_page_ignores_speculative_failures_but_not_required_pages(self):
        def fetch(params, **kwargs):
            if params["page"] == 1:
                return [{"id": 1}]
            raise requests.Timeout("past end")

        with patch.object(mantis, "fetch_mantis_issues", side_effect=fetch):
            self.assertEqual(mantis.fetch_mantis_recent_issues(800), [{"id": 1}])

    def test_simultaneous_callers_share_one_collection(self):
        entered, release = Event(), Event()

        def fetch(params, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return self.page(params)

        with patch.object(mantis, "fetch_mantis_issues", side_effect=fetch) as api, ThreadPoolExecutor(2) as pool:
            first = pool.submit(mantis.fetch_mantis_recent_issues, 100)
            self.assertTrue(entered.wait(3))
            second = pool.submit(mantis.fetch_mantis_recent_issues, 100)
            release.set()
            self.assertEqual(first.result(3), second.result(3))
            self.assertEqual(api.call_count, 1)

    def test_page_drift_restarts_once_then_fails_instead_of_double_counting(self):
        with patch.object(mantis, "fetch_mantis_issues", return_value=[{"id": 1}]) as api:
            with self.assertRaises(HTTPException):
                mantis.fetch_mantis_recent_issues(2, page_size=1)
            self.assertEqual(api.call_count, 4)
            api.side_effect = self.page
            self.assertEqual(len(mantis.fetch_mantis_recent_issues(2, page_size=1)), 2)
            self.assertEqual(api.call_count, 6)

    def test_page_drift_can_recover_without_caching_the_duplicate_generation(self):
        generation = 0
        lock = Lock()

        def fetch(params, **kwargs):
            nonlocal generation
            with lock:
                if params["page"] == 1:
                    generation += 1
                return [{"id": 1 if generation == 1 else params["page"]}]

        with patch.dict(os.environ, {"MANTIS_LIVE_MAX_WORKERS": "1"}), patch.object(
            mantis, "fetch_mantis_issues", side_effect=fetch,
        ) as api:
            self.assertEqual([row["id"] for row in mantis.fetch_mantis_recent_issues(2, page_size=1)], [2, 1])
            self.assertEqual(api.call_count, 4)

    def test_slow_collection_does_not_get_a_fresh_ttl_on_completion(self):
        with patch.object(mantis_collection.time, "monotonic", return_value=100) as clock:
            def fetch(params, **kwargs):
                clock.return_value = 140
                return self.page(params)

            with patch.object(mantis, "fetch_mantis_issues", side_effect=fetch) as api:
                mantis.fetch_mantis_recent_issues(100)
                mantis.fetch_mantis_recent_issues(100)
                self.assertEqual(api.call_count, 2)

    def test_parallel_collection_keeps_http_retry_behavior(self):
        response = Mock(status_code=200)
        response.json.return_value = {"issues": [{"id": 10}]}
        with (
            patch.object(mantis, "get_mantis_headers", return_value={}),
            patch.object(mantis.requests, "get", side_effect=[requests.Timeout(), Mock(status_code=429), response]) as get,
            patch.object(mantis, "interruptible_sleep") as sleep,
        ):
            self.assertEqual(mantis.fetch_mantis_recent_issues(1), [{"id": 10}])
            self.assertEqual(get.call_count, 3)
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_ordered_prefetch_keeps_page_order_and_bounds_in_flight_requests(self):
        second_page_done = Event()
        lock = Lock()
        in_flight = {"now": 0, "peak": 0}

        def fetch(page):
            with lock:
                in_flight["now"] += 1
                in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            try:
                # 1쪽이 2쪽보다 늦게 끝나도 결과는 1쪽부터 나와야 한다.
                if page == 1:
                    self.assertTrue(second_page_done.wait(timeout=3))
                elif page == 2:
                    second_page_done.set()
                return [{"id": page}]
            finally:
                with lock:
                    in_flight["now"] -= 1

        pages = list(mantis_collection.iter_pages_in_order(fetch, max_pages=5, workers=3))

        self.assertEqual([page for page, _ in pages], [1, 2, 3, 4, 5])
        self.assertEqual([rows for _, rows in pages], [[{"id": n}] for n in range(1, 6)])
        self.assertLessEqual(in_flight["peak"], 3)

    def test_ordered_prefetch_ignores_errors_after_stop_but_raises_consumed_page_errors(self):
        requested = []

        def fetch(page):
            requested.append(page)
            if page >= 3:
                raise RuntimeError(f"page {page} failed")
            return [{"id": page}]

        seen = []
        with closing(mantis_collection.iter_pages_in_order(fetch, max_pages=50, workers=4)) as pages:
            for page, _rows in pages:
                seen.append(page)
                if page == 2:
                    break
        self.assertEqual(seen, [1, 2])
        # 2쪽을 내줄 때까지 선요청 창(4쪽)만큼만 요청한다.
        self.assertLessEqual(max(requested), 5)

        with self.assertRaisesRegex(RuntimeError, "page 3 failed"):
            for _page in mantis_collection.iter_pages_in_order(fetch, max_pages=50, workers=4):
                pass
