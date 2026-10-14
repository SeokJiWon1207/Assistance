import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend.intent import entities


class EntityCatalogLatencyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "chroma.sqlite3"
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        with sqlite3.connect(self.database) as connection:
            connection.execute("CREATE TABLE embedding_metadata(key TEXT, string_value TEXT)")
            connection.execute("INSERT INTO embedding_metadata VALUES ('chroma:document', ?)", (self.document(),))
        self.enterContext(patch.object(entities, "CHROMA_DB_PATH", self.database))
        self.enterContext(patch.object(entities, "RUNTIME_DIR", self.runtime, create=True))
        self.clear_caches()
        self.addCleanup(self.clear_caches)

    @staticmethod
    def document(handler="홍길동"):
        return ("- 프로젝트(고객사): 112KBStar(KB증권)\n"
                f"- 담당자: {handler}\n- 요청자: 김영희\n\n[본문]\n" + "본문 내용 " * 1000)

    @staticmethod
    def clear_caches():
        for name in ("_load_known_people_cached", "_load_project_alias_groups_cached",
                     "_get_customer_alias_groups_cached", "_load_chroma_header_catalog_cached"):
            function = getattr(entities, name, None)
            if function is not None and hasattr(function, "cache_clear"):
                function.cache_clear()

    def change_handler(self, handler="박철수"):
        with sqlite3.connect(self.database) as connection:
            connection.execute("UPDATE embedding_metadata SET string_value=?", (self.document(handler),))

    def assert_people(self, handler="홍길동"):
        self.assertEqual(entities.load_known_people(), ((handler,), ("김영희",)))

    def catalog_file(self):
        files = list(self.runtime.glob("*.json"))
        self.assertEqual(len(files), 1, "본문과 별도로 재시작에 재사용할 작은 카탈로그를 저장한다")
        return files[0]

    def test_explicit_warm_path_persists_without_changing_configured_source(self):
        other = self.root / "other.sqlite3"
        with sqlite3.connect(other) as connection:
            connection.execute("CREATE TABLE embedding_metadata(key TEXT, string_value TEXT)")
            connection.execute("INSERT INTO embedding_metadata VALUES ('chroma:document', ?)", (self.document("박철수"),))
        self.assertTrue(entities.warm_entity_catalog(other))
        self.assertEqual(entities.CHROMA_DB_PATH, self.database)
        self.clear_caches()
        with patch.object(entities, "CHROMA_DB_PATH", other), patch.object(
                entities.sqlite3, "connect", side_effect=AssertionError("준비된 원본을 다시 열었다")):
            self.assert_people("박철수")

    def test_warm_retries_disk_write_using_memory_after_permissions_recover(self):
        with patch.object(os, "replace", side_effect=PermissionError("읽기 전용 캐시")):
            self.assertFalse(entities.warm_entity_catalog())
            self.assert_people()
        with patch.object(entities.sqlite3, "connect", side_effect=AssertionError("본문을 다시 스캔했다")):
            self.assertTrue(entities.warm_entity_catalog())
        self.assertLess(self.catalog_file().stat().st_size, 4096)

    def test_warm_reports_missing_source_without_persisting_empty_catalog(self):
        self.assertFalse(entities.warm_entity_catalog(self.root / "missing.sqlite3"))
        self.assertEqual(list(self.runtime.iterdir()), [])

    def test_restart_reads_small_catalog_without_opening_source_documents(self):
        self.assert_people()
        projects = entities.load_project_alias_groups()
        self.assertIn("KB증권", {alias for group in projects for alias in group})
        self.assertLess(self.catalog_file().stat().st_size, 4096)
        self.clear_caches()
        with patch.object(entities.sqlite3, "connect", side_effect=AssertionError("본문을 다시 열었다")):
            self.assert_people()
            self.assertEqual(entities.load_project_alias_groups(), projects)

    def test_same_size_replacement_cannot_reuse_an_old_generation(self):
        self.assert_people()
        previous = self.database.stat()
        replacement = self.root / "replacement.sqlite3"
        with sqlite3.connect(replacement) as connection:
            connection.execute("CREATE TABLE embedding_metadata(key TEXT, string_value TEXT)")
            connection.execute("INSERT INTO embedding_metadata VALUES ('chroma:document', ?)", (self.document("박철수"),))
        self.assertEqual(replacement.stat().st_size, previous.st_size)
        os.utime(replacement, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        os.replace(replacement, self.database)
        self.assert_people("박철수")

    def test_wal_commit_invalidates_catalog_without_main_database_change(self):
        with sqlite3.connect(self.database) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            self.assert_people()
            previous = self.database.stat()
            writer.execute("UPDATE embedding_metadata SET string_value=?", (self.document("박철수"),))
            writer.commit()
            self.assertEqual(self.database.stat().st_mtime_ns, previous.st_mtime_ns)
            self.assertEqual(self.database.stat().st_size, previous.st_size)
            self.assert_people("박철수")

    def test_corrupt_catalog_falls_back_to_source_and_repairs_it(self):
        self.assert_people()
        catalog = self.catalog_file()
        for bad in (b"{broken", b'{"version":1,"source":[],"fields":{}}', b"[" * 10000 + b"]" * 10000,
                    b" " * (2 * 1024 * 1024)):
            with self.subTest(bytes=len(bad)):
                catalog.write_bytes(bad)
                self.clear_caches()
                self.assert_people()
                self.assertLess(catalog.stat().st_size, 4096)
                self.assertIsInstance(json.loads(catalog.read_text()), dict)

    def test_catalog_write_failure_keeps_complete_source_results(self):
        with patch.object(os, "replace", side_effect=PermissionError("읽기 전용 캐시")):
            self.assert_people()
            self.assertIn("KB증권", {alias for group in entities.load_project_alias_groups() for alias in group})
        self.assertEqual(list(self.runtime.iterdir()), [], "실패한 임시 파일을 남기지 않는다")

    def test_temporary_source_failure_is_not_cached_as_an_empty_roster(self):
        original = sqlite3.connect
        calls = 0

        def connect(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise sqlite3.OperationalError("일시적 열기 실패")
            return original(*args, **kwargs)

        with patch.object(entities.sqlite3, "connect", side_effect=connect):
            self.assertEqual(entities.load_known_people(), ((), ()))
            self.assert_people()

    def test_concurrent_cold_calls_scan_each_header_only_once(self):
        calls = Counter()
        lock = threading.Lock()
        barrier = threading.Barrier(8)
        original = entities._distinct_chroma_header_values

        def scan(connection, field):
            with lock:
                calls[field] += 1
            time.sleep(0.01)
            return original(connection, field)

        def load(_):
            barrier.wait()
            return entities.load_known_people(), entities.load_project_alias_groups()

        with patch.object(entities, "_distinct_chroma_header_values", side_effect=scan), ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(load, range(8)))
        self.assertTrue(all(people == (("홍길동",), ("김영희",)) for people, _ in results))
        self.assertEqual(calls, {"프로젝트(고객사)": 1, "담당자": 1, "요청자": 1})

    def test_generation_change_during_scan_retries_before_caching(self):
        original = entities._distinct_chroma_header_values
        changed = False
        with sqlite3.connect(self.database) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")

            def scan(connection, field):
                nonlocal changed
                values = original(connection, field)
                if not changed:
                    changed = True
                    writer.execute("UPDATE embedding_metadata SET string_value=?", (self.document("박철수"),))
                    writer.commit()
                return values

            with patch.object(entities, "_distinct_chroma_header_values", side_effect=scan):
                self.assert_people("박철수")
            self.clear_caches()
            with patch.object(entities.sqlite3, "connect", side_effect=AssertionError("세대 변경 후 카탈로그 미저장")):
                self.assert_people("박철수")
