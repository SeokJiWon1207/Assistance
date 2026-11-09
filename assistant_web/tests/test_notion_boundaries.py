"""Notion의 값·HTTP·저장본·검색 조정 경계를 고정한다."""
import importlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from architecture_graph import collect_python_edges, strongly_connected_components

BACKEND = Path(__file__).resolve().parents[1] / 'backend'


class NotionBoundaryTests(unittest.TestCase):
    def test_legacy_paths_share_implementations_and_cache_state(self):
        for legacy, owner, names in (
            ('client', 'read_api', ('fetch_notion_database_schema', '_database_cache', '_database_cache_lock')),
            ('client', 'schema_cache', ('remember_notion_database_schema', '_schema_seeds')),
            ('client', 'cache_maintenance', ('start_notion_cache_warmer', 'warm_notion_database_cache')),
            ('mapping', 'values', ('notion_property_to_text', 'notion_title', 'notion_block_text')),
            ('search', 'matching', ('notion_page_matches_constraints', 'notion_page_match_score', 'notion_page_sort_key')),
            ('search', 'filter_conditions', ('merge_common_notion_constraints',)),
            ('search', 'server_filters', ('notion_database_customer_filter', '_NOTION_USER_ID_HINTS')),
            ('complete_snapshot', 'snapshot_state', ('coverage', '_state', 'catalog_max_age_seconds')),
            ('body_index_sync', 'crawl_transport', ('_request', '_Budget', '_ReadFailure')),
            ('body_index', 'page_ownership', ('segment_belongs_to_page', 'page_belongs_to_database')),
        ):
            old = importlib.import_module('backend.notion.' + legacy)
            new = importlib.import_module('backend.notion.' + owner)
            for name in names:
                with self.subTest(module=legacy, name=name):
                    self.assertIs(getattr(old, name), getattr(new, name))

    def test_pure_matching_import_does_not_load_storage_or_http(self):
        code = '''
import importlib, sys
class Boundary:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in {"backend.notion.body_index", "backend.notion.read_api", "backend.notion.discovery"}:
            raise AssertionError("순수 매칭의 실행 계층 의존: " + fullname)
sys.meta_path.insert(0, Boundary())
importlib.import_module("backend.notion.values")
importlib.import_module("backend.notion.matching")
'''
        result = subprocess.run(
            [sys.executable, '-I', '-B', '-c', f'import sys; sys.path.insert(0, {str(BACKEND.parent)!r})\n' + code],
            capture_output=True, text=True, timeout=20, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_frozen_property_values_and_business_dates_are_unchanged(self):
        from backend.notion import values
        cases = json.loads((Path(__file__).parent / 'fixtures/notion_boundary_cases.json').read_text())
        for case in cases:
            with self.subTest(kind=case['kind'], value=case['input']):
                convert = values.notion_kst_date if case['kind'] == 'date' else values.notion_property_to_text
                self.assertEqual(convert(case['input']), case['output'])

    def test_warmer_stops_and_refreshes_before_half_catalog_lifetime(self):
        from backend.notion import cache_maintenance, snapshot_maintenance, snapshot_state, semantic_index
        for interval, lifetime, expected in ((3600, 1200, 600), (300, 1200, 300)):
            with self.subTest(interval=interval):
                stop = Mock()
                stop.is_set.side_effect = [False, True]
                with (
                    patch.object(cache_maintenance, 'Event', return_value=stop),
                    patch.object(cache_maintenance, 'Thread') as thread,
                    patch.object(cache_maintenance, 'warm_notion_database_cache', return_value=2) as warm,
                    patch.object(snapshot_maintenance, 'refresh_catalog_cache', return_value={'status': 'fresh'}) as refresh,
                    patch.object(semantic_index, 'sync_semantic_index', return_value={'status': 'ready'}) as semantic,
                    patch.object(snapshot_state, 'catalog_max_age_seconds', return_value=lifetime),
                ):
                    self.assertIs(cache_maintenance.start_notion_cache_warmer(interval), stop)
                    thread.return_value.start.assert_called_once()
                    self.assertTrue(thread.call_args.kwargs['daemon'])
                    thread.call_args.kwargs['target']()
                warm.assert_called_once()
                refresh.assert_called_once()
                semantic.assert_called_once_with(max_chunks=256, max_seconds=30)
                stop.wait.assert_called_once_with(expected)

    def test_no_runtime_cycle_contains_a_notion_module(self):
        cycles = strongly_connected_components(collect_python_edges(BACKEND))
        self.assertEqual([sorted(c) for c in cycles if any(n.startswith('backend.notion.') for n in c)], [])

    def test_foundations_do_not_import_search_or_maintenance(self):
        owners = {'backend.notion.' + name for name in (
            'values', 'matching', 'filter_conditions', 'page_ownership', 'schema_cache', 'read_api', 'crawl_transport', 'crawl_validation', 'snapshot_state',
        )}
        forbidden = {'backend.notion.' + name for name in (
            'client', 'search', 'complete_snapshot', 'snapshot_maintenance', 'cache_maintenance', 'body_index_sync',
        )}
        edges = collect_python_edges(BACKEND)
        self.assertEqual([(e.importer, e.scope, e.imported) for e in edges
                          if e.importer in owners and e.imported in forbidden], [])
        for module in owners:
            with self.subTest(module=module):
                self.assertIsNotNone(importlib.util.find_spec(module), '독립 소유 모듈이 필요합니다')
