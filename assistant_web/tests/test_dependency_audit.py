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


if __name__ == "__main__":
    unittest.main()
