import configparser
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class ProjectConfigurationTests(unittest.TestCase):
    def test_local_python_version_matches_ci_runtime(self):
        self.assertTrue(
            (PROJECT_ROOT / ".python-version").read_text(encoding="utf-8").strip().startswith("3.12")
        )
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn('python-version: "3.12"', workflow)

    def test_coverage_gate_measures_production_code_only(self):
        config = configparser.ConfigParser()
        config.read(PROJECT_ROOT / ".coveragerc", encoding="utf-8")

        self.assertIn("*/tests/*", config.get("run", "omit"))
        self.assertIn("*/tests/*", config.get("report", "omit"))
        self.assertEqual(config.getint("report", "fail_under"), 67)

        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("run: coverage report", workflow)
        self.assertNotIn("--fail-under=70", workflow)


if __name__ == "__main__":
    unittest.main()
