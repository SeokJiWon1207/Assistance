"""운영 삭제 범위와 보존 조건을 실제 임시 파일로 검증한다."""
import importlib.util
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/prune_deployments.py'
IDS = ['20261020T010000Z', '20261021T010000Z', '20261022T010000Z', '20261023T010000Z']


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.exists(), '배포 보관 도구가 아직 구현되지 않았습니다')
        spec = importlib.util.spec_from_file_location('deployment_retention', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.app = self.root / 'app'
        self.state = self.root / 'state'
        self.legacy = self.root / 'legacy-backups'
        for path in (self.app / 'releases', self.app / 'backups', self.state / 'deployment-backups', self.legacy):
            path.mkdir(parents=True)
        (self.app / '.deployment.lock').touch()
        for name in IDS:
            self.release(name)
            backup = self.state / 'deployment-backups' / name
            backup.mkdir()
            (backup / 'manifest.json').write_text('["users.sqlite3"]')
            (backup / 'users.sqlite3').write_text('backup')
        (self.app / 'current').symlink_to(self.app / 'releases' / IDS[-1])
        (self.state / 'users.sqlite3').write_text('active users')

    def release(self, name):
        path = self.app / 'releases' / name
        (path / 'assistant_web/.venv/bin').mkdir(parents=True)
        (path / 'assistant_web/.venv/bin/python').write_text('runtime')
        (path / 'data/mantis_chroma_db').mkdir(parents=True)
        (path / 'data/mantis_chroma_db/chroma.sqlite3').write_text('search')
        (path / '.deployment-ready.json').write_text('{}')
        return path

    def run_cleanup(self, **kwargs):
        return self.module.prune(
            self.app, self.state, self.legacy, apply=True, healthy=lambda: True,
            process_paths=[], now=datetime(2026, 10, 26, tzinfo=timezone.utc), **kwargs,
        )

    def test_keeps_current_previous_two_and_their_backups(self):
        self.run_cleanup()
        self.assertFalse((self.app / 'releases' / IDS[0]).exists())
        self.assertFalse((self.state / 'deployment-backups' / IDS[0]).exists())
        for name in IDS[1:]:
            self.assertTrue((self.app / 'releases' / name).exists())
            self.assertTrue((self.state / 'deployment-backups' / name / 'users.sqlite3').exists())
        self.assertEqual((self.state / 'users.sqlite3').read_text(), 'active users')
        self.assertEqual(self.run_cleanup()['removed'], [])

    def test_rollback_current_preserves_newer_releases(self):
        (self.app / 'current').unlink()
        (self.app / 'current').symlink_to(self.app / 'releases' / IDS[-2])
        self.run_cleanup()
        self.assertTrue(all((self.app / 'releases' / name).exists() for name in IDS))

    def test_retained_virtualenv_dependency_pins_old_release(self):
        import shutil
        venv = self.app / 'releases' / IDS[-1] / 'assistant_web/.venv'
        shutil.rmtree(venv)
        venv.symlink_to(self.app / 'releases' / IDS[0] / 'assistant_web/.venv')
        self.run_cleanup()
        self.assertTrue((self.app / 'releases' / IDS[0]).exists())
        self.assertTrue((venv / 'bin/python').exists())

    def test_process_reference_pins_release_and_backup(self):
        result = self.module.prune(self.app, self.state, self.legacy, apply=True, healthy=lambda: True,
                                   process_paths=[self.app / 'releases' / IDS[0] / 'worker.py'])
        self.assertEqual(result['removed'], [])

    def test_dry_run_and_unhealthy_service_do_not_delete(self):
        result = self.module.prune(self.app, self.state, self.legacy, process_paths=[])
        self.assertTrue(result['candidates'])
        self.assertTrue((self.app / 'releases' / IDS[0]).exists())
        result = self.module.prune(self.app, self.state, self.legacy, apply=True,
                                   healthy=lambda: False, process_paths=[])
        self.assertEqual(result['status'], 'skipped_unhealthy')
        self.assertTrue((self.app / 'releases' / IDS[0]).exists())

    def test_missing_or_outside_current_fails_closed(self):
        (self.app / 'current').unlink()
        with self.assertRaises(ValueError):
            self.run_cleanup()
        (self.app / 'current').symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.run_cleanup()
        self.assertTrue((self.app / 'releases' / IDS[0]).exists())

    def test_changed_current_during_health_check_aborts(self):
        def change_current():
            (self.app / 'current').unlink()
            (self.app / 'current').symlink_to(self.app / 'releases' / IDS[0])
            return True
        with self.assertRaises(RuntimeError):
            self.module.prune(self.app, self.state, self.legacy, apply=True,
                              healthy=change_current, process_paths=[])
        self.assertTrue((self.app / 'releases' / IDS[0]).exists())

    def test_snapshots_unknown_backups_and_symlink_roots_are_untouched(self):
        target = self.app / 'snapshots/20200101T000000Z'
        target.mkdir(parents=True)
        (target / 'index').write_text('shared index')
        link = self.legacy / ('users-20200101-000000.sqlite3')
        link.symlink_to(self.state / 'users.sqlite3')
        unknown = self.app / 'backups/manual-important.txt'
        unknown.write_text('keep')
        self.run_cleanup()
        self.assertEqual((target / 'index').read_text(), 'shared index')
        self.assertTrue(link.is_symlink())
        self.assertEqual(unknown.read_text(), 'keep')

    def test_loose_backups_keep_latest_three_groups_and_seven_day_grace(self):
        for stamp in ['20200101-000000', '20200102-000000', '20200103-000000', '20200104-000000',
                      '20261025-000000']:
            for name in ('users', 'workspace', 'assistant_audit'):
                (self.legacy / f'{name}-{stamp}.sqlite3.gz').write_text('archive')
        self.run_cleanup()
        self.assertFalse((self.legacy / 'users-20200101-000000.sqlite3.gz').exists())
        self.assertFalse((self.legacy / 'users-20200102-000000.sqlite3.gz').exists())
        self.assertTrue((self.legacy / 'users-20200103-000000.sqlite3.gz').exists())
        self.assertTrue((self.legacy / 'users-20261025-000000.sqlite3.gz').exists())

    def test_lock_held_by_deployment_skips_cleanup(self):
        import fcntl
        with (self.app / '.deployment.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_cleanup()
        self.assertEqual(result['status'], 'skipped_locked')
        self.assertTrue((self.app / 'releases' / IDS[0]).exists())

    def test_incomplete_release_does_not_replace_a_rollback_slot(self):
        incomplete = self.app / 'releases/20261022T120000Z'
        incomplete.mkdir()
        self.run_cleanup()
        self.assertTrue((self.app / 'releases' / IDS[1]).exists())
        self.assertTrue(incomplete.exists())

    def test_apply_writes_machine_readable_audit(self):
        self.run_cleanup()
        result = json.loads((self.state / 'retention-last-run.json').read_text())
        self.assertEqual(result['status'], 'complete')
        self.assertIn(str(self.app / 'releases' / IDS[0]), result['removed'])

    def test_fully_prepared_but_unpublished_release_is_not_a_rollback_slot(self):
        staged = self.release('20261022T120000Z')
        (staged / '.deployment-ready.json').unlink()
        self.run_cleanup()
        self.assertTrue(staged.exists())
        self.assertTrue((self.app / 'releases' / IDS[1]).exists())

    def test_symlinked_backup_root_aborts_without_deletion(self):
        import shutil
        shutil.rmtree(self.legacy)
        self.legacy.symlink_to(self.state / 'deployment-backups')
        with self.assertRaises(ValueError):
            self.run_cleanup()
        self.assertTrue((self.app / 'releases' / IDS[0]).exists())

    def test_cannot_configure_fewer_than_current_and_two_previous(self):
        with self.assertRaises(ValueError):
            self.run_cleanup(keep=2)
        self.assertTrue((self.app / 'releases' / IDS[0]).exists())

    def test_missing_deployment_lock_does_not_create_root_owned_lock(self):
        (self.app / '.deployment.lock').unlink()
        with self.assertRaises(ValueError):
            self.run_cleanup()
        self.assertFalse((self.app / '.deployment.lock').exists())
        self.assertTrue((self.app / 'releases' / IDS[0]).exists())

    def test_insufficient_published_history_keeps_existing_backups(self):
        import shutil
        for name in IDS[:-1]:
            shutil.rmtree(self.app / 'releases' / name)
        self.run_cleanup()
        self.assertTrue((self.state / 'deployment-backups' / IDS[0]).exists())


class RedeployCommandRetentionTests(unittest.TestCase):
    def test_desktop_redeploy_command_passes_three_retained_versions(self):
        import os
        import shutil
        import subprocess
        zsh = shutil.which('zsh')
        if not zsh:
            self.skipTest('데스크톱 재배포 명령은 zsh에서 실행됩니다.')
        source = next(SCRIPT.parent.joinpath('commands').glob('AWS_*재배포.command'), None)
        if source is None:
            import unicodedata
            source = next(p for p in SCRIPT.parent.joinpath('commands').glob('*.command')
                          if unicodedata.normalize('NFC', p.name) == 'AWS_시연서버_재배포.command')
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command = root / 'assistant_web/scripts/commands/redeploy.command'
            command.parent.mkdir(parents=True)
            shutil.copyfile(source, command)
            deploy = root / 'assistant_web/deploy/aws/deploy_to_ec2.sh'
            deploy.parent.mkdir(parents=True)
            capture = root / 'captured'
            deploy.write_text('#!/bin/sh\nprintf "%s" "$RELEASE_KEEP" > "$CAPTURE"\n')
            deploy.chmod(0o700)
            key = root / 'key.pem'
            key.write_text('test-only-placeholder')
            result = subprocess.run([zsh, '-c', 'read() { return 0; }; source "$0"', str(command)],
                                    env={**os.environ, 'ASSISTANCE_SSH_KEY': str(key), 'CAPTURE': str(capture)},
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(capture.read_text(), '3')
