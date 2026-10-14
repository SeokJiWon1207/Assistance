import configparser
import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
CHECK_SCRIPT = PROJECT_ROOT / "assistant_web" / "scripts" / "check.sh"


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
        self.assertEqual(config.getint("report", "fail_under"), 72)

        check_script = CHECK_SCRIPT.read_text(encoding="utf-8")
        self.assertIn("-m coverage report", check_script)
        self.assertNotIn("--fail-under", check_script)

        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("--fail-under", workflow)

    def test_ci_runs_every_shared_check_step(self):
        check_script = CHECK_SCRIPT.read_text(encoding="utf-8")
        match = re.search(r"^ALL_STEPS=\(([^)]*)\)$", check_script, re.MULTILINE)
        self.assertIsNotNone(match)
        steps = match.group(1).split() if match else []
        self.assertIn("tests", steps)

        workflow = (PROJECT_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        for step in steps:
            with self.subTest(step=step):
                self.assertIn(f"run: bash assistant_web/scripts/check.sh {step}\n", workflow)


if __name__ == "__main__":
    unittest.main()
