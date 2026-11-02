"""설정 import, 명시 초기화, Notion 순수 설정/발견 경계를 보호한다."""
import asyncio
import importlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


APP = Path(__file__).resolve().parents[1]


class ConfigurationBoundaryTests(unittest.TestCase):
    def module(self, name):
        self.assertIsNotNone(importlib.util.find_spec(name), f'설정 책임 모듈 누락: {name}')
        return importlib.import_module(name)

    def test_configuration_import_does_not_read_json_or_call_network(self):
        script = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import requests
from unittest.mock import patch
original = Path.read_text
def read(path, *args, **kwargs):
    if path.suffix == '.json' or path.name == '.env':
        raise AssertionError('import 중 설정 파일 읽기: ' + path.name)
    return original(path, *args, **kwargs)
with patch.object(Path, 'read_text', read), patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('import 중 HTTP')):
    from backend import config, common, customer_identity
    assert config.CUSTOMER_ALIASES is common.CUSTOMER_ALIASES
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(APP)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_configured_settings_preserve_precedence_without_discovery(self):
        settings = self.module('backend.settings.notion')
        with patch.dict(os.environ, {'NOTION_API_TOKEN': 'primary', 'NOTION_TOKEN': 'fallback',
                                    'NOTION_DATABASE_IDS': 'a-1, a1\nb-2',
                                    'NOTION_DATABASE_ID': 'unused', 'NOTION_API_VERSION': 'custom'}, clear=True):
            result = settings.get_configured_notion_settings()
        self.assertEqual(result, {'token': 'primary', 'database_ids': ['a1', 'b2'],
                                 'database_source': 'configured', 'database_discovery_error': '',
                                 'database_discovery_error_kind': '', 'version': 'custom'})
        with patch.dict(os.environ, {'NOTION_TOKEN': 'fallback', 'NOTION_ISSUE_DB_ID': 'c-3'}, clear=True):
            self.assertEqual(settings.get_configured_notion_settings()['database_ids'], ['c3'])
            self.assertEqual(settings.get_notion_headers()['Authorization'], 'Bearer fallback')
        with patch.dict(os.environ, {'NOTION_API_TOKEN': 'only-token'}, clear=True):
            self.assertEqual(settings.get_configured_notion_settings()['database_ids'], [])

    def test_pure_settings_do_not_import_http_or_discovery(self):
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
class Boundary:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'backend.config', 'backend.notion.discovery', 'backend.notion.transport'} or fullname.split('.')[0] in {'requests', 'httpx', 'fastapi'}:
            raise AssertionError('순수 설정에서 HTTP 의존: ' + fullname)
sys.meta_path.insert(0, Boundary())
from backend.settings.notion import get_configured_notion_settings, get_notion_headers
from backend.settings import catalogs
get_configured_notion_settings()
get_notion_headers()
'''
        result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(APP)],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_discovery_cache_expiry_and_failure_ttl_preserve_requests(self):
        discovery = self.module('backend.notion.discovery')
        now = [100.0]
        ok = Mock(status_code=200)
        ok.json.return_value = {'results': [{'object': 'database', 'id': 'a-1'}]}
        failed = Mock(status_code=401, text='unauthorized')
        with patch.dict(os.environ, {'NOTION_API_TOKEN': 'fixture', 'NOTION_CACHE_TTL_SECONDS': '60'}, clear=True), \
                patch.dict(discovery._notion_discovery_cache, {}, clear=True), \
                patch.object(discovery.time, 'monotonic', side_effect=lambda: now[0]), \
                patch.object(self.module('backend.notion.transport'), 'notion_post_json', side_effect=[ok, failed, ok]) as post:
            first = discovery.resolve_notion_settings()
            first['database_ids'].append('caller-change')
            now[0] = 159
            self.assertEqual(discovery.resolve_notion_settings()['database_ids'], ['a1'])
            self.assertEqual(post.call_count, 1)
            now[0] = 160
            self.assertEqual(discovery.resolve_notion_settings()['database_discovery_error_kind'], 'dependency')
            now[0] = 189
            self.assertEqual(discovery.resolve_notion_settings()['database_ids'], [])
            self.assertEqual(post.call_count, 2)
            now[0] = 190
            self.assertEqual(discovery.resolve_notion_settings()['database_ids'], ['a1'])
            self.assertEqual(post.call_count, 3)

    def catalog_context(self, folder):
        catalogs = self.module('backend.settings.catalogs')
        self.enterContext(patch.object(catalogs, 'CONFIG_DIR', Path(folder)))
        self.enterContext(patch.object(catalogs, '_initialized', False))
        for name in ('CUSTOMER_ALIASES', 'CUSTOMER_DIRECTORY', 'CUSTOMER_PROJECT_DISPLAY_NAMES'):
            self.enterContext(patch.dict(getattr(catalogs, name), {}, clear=True))
        self.enterContext(patch.dict(catalogs.DEFAULT_CUSTOMER_ALIASES, {'기본': ['기본별칭']}, clear=True))
        return catalogs

    def test_catalog_initialization_preserves_merge_order_and_object_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            catalogs = self.catalog_context(folder)
            path = Path(folder)
            (path/'customer_aliases.json').write_text(json.dumps({'고객': ['기존', '회사']}))
            (path/'customer_directory.json').write_text(json.dumps({
                'customers': {'고객': {'name': '회사', 'projects': ['001Code'], 'aliases': ['기존', '새별칭']}},
                'project_display_names': {'old': {'name': '표시전용'}}}))
            aliases = catalogs.CUSTOMER_ALIASES
            with self.assertRaisesRegex(RuntimeError, '초기화'):
                catalogs.get_customer_aliases()
            catalogs.initialize_customer_catalogs()
            self.assertIs(catalogs.get_customer_aliases(), aliases)
            self.assertEqual(aliases, {'고객': ['회사', '기존', '001Code', '새별칭']})
            self.assertEqual(catalogs.get_customer_project_display_names(), {'old': '표시전용'})
            self.assertNotIn('표시전용', str(aliases))
            with patch.object(Path, 'read_text', side_effect=AssertionError('중복 초기화')):
                catalogs.initialize_customer_catalogs()
            self.assertIs(catalogs.get_customer_aliases(), aliases)

    def test_catalog_missing_or_malformed_aliases_use_existing_fallback(self):
        with tempfile.TemporaryDirectory() as folder:
            catalogs = self.catalog_context(folder)
            self.assertEqual(catalogs.load_customer_aliases(), {'기본': ['기본별칭']})
            self.assertEqual(catalogs.load_customer_directory(), {})
            self.assertEqual(catalogs.load_customer_project_display_names(), {})
            for contents in ('{', '[]', '{"고객": [1]}'):
                with self.subTest(contents=contents):
                    (Path(folder)/'customer_aliases.json').write_text(contents)
                    self.assertEqual(catalogs.load_customer_aliases(), {'기본': ['기본별칭']})

    def test_concurrent_initialization_loads_one_shared_catalog(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        with tempfile.TemporaryDirectory() as folder:
            catalogs = self.catalog_context(folder)
            ready = Barrier(2)

            def start(_):
                ready.wait(timeout=5)
                catalogs.initialize_customer_catalogs()
                return catalogs.get_customer_aliases()

            with patch.object(catalogs, 'load_customer_aliases', wraps=catalogs.load_customer_aliases) as load, \
                    ThreadPoolExecutor(max_workers=2) as pool:
                first, second = pool.map(start, range(2))
                load.assert_called_once()
                self.assertIs(first, second)
                self.assertEqual(first, {'기본': ['기본별칭']})

    def test_invalid_directory_fails_before_publishing_catalogs_and_can_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            catalogs = self.catalog_context(folder)
            path = Path(folder)/'customer_directory.json'
            for contents in ('{', '{"customers": {"고객": {"name": ""}}}',
                             '{"project_display_names": {"code": {"name": ""}}}'):
                with self.subTest(contents=contents):
                    path.write_text(contents)
                    with self.assertRaises(ValueError):
                        catalogs.initialize_customer_catalogs()
                    self.assertEqual(catalogs.CUSTOMER_ALIASES, {})
                    with self.assertRaises(RuntimeError):
                        catalogs.get_customer_directory()
            path.write_text('{}')
            catalogs.initialize_customer_catalogs()
            self.assertEqual(catalogs.get_customer_aliases(), {'기본': ['기본별칭']})

    def test_app_initializes_catalogs_before_services_and_rejects_invalid_directory(self):
        from backend import main
        with tempfile.TemporaryDirectory() as folder:
            catalogs = self.catalog_context(folder)
            path = Path(folder)/'customer_directory.json'
            path.write_text('{')

            async def start():
                async with main.lifespan(main.app):
                    return dict(catalogs.get_customer_aliases())

            with patch.object(main, 'init_users_db') as users:
                with self.assertRaises(ValueError):
                    asyncio.run(start())
                users.assert_not_called()
            path.write_text('{}')
            observed = []
            with patch.dict(os.environ, {'APP_ENV': 'development', 'NOTION_CACHE_WARM_ENABLED': 'false',
                                         'MANTIS_COMPLETE_SYNC_ENABLED': 'false'}), \
                    patch.object(main, 'validate_runtime_security'), \
                    patch.object(main, 'init_users_db', side_effect=lambda: observed.append(dict(catalogs.get_customer_aliases()))), \
                    patch.object(main, 'init_workspace_db'), patch.object(main, 'init_audit_db'), \
                    patch.object(main, 'prune_audit_logs'):
                self.assertEqual(asyncio.run(start()), {'기본': ['기본별칭']})
                self.assertEqual(observed, [{'기본': ['기본별칭']}])

    def test_standalone_index_comparison_initializes_catalogs(self):
        result = subprocess.run([sys.executable, str(APP.parent/'scripts/benchmark_mantis_index.py'),
                                 '--documents', '40', '--runs', '3', '--minimum-speedup', '0'],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('documents=40 matches=2 ', result.stdout)
