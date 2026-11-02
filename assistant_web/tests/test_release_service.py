import importlib.util
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/release_service.py'


class ServiceReleaseTests(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('release_service', SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def test_updates_old_overrides_and_preserves_environment_files_and_permissions(self):
        for name in ('template.conf', 'zz-release.conf'):
            target = self.root / name
            target.write_text('[Service]\nEnvironment=APP_RELEASE_ID=20261021T230902Z\nEnvironmentFile=/private/gemini.env\nEnvironment=AUTH_COOKIE_SECURE=true\n')
            target.chmod(0o640)
        untouched = self.root / 'other.conf'
        untouched.write_text('[Service]\nEnvironment=OTHER=unchanged\n')
        self.module.sync_release_id(self.root, '20261102T072200Z')
        for name in ('template.conf', 'zz-release.conf'):
            target = self.root / name
            self.assertIn('APP_RELEASE_ID=20261102T072200Z', target.read_text())
            self.assertIn('EnvironmentFile=/private/gemini.env', target.read_text())
            self.assertIn('AUTH_COOKIE_SECURE=true', target.read_text())
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
        self.assertEqual(untouched.read_text(), '[Service]\nEnvironment=OTHER=unchanged\n')

    def test_invalid_override_or_release_cannot_partially_modify_files(self):
        first = self.root / 'a.conf'
        first.write_text('[Service]\nEnvironment=APP_RELEASE_ID=old\n')
        invalid = self.root / 'z.conf'
        invalid.write_text('[Service]\nEnvironment=APP_RELEASE_ID=one\nEnvironment=APP_RELEASE_ID=two\n')
        for release in ('bad', '20261102T072200Z'):
            with self.subTest(release=release), self.assertRaises(ValueError):
                self.module.sync_release_id(self.root, release)
            self.assertEqual(first.read_text(), '[Service]\nEnvironment=APP_RELEASE_ID=old\n')

    def test_missing_directory_is_ignored_and_symlink_is_rejected(self):
        self.module.sync_release_id(self.root / 'missing', '20261102T072200Z')
        self.assertFalse((self.root / 'missing').exists())
        outside = self.root / 'outside'
        outside.write_text('[Service]\nEnvironment=APP_RELEASE_ID=old\n')
        (self.root / 'linked.conf').symlink_to(outside)
        with self.assertRaises(ValueError):
            self.module.sync_release_id(self.root, '20261102T072200Z')
        self.assertIn('APP_RELEASE_ID=old', outside.read_text())
