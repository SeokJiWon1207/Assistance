import json
import os
import shutil
import sys
import subprocess
import tempfile
import unittest
import unicodedata
from pathlib import Path


PACKAGE_SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "aws" / "package_release.sh"


class ReleasePackagingTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory(prefix="release-package-test-")
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.release = self.root / "release"
        self.files = {
            ".coveragerc": "[run]\nomit = */tests/*\n",
            ".python-version": "3.12\n",
            "pyproject.toml": '[tool.ruff]\ntarget-version = "py312"\n',
            ".github/workflows/ci.yml": 'python-version: "3.12"\n',
            "scripts/make_vector_db.py": "# snapshot builder\n",
            "scripts/benchmark_mantis_index.py": "# comparison verifier\n",
            "assistant_web/scripts/release_environment.py": (PACKAGE_SCRIPT.parents[2]/"scripts/release_environment.py").read_text(),
            "assistant_web/deploy/aws/select_python.sh": PACKAGE_SCRIPT.with_name("select_python.sh").read_text(),
            "assistant_web/backend/app.py": "# production backend\n",
            "assistant_web/backend/static/vendor/bklit/charts.js": "// compiled chart\n",
            "assistant_web/frontend/vendor/bklit/LICENSE": "MIT\n",
            "assistant_web/tests/run_test_suite.py": "# deterministic test runner\n",
            "assistant_web/config/evaluation_cases.json": json.dumps([{"id": "current-release-case"}]),
            "assistant_web/scripts/commands/AWS_시연서버_재배포.command": "# redeploy\n",
            "assistant_web/scripts/commands/AWS_점검모드.command": "# maintenance\n",
            "assistant_web/scripts/commands/AI비서_서버_재시작.command": "# restart\n",
        }
        for relative, content in self.files.items():
            self.write(relative, content)

    def write(self, relative, content):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def package(self):
        return subprocess.run(
            ["bash", str(PACKAGE_SCRIPT), str(self.source), str(self.release)],
            capture_output=True,
            text=True,
            check=False,
        )

    def test_release_contains_matching_application_and_verification_files_without_private_state(self):
        private_paths = [
            "assistant_web/.env",
            "assistant_web/key.pem",
            "assistant_web/firebase-service-account.json",
            "assistant_web/runtime/users.sqlite3",
            "assistant_web/.venv/bin/python",
            "assistant_web/frontend/node_modules/react/index.js",
            "assistant_web/__pycache__/app.pyc",
            "assistant_web/artifacts/debug.json",
            "assistant_web/.mypy_cache/3.12/cache.db",
            "assistant_web/.ruff_cache/cache",
            "assistant_web/.pytest_cache/cache",
        ]
        for relative in private_paths:
            self.write(relative, "private state")
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        for relative, content in self.files.items():
            with self.subTest(relative=relative):
                self.assertEqual((self.release / relative).read_text(encoding="utf-8"), content)
        for relative in private_paths:
            with self.subTest(private=relative):
                self.assertFalse((self.release / relative).exists())

    def test_decomposed_command_name_is_packaged_once_with_linux_compatible_nfc_name(self):
        relative = "assistant_web/scripts/commands/AWS_시연서버_재배포.command"
        original = self.source / relative
        decomposed = original.with_name(unicodedata.normalize("NFD", original.name))
        original.rename(decomposed)
        result = self.package()
        self.assertEqual(result.returncode, 0, result.stderr)
        command_names = [path.name for path in (self.release / "assistant_web/scripts/commands").glob("*.command")]
        self.assertEqual(set(command_names), {
            "AWS_시연서버_재배포.command", "AWS_점검모드.command", "AI비서_서버_재시작.command",
        })
        self.assertEqual((self.release / relative).read_text(encoding="utf-8"), "# redeploy\n")

    def test_deploy_uploads_complete_package_through_transport_boundary(self):
        self.deploy_through_transport(reuse=False)

    def test_deploy_reuses_existing_snapshot_without_uploading_local_data(self):
        self.deploy_through_transport(reuse=True)

    def test_failed_deploy_keeps_reused_production_snapshot(self):
        self.deploy_through_transport(reuse=True, fail_install=True)

    def test_reuse_rejects_snapshot_outside_managed_directory(self):
        self.deploy_through_transport(reuse=True, outside=True)

    def deploy_through_transport(self, *, reuse, fail_install=False, outside=False):
        deploy_directory = self.source / "assistant_web/deploy/aws"
        deploy_directory.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PACKAGE_SCRIPT, deploy_directory / PACKAGE_SCRIPT.name)
        shutil.copyfile(PACKAGE_SCRIPT.with_name("deploy_to_ec2.sh"), deploy_directory / "deploy_to_ec2.sh")
        self.write("assistant_web/.env", "GEMINI_API_KEY=local-invalid\nOPENAI_API_KEY=local-openai\n")
        self.write("data/mantis_chroma_db/freshness.json", "{}")
        transport = self.root / "transport"
        transport.mkdir()
        (transport / "ssh").write_text(
            f"#!{sys.executable}\n"
            "import os,subprocess,sys\n"
            "command=sys.argv[-1]\n"
            "if command.endswith('bash -s'):\n"
            "    body=sys.stdin.read()\n"
            "    if any(stage in body for stage in ('# release-env-stage', '# snapshot-reuse-stage', '# snapshot-link-stage', '# staged-release-cleanup')):\n"
            "        raise SystemExit(subprocess.run(['/bin/bash','-c',command],input=body,text=True).returncode)\n"
            "if 'setup_server.sh' in command and os.environ.get('TEST_FAIL_INSTALL') == '1':\n"
            "    raise SystemExit(9)\n"
            "raise SystemExit(0)\n", encoding="utf-8")
        real_rsync = shutil.which("rsync")
        self.assertIsNotNone(real_rsync)
        (transport / "rsync").write_text(
            f"#!{sys.executable}\n"
            "import subprocess, sys\n"
            "from pathlib import Path\n"
            "arguments = []\n"
            "iterator = iter(sys.argv[1:])\n"
            "for argument in iterator:\n"
            "    if argument == '-e':\n"
            "        next(iterator)\n"
            "    else:\n"
            "        arguments.append(argument.removeprefix('deployer@unit-test:'))\n"
            "destination = Path(arguments[-1])\n"
            "(destination if arguments[-1].endswith('/') else destination.parent).mkdir(parents=True, exist_ok=True)\n"
            f"raise SystemExit(subprocess.call([{real_rsync!r}, *arguments]))\n",
            encoding="utf-8",
        )
        for executable in transport.iterdir():
            executable.chmod(0o755)
        remote = self.root / "remote"
        production_env = remote / "current/assistant_web/.env"
        production_env.parent.mkdir(parents=True)
        production_env.write_text("GEMINI_API_KEY=production-working\nMANTIS_API_TOKEN=production\n")
        snapshot = remote / ("unmanaged/20261013T021947Z" if outside else "snapshots/20261013T021947Z")
        (remote / "snapshots").mkdir(exist_ok=True)
        database = snapshot / "mantis_chroma_db/chroma.sqlite3"
        database.parent.mkdir(parents=True)
        database.write_text("production-search-data")
        (production_env.parents[1] / "data").symlink_to(snapshot, target_is_directory=True)
        result = subprocess.run(
            ["bash", str(deploy_directory / "deploy_to_ec2.sh")],
            env={**os.environ, "PATH": f"{transport}{os.pathsep}{os.environ['PATH']}",
                 "EC2_HOST": "unit-test", "EC2_USER": "deployer", "SSH_KEY": str(self.root / "key.pem"),
                 "APP_ROOT": str(remote), "RELEASE_ID": "20261014T120000Z", "INCLUDE_ENV": "1",
                 "FIREBASE_CREDENTIALS": "", "LOCAL_PYTHON": sys.executable, "DEPLOY_PYTHON": sys.executable,
                 "REUSE_REMOTE_SNAPSHOT": "1" if reuse else "0",
                 "TEST_FAIL_INSTALL": "1" if fail_install else "0"},
            capture_output=True,
            text=True,
            check=False,
        )
        if fail_install or outside:
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(database.read_text(), "production-search-data")
            self.assertFalse((remote / "releases/20261014T120000Z").exists())
            return
        self.assertEqual(result.returncode, 0, result.stderr)
        uploaded = remote / "releases/20261014T120000Z"
        for relative, content in self.files.items():
            with self.subTest(relative=relative):
                self.assertEqual((uploaded / relative).read_text(encoding="utf-8"), content)
        env = (uploaded / "assistant_web/.env").read_text(encoding="utf-8")
        self.assertIn("GEMINI_API_KEY=production-working", env)
        self.assertIn("MANTIS_API_TOKEN=production", env)
        self.assertIn("OPENAI_API_KEY=local-openai", env)
        self.assertNotIn("local-invalid", env)
        self.assertFalse((uploaded / "assistant_web/.env-overlay").exists())
        self.assertEqual(production_env.read_text(), "GEMINI_API_KEY=production-working\nMANTIS_API_TOKEN=production\n")
        if reuse:
            self.assertEqual((uploaded / "data").resolve(), snapshot.resolve())
            self.assertFalse((remote / "snapshots/20261014T120000Z/mantis_chroma_db/freshness.json").exists())
        self.assertEqual(database.read_text(), "production-search-data")

    def test_missing_verification_config_rejects_release_before_packaging(self):
        (self.source / ".coveragerc").unlink()
        result = self.package()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".coveragerc", result.stderr)
        self.assertFalse((self.release / "assistant_web").exists())


if __name__ == "__main__":
    unittest.main()
