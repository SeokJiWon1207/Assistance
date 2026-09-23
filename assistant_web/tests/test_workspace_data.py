import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from pathlib import Path
from unittest.mock import patch

from backend import workspace_data


class WorkspaceDataCacheTests(unittest.TestCase):
    def tearDown(self):
        workspace_data.clear_workspace_snapshot_cache()
        workspace_data.clear_workspace_live_cache()

    def test_shared_raw_cache_still_filters_each_person_independently(self):
        from backend.workspace import _my_mantis_issues
        rows = [
            {"id": 2, "handler": {"real_name": "석지원"}, "summary": "A"},
            {"id": 1, "handler": {"real_name": "김지원"}, "summary": "B"},
        ]
        with (
            patch.object(workspace_data, "get_mantis_settings", return_value={"url": "u", "token": "t"}),
            patch.object(workspace_data, "fetch_mantis_issues", return_value=rows) as api,
        ):
            first = _my_mantis_issues({"mantisUsername": "석지원"})
            second = _my_mantis_issues({"mantisUsername": "김지원"})
        self.assertEqual([row["id"] for row in first[1]], [2])
        self.assertEqual([row["id"] for row in second[1]], [1])
        api.assert_called_once()

    def test_live_data_reuses_larger_window_and_refresh_expiry_connection_invalidate(self):
        settings = {"url": "https://mantis.test/issues", "token": "first"}
        def fetch(params, **kwargs):
            return [{"id": 2000 - (params["page"] - 1) * 100 - n, "handler": {"name": "user"}} for n in range(100)]
        with (
            patch.object(workspace_data, "get_mantis_settings", return_value=settings),
            patch.object(workspace_data, "fetch_mantis_issues", side_effect=fetch) as api,
            patch.object(workspace_data.time, "monotonic", return_value=100) as clock,
        ):
            team = workspace_data.fetch_workspace_recent_issues(1000)
            personal = workspace_data.fetch_workspace_recent_issues(600)
            self.assertEqual(personal, team[:600])
            self.assertEqual(api.call_count, 10)
            personal[0]["handler"]["name"] = "changed"
            self.assertEqual(workspace_data.fetch_workspace_recent_issues(600)[0]["handler"]["name"], "user")
            workspace_data.fetch_workspace_recent_issues(1000, force_refresh=True)
            self.assertEqual(api.call_count, 20)
            clock.return_value = 161
            workspace_data.fetch_workspace_recent_issues(600)
            self.assertEqual(api.call_count, 26)
            settings["token"] = "second"
            workspace_data.fetch_workspace_recent_issues(600)
            self.assertEqual(api.call_count, 32)

    def test_larger_request_does_not_mix_old_and_new_pages(self):
        with (
            patch.object(workspace_data, "get_mantis_settings", return_value={"url": "u", "token": "t"}),
            patch.object(workspace_data, "fetch_mantis_issues", return_value=[{"id": n} for n in range(100)]) as api,
        ):
            workspace_data.fetch_workspace_recent_issues(100)
            workspace_data.fetch_workspace_recent_issues(200)
        self.assertEqual([c.args[0]["page"] for c in api.call_args_list], [1, 1, 2])

    def test_parallel_pages_keep_order_even_if_later_page_finishes_first(self):
        third_finished = Event()
        def fetch(params, **kwargs):
            page = params["page"]
            if page == 2:
                self.assertTrue(third_finished.wait(3))
            if page == 3:
                third_finished.set()
            size = 10 if page == 3 else 100
            return [{"id": 1000 - (page - 1) * 100 - n} for n in range(size)]
        with (
            patch.object(workspace_data, "get_mantis_settings", return_value={"url": "u", "token": "t"}),
            patch.object(workspace_data, "fetch_mantis_issues", side_effect=fetch) as api,
        ):
            rows = workspace_data.fetch_workspace_recent_issues(300)
        self.assertEqual([row["id"] for row in rows], list(range(1000, 790, -1)))
        self.assertEqual(api.call_count, 3)

    def test_failed_later_page_discards_partial_cache(self):
        with (
            patch.object(workspace_data, "get_mantis_settings", return_value={"url": "u", "token": "t"}),
            patch.object(workspace_data, "fetch_mantis_issues", side_effect=[
                [{"id": n} for n in range(100)], RuntimeError("page failed"), [{"id": 999}],
            ]) as api,
        ):
            with self.assertRaises(RuntimeError):
                workspace_data.fetch_workspace_recent_issues(200)
            self.assertEqual(workspace_data.fetch_workspace_recent_issues(200), [{"id": 999}])
            self.assertEqual(api.call_args_list[-1].args[0]["page"], 1)

    def test_speculative_page_past_end_cannot_discard_completed_results(self):
        def fetch(params, **kwargs):
            if params["page"] == 1:
                return [{"id": n} for n in range(100)]
            if params["page"] == 2:
                return []
            raise RuntimeError("past end")
        with (
            patch.object(workspace_data, "get_mantis_settings", return_value={"url": "u", "token": "t"}),
            patch.object(workspace_data, "fetch_mantis_issues", side_effect=fetch),
        ):
            self.assertEqual(len(workspace_data.fetch_workspace_recent_issues(300)), 100)

    def test_concurrent_requests_share_one_fetch_and_failures_are_not_cached(self):
        entered, release = Event(), Event()
        def fetch(*args, **kwargs):
            entered.set()
            self.assertTrue(release.wait(3))
            return [{"id": 1}]
        with (
            patch.object(workspace_data, "get_mantis_settings", return_value={"url": "u", "token": "t"}),
            patch.object(workspace_data, "fetch_mantis_issues", side_effect=fetch) as api,
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            first = pool.submit(workspace_data.fetch_workspace_recent_issues)
            self.assertTrue(entered.wait(3))
            second = pool.submit(workspace_data.fetch_workspace_recent_issues)
            release.set()
            self.assertEqual(first.result(3), second.result(3))
            self.assertEqual(api.call_count, 1)
            api.side_effect = RuntimeError("offline")
            with self.assertRaises(RuntimeError):
                workspace_data.fetch_workspace_recent_issues(force_refresh=True)
            api.side_effect = None
            api.return_value = [{"id": 2}]
            self.assertEqual(workspace_data.fetch_workspace_recent_issues(), [{"id": 2}])

    def test_snapshot_is_parsed_once_until_its_files_change(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "chroma.sqlite3"
            index_path = Path(directory) / "mantis_index.sqlite3"
            database_path.write_bytes(b"first")
            index_path.write_bytes(b"index")
            snapshot = {
                "available": True,
                "matched_count": 1,
                "issues": [{"id": 1}],
                "reason": "",
            }
            with (
                patch.object(workspace_data, "CHROMA_DB_PATH", database_path),
                patch.dict(
                    os.environ,
                    {"MANTIS_INDEX_DB_PATH": str(index_path), "MANTIS_INDEX_MODE": "primary"},
                ),
                patch.object(workspace_data, "scan_mantis_documents", return_value=snapshot) as scan,
            ):
                first = workspace_data.load_workspace_snapshot()
                second = workspace_data.load_workspace_snapshot()
                database_path.write_bytes(b"second-version")
                third = workspace_data.load_workspace_snapshot()

        self.assertEqual(first, second)
        self.assertEqual(third[0], [{"id": 1}])
        self.assertEqual(scan.call_count, 2)
