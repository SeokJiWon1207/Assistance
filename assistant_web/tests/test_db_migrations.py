import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from backend import audit
from backend.db_migrations import run_sqlite_migrations


class SqliteMigrationTests(unittest.TestCase):
    def test_existing_data_survives_sequential_migrations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "test.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
                connection.execute("INSERT INTO records (value) VALUES ('kept')")

            def baseline(connection):
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY, value TEXT)"
                )

            def add_owner(connection):
                connection.execute("ALTER TABLE records ADD COLUMN owner TEXT NOT NULL DEFAULT ''")

            run_sqlite_migrations(
                database_path,
                [baseline, add_owner],
                database_name="test",
            )

            with closing(sqlite3.connect(database_path)) as connection, connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)
                self.assertEqual(
                    connection.execute("SELECT value, owner FROM records").fetchone(),
                    ("kept", ""),
                )

    def test_newer_database_version_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "future.sqlite3"
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.execute("PRAGMA user_version = 2")

            with self.assertRaisesRegex(RuntimeError, "newer than supported"):
                run_sqlite_migrations(
                    database_path,
                    [lambda connection: None],
                    database_name="test",
                )

    def test_audit_owner_migration_preserves_legacy_rows_and_scopes_sources(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            database_path = Path(temporary_directory) / "audit.sqlite3"
            run_sqlite_migrations(
                database_path,
                [audit._create_audit_schema],
                database_name="audit",
            )
            payload = '{"sources": [{"type": "mantis", "issue_id": 30072}]}'
            with closing(sqlite3.connect(database_path)) as connection, connection:
                connection.execute(
                    "INSERT INTO validation_logs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    ("a" * 32, "2026-08-10", "질문", "model", "model", 100, "pass", payload),
                )

            with patch.object(audit, "AUDIT_DB_PATH", database_path):
                audit.init_audit_db()
                self.assertEqual(audit.load_audit_sources("a" * 32), [{"type": "mantis", "issue_id": 30072}])
                self.assertEqual(audit.load_audit_sources("a" * 32, user_uid="other-user"), [])

            with closing(sqlite3.connect(database_path)) as connection, connection:
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(
                    connection.execute(
                        "SELECT user_uid FROM validation_logs WHERE id = ?", ("a" * 32,)
                    ).fetchone()[0],
                    "",
                )


if __name__ == "__main__":
    unittest.main()
