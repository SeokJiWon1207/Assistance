"""오프라인 CI에서도 원본 출처와 배포 번들의 최신 상태를 확인한다."""
import hashlib
import json
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1]
FRONTEND = APP / 'frontend'
VENDOR = FRONTEND / 'vendor' / 'bklit'
BUNDLE = APP / 'backend' / 'static' / 'vendor' / 'bklit'


class BklitBundleTests(unittest.TestCase):
    def test_original_components_match_pinned_upstream(self):
        manifest = json.loads((VENDOR / 'manifest.json').read_text())
        self.assertEqual(manifest['repository'], 'https://github.com/bklit/bklit-ui')
        self.assertEqual(manifest['license'], 'MIT')
        self.assertEqual(len(manifest['commit']), 40)
        for name, digest in manifest['files'].items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((VENDOR / name).read_bytes()).hexdigest(), digest)

    def test_deploy_bundle_matches_sources_and_lockfile(self):
        manifest = json.loads((BUNDLE / 'build.json').read_text())
        source = hashlib.sha256()
        def walk(directory):
            for entry in sorted(directory.iterdir()):
                if entry.is_dir():
                    walk(entry)
                else:
                    source.update(entry.relative_to(FRONTEND).as_posix().encode())
                    source.update(entry.read_bytes())
        for folder in ('src', 'vendor', 'scripts'):
            walk(FRONTEND / folder)
        for name in ('package.json', 'package-lock.json', 'tsconfig.json', '../backend/static/modules/chart_models.js'):
            source.update((FRONTEND / name).read_bytes())
        self.assertEqual(source.hexdigest(), manifest['sourceHash'], 'npm --prefix assistant_web/frontend run build 필요')
        for name, digest in manifest['outputs'].items():
            self.assertEqual(hashlib.sha256((BUNDLE / name).read_bytes()).hexdigest(), digest)
