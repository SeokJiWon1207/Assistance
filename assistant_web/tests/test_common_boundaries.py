"""공통 함수의 기존 입력 계약과 순수 모듈의 import 경계를 보호한다."""
import ast
import importlib
import inspect
import json
import subprocess
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from backend import common, config


APP = Path(__file__).resolve().parents[1]
CASES = json.loads((APP / 'tests/fixtures/common_cases.json').read_text())


class CommonBehaviorTests(unittest.TestCase):
    def test_frozen_common_signatures_and_boundary_values(self):
        for name, signature in CASES['signatures'].items():
            with self.subTest(function=name):
                self.assertEqual(str(inspect.signature(getattr(common, name))), signature)
        for case in CASES['cases']:
            with self.subTest(function=case['symbol'], args=case['args']):
                args = case['args']
                if case['symbol'] == 'add_customer_alias':
                    args = [set(args[0]), *args[1:]]
                function = getattr(common, case['symbol'])
                if 'error' in case:
                    with self.assertRaises((ValueError, TypeError)) as raised:
                        function(*args)
                    self.assertEqual(type(raised.exception).__name__, case['error']['type'])
                    self.assertEqual(str(raised.exception), case['error']['message'])
                else:
                    actual = function(*args)
                    self.assertEqual(type(actual).__name__, case['return_type'])
                    value = actual.isoformat() if isinstance(actual, date) else actual
                    self.assertEqual(json.loads(json.dumps(value)), case['expected'])
                    if 'mutated_aliases' in case:
                        self.assertEqual(sorted(args[0]), case['mutated_aliases'])

    def test_customer_catalog_mutation_and_explicit_cache_clear_keep_their_meaning(self):
        owner = importlib.import_module('backend.customer_identity')
        common.customer_alias_owner.cache_clear()
        common.customer_project_owner.cache_clear()
        self.addCleanup(common.customer_alias_owner.cache_clear)
        self.addCleanup(common.customer_project_owner.cache_clear)
        self.assertIsNone(common.customer_alias_owner('997Fixture'))
        self.assertIsNone(common.customer_project_owner('997Fixture_Project'))
        with patch.dict(config.CUSTOMER_ALIASES, {'회귀고객': ['997Fixture']}):
            self.assertIsNone(common.customer_alias_owner('997Fixture'))
            owner.customer_alias_owner.cache_clear()
            self.assertEqual(common.customer_alias_owner('997Fixture'), '회귀고객')
            self.assertIsNone(common.customer_project_owner('997Fixture_Project'))
            owner.customer_project_owner.cache_clear()
            self.assertEqual(common.customer_project_owner('997Fixture_Project'), '회귀고객')
        self.assertEqual(common.customer_alias_owner.cache_parameters(), {'maxsize': 2048, 'typed': False})
        self.assertEqual(common.customer_project_owner.cache_parameters(), {'maxsize': 512, 'typed': False})


class CommonBoundaryTests(unittest.TestCase):
    def test_pure_helpers_import_without_configuration_or_http_clients(self):
        script = r'''
import importlib
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
class Boundary:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {'backend.common', 'backend.config', 'backend.environment'} or fullname.split('.')[0] in {'requests', 'httpx', 'fastapi'}:
            raise AssertionError('순수 모듈에서 상위 설정/HTTP import: ' + fullname)
sys.meta_path.insert(0, Boundary())
original_read_text = Path.read_text
def checked_read(path, *args, **kwargs):
    if path.suffix == '.json' or path.name == '.env':
        raise AssertionError('순수 모듈에서 설정 파일 읽기: ' + path.name)
    return original_read_text(path, *args, **kwargs)
Path.read_text = checked_read
for name in sys.argv[2:]:
    importlib.import_module(name)
'''
        modules = ['backend.utils.text', 'backend.utils.numbers', 'backend.utils.dates',
                   'backend.settings.values', 'backend.mantis_status', 'backend.sources.chroma_document']
        for ordered in (modules, list(reversed(modules))):
            with self.subTest(order=ordered):
                result = subprocess.run([sys.executable, '-I', '-B', '-c', script, str(APP), *ordered],
                                        capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_production_consumers_use_owners_instead_of_common(self):
        references = []
        for folder in (APP / 'backend', APP / 'scripts', APP.parent / 'scripts'):
            for path in folder.rglob('*.py'):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and (
                        node.module == 'backend.common'
                        or node.module == 'backend' and any(alias.name == 'common' for alias in node.names)
                    ):
                        references.append((str(path.relative_to(APP.parent)), node.lineno))
                    elif isinstance(node, ast.Import) and any(alias.name == 'backend.common' for alias in node.names):
                        references.append((str(path.relative_to(APP.parent)), node.lineno))
        self.assertEqual(references, [])
