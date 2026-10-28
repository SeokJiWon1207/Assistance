"""전수 검색은 신선한 전체 목록과 모든 페이지의 읽기 근거가 있을 때만 허용한다."""
import importlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

from backend.notion.body_index import BodyIndex
from test_notion_body_index import page, segment


class CompleteSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.index = BodyIndex(Path(self.directory.name) / 'body.sqlite3', fingerprint='connection')

    def snapshot(self):
        return importlib.import_module('backend.notion.complete_snapshot')

    def add(self, identifier='page-1', text='needle', complete=True, coverage=None):
        raw = page(identifier, title='주간보고')
        self.index.upsert_catalog('db-1', [raw], complete=True)
        self.index.replace_content(identifier, [segment(text, owner=identifier)], complete=complete,
                                   expected_edited_time=raw['last_edited_time'], coverage=coverage)

    def test_empty_or_unfinished_catalog_cannot_be_complete(self):
        self.assertFalse(self.snapshot().coverage(self.index, ['db-1'])['complete'])
        self.index.upsert_catalog('db-1', [page()], complete=False)
        self.assertFalse(self.snapshot().coverage(self.index, ['db-1'])['complete'])

    def test_unchanged_body_uses_fresh_catalog_revision_not_body_age(self):
        self.add()
        with closing(sqlite3.connect(self.index.path)) as db, db:
            db.execute("UPDATE pages SET indexed_at='2026-10-13T00:00:00+00:00'")
        result = self.snapshot().coverage(self.index, ['db-1'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['page_count'], 1)
        self.assertTrue(result['snapshot_as_of'])

    def test_changed_revision_and_unknown_failures_remain_incomplete(self):
        self.add()
        self.index.upsert_page('db-1', page(revision='2026-10-14T00:00:00Z'))
        self.assertFalse(self.snapshot().coverage(self.index, ['db-1'])['complete'])
        self.add()
        self.index.record_failure('page-1', 'http_429')
        self.assertFalse(self.snapshot().coverage(self.index, ['db-1'])['complete'])

    def test_api_unsupported_elements_are_explicit_exclusions(self):
        self.add(complete=False, coverage={
            'supported_content_complete': True, 'incomplete_reasons': ['unsupported_block'],
            'unsupported_elements': [{'block_id': 'button', 'block_type': 'button'}],
        })
        self.index.record_failure('page-1', 'unsupported_block')
        result = self.snapshot().coverage(self.index, ['db-1'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['excluded_element_count'], 1)
        self.assertEqual(result['excluded_element_types'], {'button': 1})

    def test_missing_or_old_diagnostics_do_not_certify_partial_content(self):
        self.add(complete=False)
        self.assertFalse(self.snapshot().coverage(self.index, ['db-1'])['complete'])
        self.add(complete=False, coverage={'supported_content_complete': False,
                                         'incomplete_reasons': ['time_limit']})
        self.assertFalse(self.snapshot().coverage(self.index, ['db-1'])['complete'])

    def test_latest_supported_partial_replaces_stale_complete_evidence(self):
        self.add(text='removed needle')
        self.index.replace_content('page-1', [segment('new token', owner='page-1')], complete=False,
            expected_edited_time=page()['last_edited_time'], coverage={
                'supported_content_complete': True, 'incomplete_reasons': ['unsupported_block'],
                'unsupported_elements': [{'block_id': 'b', 'block_type': 'button'}]})
        self.index.record_failure('page-1', 'unsupported_block')
        records = self.snapshot().records(self.index, ['db-1'], ['token'])
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['content'], 'new token')
        self.assertEqual(self.snapshot().records(self.index, ['db-1'], ['needle']), [])

    def test_snapshot_search_keeps_more_than_thirty_matches_and_metadata_only_hits(self):
        rows = [page(f'p{i}', title='주간보고') for i in range(40)]
        self.index.upsert_catalog('db-1', rows, complete=True)
        for raw in rows:
            self.index.replace_content(raw['id'], [segment('needle', owner=raw['id'])], complete=True,
                                       expected_edited_time=raw['last_edited_time'])
        self.assertEqual(len(self.snapshot().records(self.index, ['db-1'], ['needle'])), 40)
        self.assertEqual(len(self.snapshot().records(self.index, ['db-1'], ['주간보고'])), 40)
        self.assertEqual(self.snapshot().records(self.index, ['other-db'], ['needle']), [])

    def test_new_empty_supported_snapshot_drops_old_text(self):
        self.add(text='deleted needle')
        self.index.replace_content('page-1', [], complete=False,
            expected_edited_time=page()['last_edited_time'], coverage={
                'supported_content_complete': True, 'incomplete_reasons': ['unsupported_block']})
        self.index.record_failure('page-1', 'unsupported_block')
        self.assertTrue(self.snapshot().coverage(self.index, ['db-1'])['complete'])
        self.assertEqual(self.snapshot().records(self.index, ['db-1'], ['needle']), [])

    def test_stale_catalog_is_not_complete(self):
        self.add()
        with closing(sqlite3.connect(self.index.path)) as db, db:
            db.execute("UPDATE catalogs SET updated_at='2020-01-01T00:00:00+00:00'")
        self.assertIsNone(self.snapshot().read_complete(self.index, ['db-1'], ['needle']))

    def test_unusable_catalog_falls_back_without_reading_page_or_body_tables(self):
        from unittest.mock import patch
        snapshot = self.snapshot()
        connect = sqlite3.connect
        read_tables = set()

        def track_reads(action, table, _column, _database, _trigger):
            if action == sqlite3.SQLITE_READ:
                read_tables.add(table)
            return sqlite3.SQLITE_OK

        def traced_connect(*args, **kwargs):
            db = connect(*args, **kwargs)
            db.set_authorizer(track_reads)
            return db

        for scope, update in (
            ([], ''),
            (['db-1', 'missing'], ''),
            (['db-1'], 'UPDATE catalogs SET complete=0'),
            (['db-1'], "UPDATE catalogs SET updated_at='2020-01-01T00:00:00+00:00'"),
            (['db-1'], "UPDATE catalogs SET updated_at='9999-01-01T00:00:00+00:00'"),
            (['db-1'], "UPDATE catalogs SET updated_at='invalid'"),
        ):
            with self.subTest(scope=scope, update=update):
                self.add()
                if update:
                    with closing(connect(self.index.path)) as db, db:
                        db.execute(update)
                read_tables.clear()
                with (
                    patch.object(snapshot, 'open_body_index', return_value=self.index),
                    patch.object(snapshot, '_scope_current', return_value=True),
                    patch.dict('os.environ', {'NOTION_FETCH_PAGE_BLOCKS': 'true',
                                             'NOTION_SNAPSHOT_CATALOG_MAX_AGE_SECONDS': '7200'}),
                    patch.object(sqlite3, 'connect', side_effect=traced_connect),
                ):
                    result = snapshot.search_complete_snapshot(
                        scope, terms=['needle'], read_scope={key: key for key in scope}, constraints={},
                        pre_body_constraints={}, body_clauses=[], keyword_clauses=[], limit=5)
                self.assertIsNone(result)
                self.assertFalse(read_tables & {'pages', 'blocks', 'partial_blocks', 'collection_coverage'},
                                 f'사용할 수 없는 목록에서 원문을 읽었습니다: {read_tables}')

    def test_complete_search_uses_all_candidates_and_keeps_display_limit_separate(self):
        from unittest.mock import patch
        from backend.notion import complete_snapshot as snapshot
        candidates = [{'id': str(i), 'match_score': 1, 'last_edited_time': ''} for i in range(40)]
        self.add()
        with patch.object(snapshot, 'open_body_index', return_value=self.index), patch.object(
            snapshot, '_current_results', return_value=True), patch(
            'backend.notion.search._indexed_body_matches', return_value=candidates):
            result = snapshot.search_complete_snapshot(
                ['db-1'], terms=['needle'], read_scope={'db-1': ''}, constraints={},
                pre_body_constraints={}, body_clauses=[], keyword_clauses=[], limit=5)
        self.assertTrue(result['meta']['search_complete'])
        self.assertEqual(result['meta']['candidate_count'], 40)
        self.assertEqual(len(result['notion_pages']), 5)
        self.assertEqual(result['meta']['status'], 'snapshot')

    def test_revoked_result_never_returns_stored_content(self):
        from unittest.mock import patch
        from backend.notion import complete_snapshot as snapshot
        self.add()
        with patch.object(snapshot, 'open_body_index', return_value=self.index), patch.object(
            snapshot, '_current_results', return_value=False), patch(
            'backend.notion.search._indexed_body_matches', return_value=[{'id': 'page1'}]):
            result = snapshot.search_complete_snapshot(
                ['db-1'], terms=['needle'], read_scope={'db-1': ''}, constraints={},
                pre_body_constraints={}, body_clauses=[], keyword_clauses=[], limit=5)
        self.assertIsNone(result)

    def test_paginated_property_is_incomplete_until_all_items_are_read(self):
        from unittest.mock import patch
        from backend.notion.property_snapshot import read_full_properties, pending_properties
        raw = page()
        raw['properties']['관계'] = {'id': 'related', 'type': 'relation', 'relation': [], 'has_more': True}
        self.index.upsert_catalog('db-1', [raw], complete=True)
        self.index.replace_content('page-1', [segment('needle')], complete=True,
                                   expected_edited_time=raw['last_edited_time'])
        self.assertFalse(self.snapshot().coverage(self.index, ['db-1'])['complete'])
        with patch('backend.notion.body_index_sync._request', side_effect=[
            {'results': [{'relation': {'id': 'first'}}], 'has_more': True, 'next_cursor': 'next'},
            {'results': [{'relation': {'id': 'last'}}], 'has_more': False},
        ]) as request:
            full = read_full_properties(raw, None)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(full['properties']['관계']['relation'], [{'id': 'first'}, {'id': 'last'}])
        self.assertEqual(pending_properties(full), [])

    def test_search_pipeline_counts_every_match_before_displaying_five(self):
        from unittest.mock import patch
        from backend.notion import search
        import test_notion_accuracy_regressions as fixtures
        fixtures.NotionAccuracyRegressionTests.search_fixture(self, [], {})
        rows = [page(f'p{i}', database='db', title='주간보고') for i in range(40)]
        self.index.upsert_catalog('db', rows, complete=True)
        for raw in rows:
            self.index.replace_content(raw['id'], [segment('이슈진행', owner=raw['id'])], complete=True,
                                       expected_edited_time=raw['last_edited_time'])
        with patch('backend.notion.complete_snapshot.open_body_index', return_value=self.index), patch(
            'backend.notion.complete_snapshot._current_results', return_value=True):
            result = search.execute_notion_search('노션 "주간보고" 문서에서 본문에 "이슈진행" 포함된 문서 찾아줘')
        self.assertEqual(result['meta']['candidate_count'], 40)
        self.assertEqual(len(result['notion_pages']), 5)
        self.assertTrue(result['meta']['search_complete'])
        self.assertEqual(result['meta']['incomplete_reasons'], [])

    def test_child_revalidation_reuses_only_unchanged_complete_body(self):
        from unittest.mock import patch
        import time
        from backend.notion.snapshot_maintenance import refresh_page
        parent = page('parent')
        child = {**page('child'), 'parent': {'type': 'page_id', 'page_id': 'parent'}}
        self.index.upsert_catalog('db-1', [parent], complete=True)
        self.index.upsert_page('db-1', child, parent_pages={'parent': parent})
        for raw in [parent, child]:
            self.index.replace_content(raw['id'], [segment('needle', owner=raw['id'])], complete=True,
                                       expected_edited_time=raw['last_edited_time'])
        self.index.upsert_catalog('db-1', [parent], complete=True)
        self.assertIn('child', self.snapshot().coverage(self.index, ['db-1'])['pending_page_ids'])
        with patch('backend.notion.snapshot_maintenance._request', return_value=child), patch(
            'backend.notion.snapshot_maintenance._page_ancestors', return_value={'parent': parent}), patch(
            'backend.notion.snapshot_maintenance.validate_index_candidate') as crawl:
            self.assertTrue(refresh_page(self.index, 'child', ['db1'], time.monotonic() + 30))
            crawl.assert_not_called()
        self.assertTrue(self.snapshot().coverage(self.index, ['db-1'])['complete'])

    def test_revoked_synced_source_is_not_returned_from_snapshot(self):
        from unittest.mock import patch
        from backend.notion.body_index_sync import _ReadFailure
        self.add()
        candidate = {'raw_page': page(), 'database_id': 'db1',
                     'segments': [{'synced_source_id': 'source'}]}
        with patch('backend.notion.body_index_sync._request', return_value=page()), patch(
            'backend.notion.body_index_sync._synced_source', side_effect=_ReadFailure('http_404')):
            self.assertFalse(self.snapshot()._current_results(
                self.index, [{'id': 'page-1'}], [candidate], ['db1']))

    def test_keyword_free_search_can_count_the_complete_snapshot(self):
        from unittest.mock import patch
        from backend.notion import search
        import test_notion_accuracy_regressions as fixtures
        fixtures.NotionAccuracyRegressionTests.search_fixture(self, [], {})
        raw = page(database='db', title='일반 문서')
        self.index.upsert_catalog('db', [raw], complete=True)
        self.index.replace_content(raw['id'], [], complete=True, expected_edited_time=raw['last_edited_time'])
        with patch('backend.notion.complete_snapshot.open_body_index', return_value=self.index), patch(
            'backend.notion.complete_snapshot._current_results', return_value=True):
            result = search.execute_notion_search('노션 문서 전체 목록 보여줘', planned_terms=[])
        self.assertTrue(result['meta']['search_complete'])
        self.assertEqual(result['meta']['candidate_count'], 1)

    def test_stale_or_changed_read_scope_cannot_certify_a_full_snapshot(self):
        import json
        from unittest.mock import patch
        from datetime import datetime, timezone
        root = Path(self.directory.name)
        saved = {'scope_complete': True, 'generated_at': '2020-01-01T00:00:00Z', 'read_scope': {'db1': 'db1'}}
        with patch('backend.config.RUNTIME_DIR', root), patch(
            'backend.notion.read_scope.child_database_search_enabled', return_value=True):
            (root / 'notion_read_scope.json').write_text(json.dumps(saved))
            self.assertFalse(self.snapshot()._scope_current({'db1': 'db1'}))
            saved['generated_at'] = datetime.now(timezone.utc).isoformat()
            (root / 'notion_read_scope.json').write_text(json.dumps(saved))
            self.assertTrue(self.snapshot()._scope_current({'db1': 'db1'}))
            self.assertFalse(self.snapshot()._scope_current({'other': 'other'}))

    def test_snapshot_scope_uses_configured_discovery_lifetime(self):
        import json
        import os
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        root = Path(self.directory.name)
        saved = {'scope_complete': True, 'read_scope': {'db1': 'db1'},
                 'generated_at': (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()}
        (root / 'notion_read_scope.json').write_text(json.dumps(saved))
        with patch('backend.config.RUNTIME_DIR', root), patch(
            'backend.notion.read_scope.child_database_search_enabled', return_value=True):
            with patch.dict(os.environ, {'NOTION_READ_SCOPE_TTL_SECONDS': '604800'}):
                self.assertTrue(self.snapshot()._scope_current({'db1': 'db1'}))
            with patch.dict(os.environ, {'NOTION_READ_SCOPE_TTL_SECONDS': '86400'}):
                self.assertFalse(self.snapshot()._scope_current({'db1': 'db1'}))

    def test_final_search_preserves_matches_in_arbitrary_property_values(self):
        from unittest.mock import patch
        from backend.notion import search
        import test_notion_accuracy_regressions as fixtures
        fixtures.NotionAccuracyRegressionTests.search_fixture(self, [], {})
        raw = page(database='db', title='운영 문서')
        raw['properties']['비고'] = {'type': 'rich_text', 'rich_text': [{'plain_text': 'unique-needle'}]}
        self.index.upsert_catalog('db', [raw], complete=True)
        self.index.replace_content(raw['id'], [], complete=True, expected_edited_time=raw['last_edited_time'])
        with patch('backend.notion.complete_snapshot.open_body_index', return_value=self.index), patch(
            'backend.notion.complete_snapshot._current_results', return_value=True):
            result = search.execute_notion_search('노션 unique-needle 문서 찾아줘', planned_terms=['unique-needle'])
        self.assertTrue(result['meta']['search_complete'])
        self.assertEqual(result['meta']['candidate_count'], 1)

    def test_deleted_child_is_explicitly_excluded_and_retried_next_catalog(self):
        from unittest.mock import patch
        import time
        from backend.notion.snapshot_maintenance import refresh_page
        parent = page('parent')
        child = {**page('child'), 'parent': {'type': 'page_id', 'page_id': 'parent'}}
        self.index.upsert_catalog('db-1', [parent], complete=True)
        self.index.upsert_page('db-1', child, parent_pages={'parent': parent})
        for raw in [parent, child]:
            self.index.replace_content(raw['id'], [segment('needle', owner=raw['id'])], complete=True,
                                       expected_edited_time=raw['last_edited_time'])
        with patch('backend.notion.snapshot_maintenance._request', return_value={**child, 'archived': True}), patch(
            'backend.notion.snapshot_maintenance._page_ancestors', return_value={'parent': parent}):
            refresh_page(self.index, 'child', ['db1'], time.monotonic() + 30)
        proof = self.snapshot().coverage(self.index, ['db1'])
        self.assertTrue(proof['complete'])
        self.assertEqual(proof['excluded_document_count'], 1)
        self.assertEqual([r['page_id'] for r in self.snapshot().records(self.index, ['db1'], ['needle'])], ['parent'])
        self.index.upsert_catalog('db1', [parent], complete=True)
        self.assertIn('child', self.snapshot().coverage(self.index, ['db1'])['pending_page_ids'])

    def test_empty_synced_dependency_requires_a_read_after_each_catalog(self):
        self.add(coverage={'supported_content_complete': True, 'synced_reference_ids': ['source'],
                           'verified_synced_references': [{'source_id': 'source', 'database_id': 'db1'}]})
        self.assertTrue(self.snapshot().coverage(self.index, ['db1'])['complete'])
        self.index.upsert_catalog('db1', [page()], complete=True)
        self.assertFalse(self.snapshot().coverage(self.index, ['db1'])['complete'])

    def test_empty_synced_dependency_is_checked_before_returning_results(self):
        from unittest.mock import patch
        from backend.notion.body_index_sync import _ReadFailure
        self.add()
        candidate = {'raw_page': page(), 'database_id': 'db1', 'segments': [],
                     'verified_synced_references': [{'source_id': 'empty-source', 'database_id': 'db1'}]}
        with patch('backend.notion.body_index_sync._request', return_value=page()), patch(
            'backend.notion.body_index_sync._synced_source', side_effect=_ReadFailure('http_404')) as source:
            self.assertFalse(self.snapshot()._current_results(self.index, [{'id': 'page-1'}], [candidate], ['db1']))
        source.assert_called_once()

    def test_transient_child_failure_is_not_an_api_exclusion(self):
        from unittest.mock import patch
        import time
        from backend.notion.snapshot_maintenance import refresh_page
        from backend.notion.body_index_sync import _ReadFailure
        parent = page('parent')
        child = {**page('child'), 'parent': {'type': 'page_id', 'page_id': 'parent'}}
        self.index.upsert_catalog('db1', [parent], complete=True)
        self.index.upsert_page('db1', child, parent_pages={'parent': parent})
        with patch('backend.notion.snapshot_maintenance._request', side_effect=_ReadFailure('http_429')):
            self.assertFalse(refresh_page(self.index, 'child', ['db1'], time.monotonic() + 30))
        proof = self.snapshot().coverage(self.index, ['db1'])
        self.assertFalse(proof['complete'])
        self.assertEqual(proof['excluded_document_count'], 0)

    def test_restored_excluded_page_requires_successful_body_read(self):
        self.add()
        self.index.exclude_page('page-1', 'http_404')
        self.assertEqual(self.snapshot().records(self.index, ['db1'], ['needle']), [])
        self.index.upsert_catalog('db1', [page()], complete=True)
        self.assertFalse(self.snapshot().coverage(self.index, ['db1'])['complete'])
        self.index.replace_content('page-1', [segment('needle')], complete=True,
                                   expected_edited_time=page()['last_edited_time'])
        self.assertTrue(self.snapshot().coverage(self.index, ['db1'])['complete'])
        self.assertEqual(len(self.snapshot().records(self.index, ['db1'], ['needle'])), 1)

    def test_catalog_older_than_refresh_window_cannot_certify_snapshot(self):
        import os
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        self.add()
        self.assertTrue(self.snapshot().coverage(self.index, ['db-1'])['complete'])
        three_hours_ago = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        with closing(sqlite3.connect(self.index.path)) as db, db:
            db.execute("UPDATE catalogs SET updated_at=?", [three_hours_ago])
        # 매시간 목록 갱신이 두 번 빠지면 새 문서를 놓쳤을 수 있으므로 전수 판정을 내리지 않는다.
        self.assertFalse(self.snapshot().coverage(self.index, ['db-1'])['complete'])
        self.assertIsNone(self.snapshot().read_complete(self.index, ['db-1'], ['needle']))
        with patch.dict(os.environ, {'NOTION_SNAPSHOT_CATALOG_MAX_AGE_SECONDS': '14400'}):
            self.assertTrue(self.snapshot().coverage(self.index, ['db-1'])['complete'])

    def test_read_scope_freshness_is_independent_of_catalog_window(self):
        import json
        from datetime import datetime, timedelta, timezone
        from unittest.mock import patch
        root = Path(self.directory.name)
        saved = {'scope_complete': True, 'read_scope': {'db1': 'db1'},
                 'generated_at': (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()}
        with patch('backend.config.RUNTIME_DIR', root), patch(
                'backend.notion.read_scope.child_database_search_enabled', return_value=True):
            (root / 'notion_read_scope.json').write_text(json.dumps(saved))
            self.assertTrue(self.snapshot()._scope_current({'db1': 'db1'}))

    def test_scheduled_refresh_rereads_catalog_even_when_recent(self):
        import json
        import sys
        from datetime import datetime, timezone
        from unittest.mock import patch
        # 스크립트는 import 시 .env를 읽으므로 로컬 설정이 다른 테스트로 새지 않게 막는다.
        with patch('backend.environment.load_dotenv'):
            from scripts import refresh_notion_snapshot as script
        root = Path(self.directory.name)
        now = datetime.now(timezone.utc).isoformat()
        proof = {'complete': True, 'catalog_complete': True, 'snapshot_as_of': now, 'pending_page_ids': []}
        (root / 'notion_read_scope.json').write_text(json.dumps({'generated_at': now}))
        with patch.object(script, 'RUNTIME_DIR', root), \
                patch.object(script, 'get_notion_settings', return_value={'database_ids': ['db1'], 'token': 't'}), \
                patch.object(script, 'resolve_notion_read_scope', return_value=({'db1': 'db1'}, True)), \
                patch.object(script, 'open_body_index', return_value=self.index), \
                patch.object(script, 'coverage', return_value=proof), \
                patch.object(script, 'refresh_snapshot', return_value=proof) as refresh, \
                patch.object(sys, 'argv', ['refresh_notion_snapshot.py']), \
                patch('builtins.print'):
            self.assertEqual(script.main(), 0)
        self.assertIs(refresh.call_args.kwargs['refresh_catalog'], True)

    def fresh_inline_state(self):
        snapshot = self.snapshot()
        snapshot._inline_refresh_failures.clear()
        self.addCleanup(snapshot._inline_refresh_failures.clear)
        return Path(self.directory.name)

    def inline_search(self, root, current_results, refresh):
        from unittest.mock import patch
        snapshot = self.snapshot()
        candidates = [{'id': 'page-1', 'match_score': 1, 'last_edited_time': ''}]
        with patch.object(snapshot, 'open_body_index', return_value=self.index), \
                patch.object(snapshot, '_current_results', side_effect=current_results), \
                patch('backend.notion.search._indexed_body_matches', return_value=candidates), \
                patch('backend.notion.snapshot_maintenance.refresh_page', side_effect=refresh) as refresh_mock, \
                patch('backend.config.RUNTIME_DIR', root):
            result = snapshot.search_complete_snapshot(
                ['db-1'], terms=['needle'], read_scope={'db-1': ''}, constraints={},
                pre_body_constraints={}, body_clauses=[], keyword_clauses=[], limit=5)
        return result, refresh_mock

    def reindex(self, *_args, **_kwargs):
        self.add()
        return True

    def test_changed_result_is_reread_inline_instead_of_dropping_the_snapshot(self):
        root = self.fresh_inline_state()
        self.add()
        checks = []

        def current(index, shown, records, allowed):
            checks.append(1)
            if len(checks) == 1:
                index.record_failure('page-1', 'snapshot_changed_or_unavailable')
                return False
            return True

        result, refresh = self.inline_search(root, current, self.reindex)
        self.assertIsNotNone(result)
        self.assertTrue(result['meta']['search_complete'])
        self.assertEqual(result['meta']['inline_refreshed_page_count'], 1)
        self.assertEqual(refresh.call_args.args[1], 'page1')
        self.assertLessEqual(refresh.call_args.kwargs['limits'].max_requests, 60)
        self.assertEqual(len(checks), 2)

    def test_page_changing_again_after_inline_read_falls_back_without_looping(self):
        root = self.fresh_inline_state()
        self.add()

        def always_changed(index, shown, records, allowed):
            index.record_failure('page-1', 'snapshot_changed_or_unavailable')
            return False

        result, refresh = self.inline_search(root, always_changed, self.reindex)
        self.assertIsNone(result)
        self.assertEqual(refresh.call_count, 1)

    def test_pending_pages_over_inline_limit_fall_back_without_reads(self):
        import os
        from unittest.mock import patch
        root = self.fresh_inline_state()
        self.add()
        self.index.record_failure('page-1', 'fetch_failed')
        with patch.dict(os.environ, {'NOTION_SNAPSHOT_INLINE_REFRESH_MAX_PAGES': '0'}):
            result, refresh = self.inline_search(root, lambda *args: True, self.reindex)
        self.assertIsNone(result)
        refresh.assert_not_called()

    def test_failed_inline_read_falls_back_and_is_not_retried_immediately(self):
        root = self.fresh_inline_state()
        self.add()
        self.index.record_failure('page-1', 'fetch_failed')
        result, refresh = self.inline_search(root, lambda *args: True, lambda *args, **kwargs: False)
        self.assertIsNone(result)
        self.assertEqual(refresh.call_count, 1)
        result, refresh = self.inline_search(root, lambda *args: True, self.reindex)
        self.assertIsNone(result)
        refresh.assert_not_called()

    def test_inline_read_waits_for_nothing_while_scheduled_refresh_holds_lock(self):
        import fcntl
        root = self.fresh_inline_state()
        self.add()
        self.index.record_failure('page-1', 'fetch_failed')
        with (root / 'notion-snapshot.lock').open('a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result, refresh = self.inline_search(root, lambda *args: True, self.reindex)
        self.assertIsNone(result)
        refresh.assert_not_called()
