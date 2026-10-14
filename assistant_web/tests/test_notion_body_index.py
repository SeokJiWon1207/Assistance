"""본문 색인의 영속성, 연결 격리와 갱신 실패 경계를 검증한다."""
from contextlib import closing

import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch


def page(identifier="page-1", revision="2026-10-12T00:00:00Z", database="db-1", title=""):
    return {"id": identifier, "object": "page", "last_edited_time": revision,
            "parent": {"type": "database_id", "database_id": database},
            "properties": {"제목": {"type": "title", "title": [{"plain_text": title}]}},
            "url": f"https://www.notion.so/{identifier}"}


def segment(text="본문 증거", identifier="block-1", owner="page-1", kind="paragraph"):
    return {"block_id": identifier, "owner_page_id": owner, "page_id": owner,
            "type": kind, "text": text}


class NotionBodyIndexTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.notion.body_index"),
                             "영속적인 Notion 본문 색인 모듈이 필요합니다")
        from backend.notion.body_index import BodyIndex
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "body.sqlite3"
        self.index = BodyIndex(self.path, fingerprint="connection-a")

    def add_complete(self, raw=None, segments=None):
        raw = raw or page()
        self.index.upsert_catalog(raw["parent"]["database_id"], [raw], complete=True)
        self.assertTrue(self.index.replace_content(raw["id"], segments or [segment()], complete=True,
                                                 expected_edited_time=raw["last_edited_time"]))

    def test_empty_title_body_is_durable_and_preserves_block_type(self):
        from backend.notion.body_index import BodyIndex
        self.add_complete(segments=[segment("q()", kind="code"),
                                    segment("가 나", identifier="row-1", kind="table_row")])
        reopened = BodyIndex(self.path, fingerprint="connection-a")
        found = reopened.search(["db-1"], ["q()"])
        self.assertEqual([item["page_id"] for item in found], ["page1"])
        self.assertEqual(found[0]["raw_page"]["properties"]["제목"]["title"][0]["plain_text"], "")
        self.assertEqual([item["type"] for item in found[0]["segments"]], ["code", "table_row"])
        self.assertTrue(found[0]["trusted"])
        self.assertTrue(reopened.status(["db-1"])["complete"])
        self.assertEqual(reopened.pending_pages(["db-1"]), [])

    def test_split_coverage_unknown_legacy_and_scope_are_not_invented(self):
        self.add_complete()
        other = page("other", database="outside")
        self.index.upsert_catalog("outside", [other], complete=True)
        self.index.replace_content("other", [segment(owner="other")], complete=False,
            expected_edited_time=other["last_edited_time"],
            coverage={"supported_content_complete": True,
                      "unsupported_elements": [{"block_id": "b", "block_type": "button"}],
                      "incomplete_reasons": ["unsupported_block"]})
        self.index.record_failure("other", "unsupported_block")
        status = self.index.status(["db-1"])
        self.assertEqual(status["supported_collected_page_count"], 1)
        self.assertEqual(status["detailed_coverage_page_count"], 0)
        self.assertEqual(status["unsupported_element_counts"], {})
        self.index.replace_content("page-1", [segment()], complete=False,
                                   expected_edited_time=page()["last_edited_time"])
        self.index.record_failure("page-1", "unsupported_block")
        status = self.index.status(["db-1"])
        self.assertEqual(status["unsupported_element_page_count"], 1)
        self.assertEqual(status["unsupported_subtype_unknown_page_count"], 1)
        self.assertEqual(status["supported_collected_page_count"], 0)

    def test_status_memory_does_not_scale_with_raw_page_payloads(self):
        self.add_complete()
        expected = self.index.status(["db-1"])
        raw = page()
        raw["large_property"] = "x" * (12 * 1024 * 1024)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE pages SET raw_page=?", (json.dumps(raw),))
        del raw
        tracemalloc.start()
        try:
            actual = self.index.status(["db-1"])
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(actual, expected)
        self.assertLess(peak, 4 * 1024 * 1024)

    def test_changed_revision_does_not_reuse_supported_coverage(self):
        raw = page()
        self.index.upsert_catalog("db-1", [raw], complete=True)
        self.index.replace_content(raw["id"], [segment()], complete=False,
            expected_edited_time=raw["last_edited_time"], coverage={"supported_content_complete": True})
        self.assertEqual(self.index.status(["db-1"])["supported_collected_page_count"], 1)
        self.index.upsert_page("db-1", page(revision="2026-10-13T00:00:00Z"))
        self.assertEqual(self.index.status(["db-1"])["supported_collected_page_count"], 0)

    def test_legacy_writer_error_cannot_leave_supported_completion(self):
        raw = page()
        self.index.upsert_catalog("db-1", [raw], complete=True)
        self.index.replace_content(raw["id"], [segment()], complete=False,
            expected_edited_time=raw["last_edited_time"], coverage={"supported_content_complete": True,
                "incomplete_reasons": ["unsupported_block"]})
        self.index.record_failure(raw["id"], "unsupported_block")
        self.assertEqual(self.index.status(["db-1"])["supported_collected_page_count"], 1)
        # 이미 실행 중인 구버전 수집기는 선택적 진단 테이블을 모른다.
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE pages SET last_error='http_429', failure_count=failure_count+1")
        self.assertEqual(self.index.status(["db-1"])["supported_collected_page_count"], 0)

    def test_changed_collection_policy_requires_revalidation_without_losing_candidates(self):
        from backend.notion import body_index
        self.add_complete()
        with patch.object(body_index, "CONTENT_POLICY_VERSION", body_index.CONTENT_POLICY_VERSION + 1, create=True):
            self.assertFalse(self.index.read_page("page-1")["trusted"])
            status = self.index.status(["db-1"])
            self.assertEqual(status["indexed_page_count"], 0)
            self.assertEqual(status["policy_pending_page_count"], 1)
            self.assertEqual(len(self.index.pending_pages(["db-1"])), 1)
            self.assertEqual(len(self.index.search(["db-1"], ["본문"])), 1)
            self.add_complete()
            self.assertTrue(self.index.read_page("page-1")["trusted"])
            self.assertEqual(self.index.status(["db-1"])["policy_pending_page_count"], 0)

    def test_legacy_policy_is_unknown_until_body_is_read_again(self):
        import sqlite3
        from backend.notion import body_index
        self.add_complete()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE content_policies")
        old = body_index.BodyIndex(self.path, fingerprint="connection-a", create=False)
        self.assertFalse(old.read_page("page-1")["content_complete"])
        self.assertEqual(old.status(["db-1"])["policy_pending_page_count"], 1)
        self.assertEqual(len(old.pending_pages(["db-1"])), 1)
        self.assertEqual(len(old.search(["db-1"], ["본문"])), 1)
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='content_policies'").fetchone())
        old.upsert_catalog("db-1", [page()], complete=True)
        self.assertEqual(old.status(["db-1"])["policy_pending_page_count"], 1)
        old.replace_content("page-1", [segment("절반")], complete=False,
                            expected_edited_time=page()["last_edited_time"])
        self.assertFalse(old.read_page("page-1")["trusted"])
        self.assertEqual(old.status(["db-1"])["policy_pending_page_count"], 0)
        self.assertEqual(old.status(["db-1"])["pending_page_count"], 1)

    def test_literal_search_is_casefolded_or_and_does_not_interpret_sql_wildcards(self):
        self.add_complete(segments=[segment("가 나 Q() 100% a_b ' OR 1=1 --")])
        for term in ["가나", "q()", "%", "a_b", "' OR 1=1 --"]:
            with self.subTest(term=term):
                self.assertEqual(len(self.index.search(["db-1"], ["없는문구", term])), 1)
        self.assertEqual(self.index.search(["db-1"], ["%없는"]), [])
        self.assertEqual(self.index.search(["db-2"], ["가나"]), [])

    def test_foreign_owned_blocks_never_enter_search_or_content(self):
        self.add_complete(segments=[segment("own"), segment("secret", "foreign", "page-2")])
        self.assertEqual(self.index.search(["db-1"], ["secret"]), [])
        record = self.index.read_page("page-1")
        self.assertEqual(record["content"], "own")
        self.assertEqual(len(record["segments"]), 1)

    def test_connection_identity_never_exposes_other_connection_content(self):
        from backend.notion.body_index import BodyIndex
        self.add_complete()
        other = BodyIndex(self.path, fingerprint="connection-b")
        self.assertIsNone(other.read_page("page-1"))
        self.assertEqual(other.search(["db-1"], ["본문"]), [])
        self.assertFalse(other.status(["db-1"])["complete"])
        other.upsert_catalog("db-1", [], complete=True)
        self.assertIsNotNone(self.index.read_page("page-1"))

    def test_default_fingerprint_changes_with_auth_version_base_roots_and_child_policy(self):
        from backend.notion import body_index
        settings = {"token": "secret-do-not-persist", "version": "v1", "database_ids": ["DB-1", "DB-2"]}
        with patch.object(body_index, "get_notion_settings", return_value=settings), \
                patch.dict("os.environ", {"NOTION_INCLUDE_CHILD_DATABASES": "false"}):
            first = body_index.BodyIndex(self.path)
            first.upsert_catalog("db-1", [page()], complete=True)
            fingerprint = first.fingerprint
            settings["database_ids"] = ["db2", "db1"]
            self.assertEqual(body_index.BodyIndex(self.path).fingerprint, fingerprint)
            for key, value in [("token", "different"), ("version", "v2"), ("database_ids", ["db1"])]:
                old = settings[key]
                settings[key] = value
                self.assertNotEqual(body_index.BodyIndex(self.path).fingerprint, fingerprint)
                settings[key] = old
            with patch.object(body_index, "NOTION_API_BASE", "https://other.invalid"):
                self.assertNotEqual(body_index.BodyIndex(self.path).fingerprint, fingerprint)
            with patch.dict("os.environ", {"NOTION_INCLUDE_CHILD_DATABASES": "true"}):
                self.assertNotEqual(body_index.BodyIndex(self.path).fingerprint, fingerprint)
        for path in self.path.parent.glob("body.sqlite3*"):
            self.assertNotIn(b"secret-do-not-persist", path.read_bytes())

    def test_revision_change_marks_old_body_untrusted_until_matching_refresh(self):
        self.add_complete()
        changed = page(revision="2026-10-12T01:00:00Z")
        self.index.upsert_catalog("db-1", [changed], complete=True)
        record = self.index.read_page("page-1")
        self.assertEqual(record["content"], "본문 증거")
        self.assertFalse(record["trusted"])
        self.assertTrue(record["needs_refresh"])
        self.assertEqual(len(self.index.pending_pages(["db-1"])), 1)
        self.assertFalse(self.index.replace_content("page-1", [segment("raced")], complete=True,
                                                   expected_edited_time=page()["last_edited_time"]))
        self.assertEqual(self.index.read_page("page-1")["content"], "본문 증거")
        self.assertTrue(self.index.replace_content("page-1", [segment("new")], complete=True,
                                                  expected_edited_time=changed["last_edited_time"]))
        self.assertTrue(self.index.read_page("page-1")["trusted"])
        self.assertEqual(self.index.search(["db-1"], ["증거"]), [])

    def test_partial_body_preserves_prior_complete_content_and_marks_incomplete(self):
        self.add_complete()
        self.index.replace_content("page-1", [segment("half")], complete=False,
                                   expected_edited_time=page()["last_edited_time"])
        record = self.index.read_page("page-1")
        self.assertEqual(record["content"], "본문 증거")
        self.assertFalse(record["trusted"])
        self.assertFalse(self.index.status(["db-1"])["complete"])
        self.index.record_failure("page-1")
        self.assertEqual(self.index.status(["db-1"])["failed_page_count"], 1)

    def test_changed_and_added_blocks_in_partial_refresh_remain_discoverable(self):
        self.add_complete(segments=[segment("oldword", kind="paragraph")])
        changed = page(revision="new-revision")
        self.index.upsert_page("db-1", changed)
        partial = [segment("newword()", kind="code"),
                   segment("새로운 표", identifier="row-2", kind="table_row"),
                   segment("foreignword", identifier="foreign", owner="other-page")]
        for _ in range(2):
            self.index.replace_content("page-1", partial, complete=False, expected_edited_time="new-revision")
            for term in ("oldword", "newword()", "새로운표"):
                with self.subTest(term=term):
                    found = self.index.search(["db-1"], [term])
                    self.assertEqual(len(found), 1)
                    self.assertFalse(found[0]["trusted"])
        self.assertEqual(self.index.search(["db-1"], ["foreignword"]), [])
        record = self.index.read_page("page-1")
        self.assertEqual(record["content"], "oldword")
        candidate = self.index.search(["db-1"], ["newword"])[0]
        evidence = [item for item in candidate["segments"] if item["text"] == "newword()"]
        self.assertEqual(len(evidence), 1)
        self.assertEqual(evidence[0]["block_id"], "block1")
        self.assertEqual(evidence[0]["owner_page_id"], "page1")
        self.assertEqual(evidence[0]["type"], "code")
        self.assertEqual(evidence[0]["snapshot"], "partial")
        self.assertEqual(evidence[0]["indexed_edited_time"], "new-revision")
        self.assertEqual(candidate["candidate_segments"], candidate["segments"])

    def test_empty_partial_failure_preserves_last_partial_candidates(self):
        for prior_complete in (False, True):
            with self.subTest(prior_complete=prior_complete):
                self.index.upsert_catalog("db-1", [], complete=True)
                self.index.upsert_catalog("db-1", [page()], complete=True)
                if prior_complete:
                    self.index.replace_content("page-1", [segment("oldword")], complete=True,
                                               expected_edited_time=page()["last_edited_time"])
                self.index.replace_content("page-1", [segment("newword")], complete=False,
                                           expected_edited_time=page()["last_edited_time"])
                self.index.replace_content("page-1", [], complete=False,
                                           expected_edited_time=page()["last_edited_time"])
                self.index.record_failure("page-1")
                self.assertEqual(len(self.index.search(["db-1"], ["newword"])), 1)
                self.assertFalse(self.index.read_page("page-1")["trusted"])

    def test_completed_refresh_cleans_partial_candidates_and_old_complete_blocks(self):
        self.add_complete(segments=[segment("oldword")])
        self.index.replace_content("page-1", [segment("newword")], complete=False,
                                   expected_edited_time=page()["last_edited_time"])
        self.assertEqual(len(self.index.search(["db-1"], ["newword"])), 1)
        self.index.replace_content("page-1", [segment("finalword")], complete=True,
                                   expected_edited_time=page()["last_edited_time"])
        self.assertEqual(self.index.search(["db-1"], ["oldword", "newword"]), [])
        self.assertTrue(self.index.search(["db-1"], ["finalword"])[0]["trusted"])
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM partial_blocks").fetchone()[0], 0)

    def test_page_pruning_cascades_partial_blocks_and_connection_filters_still_apply(self):
        from backend.notion.body_index import BodyIndex
        self.add_complete()
        self.index.replace_content("page-1", [segment("private partial")], complete=False,
                                   expected_edited_time=page()["last_edited_time"])
        self.assertEqual(len(self.index.search(["db-1"], ["private"])), 1)
        self.assertEqual(self.index.search(["db-2"], ["private"]), [])
        self.assertEqual(BodyIndex(self.path, fingerprint="connection-b").search(["db-1"], ["private"]), [])
        self.index.upsert_catalog("db-1", [], complete=True)
        self.assertEqual(self.index.search(["db-1"], ["private"]), [])
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM partial_blocks").fetchone()[0], 0)

    def test_existing_schema_without_partial_table_reads_without_migration_then_upgrades_on_write(self):
        from backend.notion.body_index import open_body_index
        self.add_complete()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("DROP TABLE IF EXISTS partial_blocks")
        reopened = open_body_index(self.path, fingerprint="connection-a")
        self.assertIsNotNone(reopened)
        self.assertEqual(len(reopened.search(["db-1"], ["본문"])), 1)
        self.assertEqual(reopened.read_page("page-1")["content"], "본문 증거")
        with closing(sqlite3.connect(self.path)) as db, db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='partial_blocks'").fetchone())
        reopened.replace_content("page-1", [segment("newword")], complete=False,
                                 expected_edited_time=page()["last_edited_time"])
        self.assertEqual(len(reopened.search(["db-1"], ["newword"])), 1)

    def test_partial_refresh_transaction_rolls_back_without_losing_prior_partial_or_complete_body(self):
        from backend.notion.body_index import BodyIndexError
        self.add_complete(segments=[segment("oldword")])
        self.index.replace_content("page-1", [segment("partialword")], complete=False,
                                   expected_edited_time=page()["last_edited_time"])
        self.assertEqual(len(self.index.search(["db-1"], ["partialword"])), 1)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER reject_partial BEFORE INSERT ON partial_blocks "
                       "WHEN NEW.text = 'bad' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(BodyIndexError):
            self.index.replace_content("page-1", [segment("bad")], complete=False,
                                       expected_edited_time=page()["last_edited_time"])
        self.assertEqual(len(self.index.search(["db-1"], ["partialword"])), 1)
        self.assertEqual(self.index.read_page("page-1")["content"], "oldword")

    def test_first_partial_body_keeps_positive_candidates_for_live_validation(self):
        self.index.upsert_catalog("db-1", [page()], complete=True)
        self.index.replace_content("page-1", [segment("first fragment")], complete=False,
                                   expected_edited_time=page()["last_edited_time"])
        candidates = self.index.search(["db-1"], ["fragment"])
        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0]["trusted"])
        self.assertEqual(len(self.index.pending_pages(["db-1"])), 1)

    def test_failed_catalog_preserves_pages_and_complete_catalog_prunes_only_its_database(self):
        self.add_complete()
        self.index.upsert_catalog("db-2", [page("page-2", database="db-2")], complete=True)
        self.index.upsert_catalog("db-1", [], complete=False)
        self.assertIsNotNone(self.index.read_page("page-1"))
        self.assertFalse(self.index.status(["db-1"])["catalog_complete"])
        self.index.upsert_catalog("db-1", [], complete=True)
        self.assertIsNone(self.index.read_page("page-1"))
        self.assertIsNotNone(self.index.read_page("page-2"))
        self.assertTrue(self.index.status(["db-1"])["complete"])

    def test_archived_page_is_removed_even_during_incomplete_catalog_update(self):
        self.add_complete()
        archived = {**page(), "in_trash": True}
        self.index.upsert_catalog("db-1", [archived], complete=False)
        self.assertIsNone(self.index.read_page("page-1"))
        self.assertEqual(self.index.search(["db-1"], ["본문"]), [])

    def test_foreign_parent_catalog_is_rejected_without_pruning_existing_data(self):
        from backend.notion.body_index import BodyIndexError
        self.add_complete()
        with self.assertRaises(BodyIndexError):
            self.index.upsert_catalog("db-1", [page(database="db-2")], complete=True)
        self.assertIsNotNone(self.index.read_page("page-1"))

    def test_failed_block_transaction_rolls_back_without_losing_prior_content(self):
        from backend.notion.body_index import BodyIndexError
        self.add_complete()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("CREATE TRIGGER reject_block BEFORE INSERT ON blocks "
                       "WHEN NEW.text = 'bad' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(BodyIndexError):
            self.index.replace_content("page-1", [segment("bad")], complete=True,
                                       expected_edited_time=page()["last_edited_time"])
        self.assertEqual(self.index.read_page("page-1")["content"], "본문 증거")
        self.assertTrue(self.index.read_page("page-1")["trusted"])

    def test_concurrent_readers_observe_whole_page_generations(self):
        self.add_complete(segments=[segment("A", "a"), segment("A", "b")])

        def write():
            for index in range(20):
                text = "A" if index % 2 else "B"
                self.index.replace_content("page-1", [segment(text, "a"), segment(text, "b")], complete=True,
                                           expected_edited_time=page()["last_edited_time"])

        def read():
            for _ in range(35):
                record = self.index.read_page("page-1")
                self.assertIn(record["content"], {"A\nA", "B\nB"})
                self.assertEqual(len(record["segments"]), 2)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(write), *(pool.submit(read) for _ in range(3))]
            for future in futures:
                future.result()

    def test_corrupt_file_and_corrupt_payload_raise_safe_error(self):
        from backend.notion.body_index import BodyIndex, BodyIndexError
        self.add_complete()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE pages SET raw_page = ?", ("not json",))
        with self.assertRaises(BodyIndexError):
            self.index.read_page("page-1")
        broken = self.path.with_name("broken.sqlite3")
        broken.write_bytes(b"not sqlite")
        with self.assertRaises(BodyIndexError):
            BodyIndex(broken, fingerprint="connection-a")

    def test_read_only_open_never_creates_files_and_rejects_foreign_connection(self):
        from backend.notion.body_index import open_body_index
        missing = self.path.with_name("missing.sqlite3")
        self.assertIsNone(open_body_index(missing, fingerprint="connection-a"))
        self.assertFalse(missing.exists())
        self.add_complete()
        self.assertIsNotNone(open_body_index(self.path, fingerprint="connection-a"))
        self.assertIsNone(open_body_index(self.path, fingerprint="connection-b"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_single_page_refresh_preserves_catalog_completeness(self):
        self.add_complete()
        self.index.upsert_page("db-1", page(revision="new"))
        status = self.index.status(["db-1"])
        self.assertTrue(status["catalog_complete"])
        self.assertFalse(status["content_complete"])
        self.assertTrue(status["catalog_updated_at"])
        self.assertTrue(status["updated_at"])

    def test_index_opened_with_config_rejects_later_connection_change(self):
        from backend.notion import body_index
        settings = {"token": "first", "version": "v1", "database_ids": ["db-1"]}
        with patch.object(body_index, "get_notion_settings", return_value=settings):
            index = body_index.BodyIndex(self.path)
            index.upsert_catalog("db-1", [page()], complete=True)
            settings["token"] = "second"
            with self.assertRaises(body_index.BodyIndexError):
                index.read_page("page-1")
            with self.assertRaises(body_index.BodyIndexError):
                index.replace_content("page-1", [segment()], complete=True,
                                      expected_edited_time=page()["last_edited_time"])

    def test_missing_revision_is_never_trusted(self):
        raw = page(revision="")
        self.add_complete(raw=raw)
        self.assertFalse(self.index.read_page("page-1")["trusted"])
        self.assertFalse(self.index.status(["db-1"])["complete"])
        self.assertEqual(len(self.index.pending_pages(["db-1"])), 1)

    def test_old_completed_body_expires_and_is_rescheduled_without_losing_content(self):
        from backend.notion import body_index
        with patch.object(body_index, "_now", return_value="2025-01-01T00:00:00+00:00"):
            self.add_complete()
        record = self.index.read_page("page-1")
        self.assertEqual(record["content"], "본문 증거")
        self.assertFalse(record["trusted"])
        self.assertEqual(len(self.index.pending_pages(["db-1"])), 1)
        status = self.index.status(["db-1"])
        self.assertTrue(status["snapshot_complete"])
        self.assertFalse(status["complete"])
        self.assertEqual(status["stale_page_count"], 1)
        self.assertEqual(status["indexed_page_count"], 0)

    def test_explicit_refresh_preserves_body_without_recording_failure(self):
        self.add_complete()
        self.assertEqual(self.index.mark_pending(["db-1"]), 1)
        record = self.index.read_page("page-1")
        self.assertEqual(record["content"], "본문 증거")
        self.assertFalse(record["trusted"])
        self.assertEqual(record["failure_count"], 0)
        self.assertEqual(record["last_error"], "")
        self.assertEqual(len(self.index.pending_pages(["db-1"])), 1)

    def test_pending_limit_does_not_apply_to_search_candidates(self):
        rows = [page(f"p{i}") for i in range(45)]
        self.index.upsert_catalog("db-1", rows, complete=True)
        self.assertEqual(len(self.index.pending_pages(["db-1"], max_pages=2)), 2)
        for raw in rows:
            self.index.replace_content(raw["id"], [segment("needle", owner=raw["id"])], complete=True,
                                       expected_edited_time=raw["last_edited_time"])
        self.assertEqual(len(self.index.search(["db-1"], ["needle"])), 45)
        self.assertEqual(json.loads(json.dumps(self.index.status(["db-1"]))) ["page_count"], 45)


if __name__ == "__main__":
    unittest.main()
