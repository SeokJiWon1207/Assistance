"""검증된 제목·속성 목록은 본문 수집과 독립적으로 빠르게 검색한다."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

import requests

from backend.notion import complete_snapshot, search
from backend.notion.body_index import BodyIndex
from backend.notion.body_index_sync import _ReadFailure
from test_notion_body_index import page


class NotionCatalogSearchTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.index = BodyIndex(Path(directory) / 'index.sqlite3', fingerprint='connection')
        self.scope = {f'db{i}': f'db{i}' for i in range(240)}
        for identifier in self.scope:
            self.index.upsert_catalog(identifier, [], complete=True)
        self.current = {}
        self.live_queries = []
        self.page_reads = []
        self.enterContext(patch.dict('os.environ', {'NOTION_TITLE_QUERY_CACHE_SECONDS': '0'}))
        self.enterContext(patch.object(complete_snapshot, 'open_body_index', return_value=self.index))
        self.enterContext(patch.object(complete_snapshot, '_scope_current', return_value=True))
        self.enterContext(patch.object(search, 'get_notion_settings', return_value={
            'token': 'fixture-token', 'database_ids': list(self.scope)}))
        self.enterContext(patch.object(search, 'resolve_notion_read_scope', return_value=(self.scope, True)))
        self.enterContext(patch.object(search, 'fetch_notion_database_title', return_value='업무'))
        self.enterContext(patch.object(search, 'notion_property_schema', return_value={'제목': {'type': 'title'}}))
        self.enterContext(patch.object(search, 'notion_post_json', side_effect=self.query))
        self.enterContext(patch('backend.notion.body_index_sync._request', side_effect=self.read_page))

    def add(self, identifier='p1', title='배포 계획', status='진행중', database='db0'):
        raw = page(identifier, title=title, database=database)
        raw['properties']['상태'] = {'type': 'status', 'status': {'name': status}}
        self.index.upsert_catalog(database, [raw], complete=True)
        self.current[identifier] = deepcopy(raw)
        return raw

    def read_page(self, endpoint, *_args, **_kwargs):
        if endpoint.startswith('pages/') and endpoint.count('/') == 1:
            identifier = endpoint.split('/')[1]
            self.page_reads.append(identifier)
            value = self.current.get(identifier)
            if isinstance(value, Exception):
                raise value
            return deepcopy(value or {})
        raise _ReadFailure('unavailable_body')

    def query(self, url, payload, **_kwargs):
        self.live_queries.append((url, payload))
        database_id = url.split('/databases/')[1].split('/')[0]
        title = (payload.get('filter') or {}).get('title', {}).get('equals')
        rows = [deepcopy(raw) for raw in self.current.values() if isinstance(raw, dict)
                and raw.get('parent', {}).get('database_id') == database_id
                and (title is None or raw['properties']['제목']['title'][0]['plain_text'] == title)]
        result = requests.Response()
        result.status_code = 200
        result._content = json.dumps({'results': rows, 'has_more': False}).encode()
        return result

    def run_search(self, question="노션에서 '배포 계획' 문서 찾아줘", **kwargs):
        return search.execute_notion_search(question, include_blocks=False, **kwargs)

    def test_exact_title_uses_catalog_and_one_live_database_query_in_240_databases(self):
        self.add()
        result = self.run_search()
        self.assertEqual([row['id'] for row in result['notion_pages']], ['p1'])
        self.assertTrue(result['meta']['search_complete'])
        self.assertEqual(result['meta']['status'], 'snapshot')
        self.assertEqual(result['meta']['coverage_basis'], 'api_properties')
        self.assertEqual(len(self.live_queries), 1)
        self.assertEqual(self.page_reads, [])
        self.assertFalse(self.index.status(list(self.scope))['content_complete'])

    def test_disabling_body_fetch_keeps_the_metadata_snapshot_fast_path(self):
        self.add()
        with patch.dict('os.environ', {'NOTION_FETCH_PAGE_BLOCKS': 'false'}):
            result = self.run_search()
        self.assertEqual([row['id'] for row in result['notion_pages']], ['p1'])
        self.assertEqual(result['meta'].get('coverage_basis'), 'api_properties')
        self.assertEqual(len(self.live_queries), 1)

    def test_unrelated_unread_bodies_do_not_turn_absent_title_into_240_queries(self):
        self.add(title='다른 문서')
        result = self.run_search()
        self.assertEqual(result['notion_pages'], [])
        self.assertTrue(result['meta']['search_complete'])
        self.assertEqual(self.live_queries, [])
        self.assertEqual(self.page_reads, [])

    def test_excluded_candidates_are_rechecked_before_applying_current_status(self):
        for before, after, expected in [('운영반영', '진행중', ['p1']), ('진행중', '운영반영', [])]:
            with self.subTest(before=before, after=after):
                self.add(status=before)
                self.current['p1']['properties']['상태']['status']['name'] = after
                result = self.run_search("노션에서 '배포 계획' 문서 찾아줘. 운영반영 상태는 빼고")
                self.assertEqual([row['id'] for row in result['notion_pages']], expected)
                self.assertTrue(result['meta']['search_complete'])
        self.assertEqual(len(self.live_queries), 2)

    def test_every_duplicate_is_counted_before_display_limit(self):
        for i in range(3):
            self.add(f'p{i}', database=f'db{i}')
        result = self.run_search(limit=1)
        self.assertEqual(len(result['notion_pages']), 1)
        self.assertEqual(result['meta']['candidate_count'], 3)
        self.assertEqual(self.page_reads, [])
        self.assertEqual(len(self.live_queries), 3)

    def test_many_duplicates_in_one_database_share_one_current_query(self):
        rows = [page(f'p{i}', database='db0', title='배포 계획') for i in range(80)]
        self.index.upsert_catalog('db0', rows, complete=True)
        self.current.update({raw['id']: raw for raw in rows})
        result = self.run_search(limit=5)
        self.assertEqual(len(result['notion_pages']), 5)
        self.assertEqual(result['meta']['candidate_count'], 80)
        self.assertEqual(len(self.live_queries), 1)
        self.assertEqual(self.page_reads, [])

    def test_current_title_query_follows_every_cursor_before_counting(self):
        first = self.add()
        second = page('p2', database='db0', title='배포 계획')
        responses = []
        for raw, more in ((first, True), (second, False)):
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({'results': [raw], 'has_more': more,
                                            'next_cursor': 'next' if more else None}).encode()
            responses.append(response)
        payloads = []
        def query(_url, payload, **_kwargs):
            payloads.append(dict(payload))
            return responses.pop(0)
        with patch.object(search, 'notion_post_json', side_effect=query):
            result = self.run_search()
        self.assertEqual({row['id'] for row in result['notion_pages']}, {'p1', 'p2'})
        self.assertEqual(result['meta']['candidate_count'], 2)
        self.assertEqual(payloads[1]['start_cursor'], 'next')

    def test_repeated_current_query_cursor_falls_back_instead_of_claiming_completeness(self):
        self.add()
        original = self.query
        calls = 0
        def query(url, payload, **kwargs):
            nonlocal calls
            calls += 1
            response = original(url, payload, **kwargs)
            if calls <= 2:
                data = response.json()
                data.update(has_more=True, next_cursor='repeated')
                response._content = json.dumps(data).encode()
            return response
        with patch.object(search, 'notion_post_json', side_effect=query):
            result = self.run_search()
        self.assertNotEqual(result['meta'].get('coverage_basis'), 'api_properties')
        self.assertEqual([row['id'] for row in result['notion_pages']], ['p1'])

    def test_stale_incomplete_or_missing_catalog_never_proves_absence(self):
        self.add()
        for update in (
            "UPDATE catalogs SET updated_at='2020-01-01T00:00:00+00:00' WHERE database_id='db239'",
            "UPDATE catalogs SET complete=0 WHERE database_id='db239'",
            "DELETE FROM catalogs WHERE database_id='db239'",
        ):
            with self.subTest(update=update):
                self.index.upsert_catalog('db239', [], complete=True)
                with closing(sqlite3.connect(self.index.path)) as db, db:
                    db.execute(update)
                self.live_queries.clear()
                self.run_search()
                self.assertEqual(len(self.live_queries), 240)

    def test_revoked_or_moved_candidate_is_never_returned_from_catalog(self):
        self.add()
        for value in (_ReadFailure('http_403'), page('p1', database='outside', title='배포 계획')):
            with self.subTest(value=type(value).__name__):
                self.current['p1'] = value
                result = self.run_search()
                self.assertEqual(result['notion_pages'], [])

    def test_parent_catalog_refresh_does_not_renew_stale_child_metadata(self):
        parent = self.add(identifier='parent', title='다른 문서')
        child = page('child', title='배포 계획')
        child['parent'] = {'type': 'page_id', 'page_id': 'parent'}
        self.index.upsert_page('db0', child, parent_pages={'parent': parent})
        self.current['child'] = child
        with closing(sqlite3.connect(self.index.path)) as db, db:
            db.execute("UPDATE pages SET updated_at='2020-01-01T00:00:00+00:00' WHERE page_id='child'")
        result = self.run_search()
        self.assertNotEqual(result['meta'].get('coverage_basis'), 'api_properties')
        self.assertEqual(len(self.live_queries), 240)

    def test_truncated_title_cannot_prove_an_absent_title(self):
        raw = self.add(title='다른 문서')
        raw['properties']['제목']['title'] *= 25
        self.index.upsert_catalog('db0', [raw], complete=True)
        self.run_search()
        self.assertEqual(len(self.live_queries), 240)

    def test_requested_content_still_fetches_live_blocks_and_marks_partial_content(self):
        from backend.notion.client import NOTION_CONTENT_INCOMPLETE
        self.add()
        with patch.object(search, 'fetch_notion_page_blocks', return_value='현재 본문\n' + NOTION_CONTENT_INCOMPLETE):
            result = search.execute_notion_search("노션에서 '배포 계획' 문서 보여줘", include_blocks=True)
        self.assertIn('현재 본문', result['notion_pages'][0]['content'])
        self.assertFalse(result['meta']['content_complete'])
        self.assertEqual(result['meta']['content_incomplete_page_ids'], ['p1'])

    def test_body_conditions_keep_the_body_validation_path(self):
        self.add()
        with patch('backend.notion.client.fetch_notion_page_content', return_value={'text': '다른 본문', 'segments': []}):
            result = self.run_search('노션에서 "배포 계획" 문서 중 본문에 "needle" 포함한 문서 찾아줘')
        self.assertNotEqual(result['meta'].get('coverage_basis'), 'api_properties')
        self.assertEqual(result['notion_pages'], [])

    def test_answer_scope_describes_properties_without_claiming_body_coverage(self):
        from backend.evaluation.data_scope import build_answer_data_scope
        scope = build_answer_data_scope({'stages': [{'source': 'notion', 'status': 'ok', 'meta': {
            'status': 'snapshot', 'coverage_basis': 'api_properties', 'search_complete': True,
            'snapshot_as_of': '2026-10-22T00:00:00+00:00',
        }}]})[0]
        self.assertEqual(scope['basis'], '저장본')
        self.assertTrue(any('제목·속성' in note for note in scope['notes']))
        self.assertFalse(any('본문 전체' in note for note in scope['notes']))
