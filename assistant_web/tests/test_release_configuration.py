"""운영 환경 파일 보존과 배포 Python 선택의 실패 경계를 검증한다."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]


class ReleaseEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.base, self.local, self.overlay, self.output = [self.root / n for n in ('base', 'local', 'overlay', 'output')]

    def helper(self):
        path = APP / 'scripts/release_environment.py'
        self.assertTrue(path.exists(), '운영 환경 보존 도우미가 필요합니다')
        spec = importlib.util.spec_from_file_location('release_environment', path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def effective(self, path):
        code = "from backend.environment import load_dotenv; import os,json,sys; load_dotenv(__import__('pathlib').Path(sys.argv[1])); print(json.dumps(dict(os.environ)))"
        process = subprocess.run([sys.executable, '-B', '-c', code, str(path)], cwd=APP,
                                 env={'PATH': os.environ['PATH']}, capture_output=True, text=True, check=True)
        return json.loads(process.stdout)

    def test_preserves_production_secrets_and_adds_only_missing_openai_key(self):
        helper = self.helper()
        self.base.write_text('GEMINI_API_KEY=production-gemini\nMANTIS_API_TOKEN=production-mantis\nAUTH_COOKIE_SECURE=true\n')
        self.local.write_text('GEMINI_API_KEY=local-invalid\nMANTIS_API_TOKEN=local-mantis\nAUTH_COOKIE_SECURE=false\nOPENAI_API_KEY="new-openai"\n')
        helper.write_overlay(self.local, self.overlay)
        self.assertNotIn('local-invalid', self.overlay.read_text())
        self.assertNotIn('MANTIS_API_TOKEN', self.overlay.read_text())
        helper.build_environment(self.base, self.overlay, self.output)
        actual = self.effective(self.output)
        self.assertEqual(actual['GEMINI_API_KEY'], 'production-gemini')
        self.assertEqual(actual['MANTIS_API_TOKEN'], 'production-mantis')
        self.assertEqual(actual['AUTH_COOKIE_SECURE'], 'true')
        self.assertEqual(actual['OPENAI_API_KEY'], 'new-openai')
        for path in (self.overlay, self.output):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_existing_openai_key_and_raw_json_are_preserved(self):
        helper = self.helper()
        original = 'OPENAI_API_KEY=production-openai\nFIREBASE_SERVICE_ACCOUNT_JSON=\'{"private_key":"line\\nnext"}\'\n'
        self.base.write_text(original)
        self.local.write_text('OPENAI_API_KEY=local-openai\n')
        helper.write_overlay(self.local, self.overlay)
        helper.build_environment(self.base, self.overlay, self.output)
        self.assertEqual(self.output.read_text(), original)
        self.assertEqual(self.effective(self.output)['OPENAI_API_KEY'], 'production-openai')

    def test_first_definition_and_blank_key_match_application_dotenv_semantics(self):
        helper = self.helper()
        self.base.write_text('OPENAI_API_KEY=\nOPENAI_API_KEY=ignored\nGEMINI_API_KEY=first\nGEMINI_API_KEY=ignored\n')
        self.local.write_text("OPENAI_API_KEY='new-openai'\nOPENAI_API_KEY=ignored\n")
        helper.write_overlay(self.local, self.overlay)
        helper.build_environment(self.base, self.overlay, self.output)
        values = self.effective(self.output)
        self.assertEqual(values['OPENAI_API_KEY'], 'new-openai')
        self.assertEqual(values['GEMINI_API_KEY'], 'first')

    def test_empty_local_key_does_not_delete_production_key(self):
        helper = self.helper()
        self.base.write_text('OPENAI_API_KEY=production\nGEMINI_API_KEY=production\n')
        self.local.write_text('OPENAI_API_KEY=\n')
        helper.write_overlay(self.local, self.overlay)
        helper.build_environment(self.base, self.overlay, self.output)
        self.assertEqual(self.output.read_text(), self.base.read_text())

    def test_missing_production_base_requires_explicit_bootstrap(self):
        helper = self.helper()
        self.local.write_text('GEMINI_API_KEY=initial\nOPENAI_API_KEY=initial-openai\n')
        helper.write_overlay(self.local, self.overlay)
        with self.assertRaisesRegex(ValueError, 'bootstrap'):
            helper.build_environment(self.base, self.overlay, self.output)
        self.assertFalse(self.output.exists())
        helper.build_environment(self.base, self.overlay, self.output, bootstrap=self.local)
        self.assertEqual(self.effective(self.output)['GEMINI_API_KEY'], 'initial')

    def test_bootstrap_cannot_replace_an_existing_base(self):
        helper = self.helper()
        self.base.write_text('GEMINI_API_KEY=production\n')
        self.local.write_text('GEMINI_API_KEY=local\n')
        helper.write_overlay(self.local, self.overlay)
        with self.assertRaisesRegex(ValueError, 'bootstrap'):
            helper.build_environment(self.base, self.overlay, self.output, bootstrap=self.local)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.base.read_text(), 'GEMINI_API_KEY=production\n')

    def test_input_and_existing_output_are_never_overwritten(self):
        helper = self.helper()
        self.base.write_text('GEMINI_API_KEY=production\n')
        self.local.write_text('OPENAI_API_KEY=new\n')
        helper.write_overlay(self.local, self.overlay)
        for destination in (self.base, self.overlay):
            with self.assertRaises((ValueError, FileExistsError)):
                helper.build_environment(self.base, self.overlay, destination)
        self.output.write_text('existing')
        with self.assertRaises(FileExistsError):
            helper.build_environment(self.base, self.overlay, self.output)
        self.assertEqual(self.output.read_text(), 'existing')

    def test_cli_reports_no_secret_values(self):
        self.helper()
        self.local.write_text('OPENAI_API_KEY=sensitive-test-value\nGEMINI_API_KEY=not-uploaded\n')
        process = subprocess.run([sys.executable, str(APP / 'scripts/release_environment.py'), 'overlay',
                                  '--source', str(self.local), '--output', str(self.overlay)], capture_output=True, text=True)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertNotIn('sensitive-test-value', process.stdout + process.stderr)
        self.assertNotIn('not-uploaded', self.overlay.read_text())


class DeploymentPythonTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.version = self.root / '.python-version'
        self.version.write_text('3.12.14\n')

    def select(self, env):
        script = APP / 'deploy/aws/select_python.sh'
        self.assertTrue(script.exists(), 'Python3.12 선택기가 필요합니다')
        return subprocess.run(['/bin/bash', str(script), str(self.version)], env=env, capture_output=True, text=True)

    def test_selects_and_validates_explicit_project_python(self):
        result = self.select({**os.environ, 'DEPLOY_PYTHON': sys.executable})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), sys.executable)

    def test_wrong_runtime_is_rejected(self):
        self.version.write_text('3.11.99\n')
        result = self.select({**os.environ, 'DEPLOY_PYTHON': sys.executable})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('3.11', result.stderr)

    def test_does_not_fall_back_to_system_python3(self):
        binary = self.root / 'bin'
        binary.mkdir()
        fake = binary / 'python3'
        fake.write_text('#!/bin/sh\nexit 0\n')
        fake.chmod(0o755)
        result = self.select({'PATH': str(binary)})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('python3.12', result.stderr)

    def test_setup_checks_runtime_before_any_system_or_service_change(self):
        script = (APP / 'deploy/aws/setup_server.sh').read_text()
        self.assertIn('select_python.sh', script)
        self.assertLess(script.index('select_python.sh'), script.index('sudo apt-get update'))
        self.assertIn('"$DEPLOY_PYTHON" -m venv .venv', script)
        self.assertNotIn('\npython3 -m venv .venv', script)

    def test_setup_rejects_invalid_python_before_running_sudo_or_creating_venv(self):
        import shutil
        release = self.root / 'app/releases/20261102T010203Z'
        directory = release / 'assistant_web/deploy/aws'
        directory.mkdir(parents=True)
        selector = APP / 'deploy/aws/select_python.sh'
        self.assertTrue(selector.exists())
        shutil.copy2(selector, directory / selector.name)
        (release / '.python-version').write_text('3.12.14\n')
        (release / 'data').symlink_to(self.root)
        touched = self.root / 'sudo-called'
        stubs = self.root / 'shell-stubs'
        stubs.write_text('id() { return 0; }\nsudo() { touch "$TEST_SUDO_CALLED"; exit 99; }\n')
        result = subprocess.run(['/bin/bash', str(APP / 'deploy/aws/setup_server.sh')], env={
            **os.environ, 'APP_ROOT': str(self.root / 'app'), 'APP_USER': 'fixture',
            'RELEASE_DIR': str(release), 'RELEASE_ID': '20261102T010203Z', 'DEPLOY_PYTHON': '/bin/false',
            'BASH_ENV': str(stubs), 'TEST_SUDO_CALLED': str(touched),
        }, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(touched.exists())
        self.assertFalse((release / 'assistant_web/.venv').exists())
