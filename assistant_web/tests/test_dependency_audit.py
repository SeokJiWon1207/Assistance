import importlib.util
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_dependency_audit.py"
SPEC = importlib.util.spec_from_file_location("check_dependency_audit", SCRIPT_PATH)
assert SPEC and SPEC.loader
check_dependency_audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_dependency_audit)


class DependencyAuditTests(unittest.TestCase):
    def write_exceptions(self, exceptions):
        temporary = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        with temporary:
            json.dump({"exceptions": exceptions}, temporary)
        self.addCleanup(Path(temporary.name).unlink, missing_ok=True)
        return Path(temporary.name)

    def test_active_exception_builds_ignore_argument(self):
        path = self.write_exceptions([
            {"id": "PYSEC-TEST", "expiresAt": "2026-09-30", "reason": "not exposed"}
        ])

        exceptions = check_dependency_audit.load_active_exceptions(path, today=date(2026, 8, 11))
        command = check_dependency_audit.build_audit_command(Path("requirements.lock"), exceptions)

        self.assertIn("--ignore-vuln", command)
        self.assertIn("--disable-pip", command)
        self.assertIn("--no-deps", command)
        self.assertIn("PYSEC-TEST", command)

    def test_expired_exception_fails_closed(self):
        path = self.write_exceptions([
            {"id": "PYSEC-TEST", "expiresAt": "2026-08-10", "reason": "not exposed"}
        ])

        with self.assertRaisesRegex(RuntimeError, "만료"):
            check_dependency_audit.load_active_exceptions(path, today=date(2026, 8, 11))

    def test_exception_cannot_hide_a_different_package_version(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = Path(directory) / "requirements.lock"
            requirements.write_text("chromadb==1.5.10\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "검토.*버전"):
                check_dependency_audit.build_audit_command(requirements, [{
                    "id": "PYSEC-TEST", "package": "chromadb", "version": "1.5.9",
                }])

    def test_exception_cannot_hide_an_absent_or_unpinned_package(self):
        for requirement in ("urllib3==2.8.0", "chromadb>=1.5.9"):
            with self.subTest(requirement=requirement), tempfile.TemporaryDirectory() as directory:
                requirements = Path(directory) / "requirements.lock"
                requirements.write_text(requirement + "\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "검토.*버전"):
                    check_dependency_audit.build_audit_command(requirements, [{
                        "id": "PYSEC-TEST", "package": "chromadb", "version": "1.5.9",
                    }])

    def test_matching_package_version_keeps_the_reviewed_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            requirements = Path(directory) / "requirements.lock"
            requirements.write_text("ChromaDB==1.5.9  # 고정 버전\n", encoding="utf-8")
            command = check_dependency_audit.build_audit_command(requirements, [{
                "id": "PYSEC-TEST", "package": "chromadb", "version": "1.5.9",
            }])
            self.assertIn("PYSEC-TEST", command)

    def test_partial_package_scope_is_rejected(self):
        for scope in ({"package": "chromadb"}, {"version": "1.5.9"}):
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(ValueError, "package.*version"):
                    check_dependency_audit.build_audit_command(Path("requirements.lock"), [{
                        "id": "PYSEC-TEST", **scope,
                    }])


if __name__ == "__main__":
    unittest.main()
