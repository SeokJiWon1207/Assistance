"""검증된 제목·속성 목록은 본문 수집과 독립적으로 빠르게 검색한다."""
from backend.notion import read_api
from backend.notion import transport
from backend.notion import schema as notion_schema
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
        self.enterContext(patch.object(search, 'resolve_notion_settings', return_value={
            'token': 'fixture-token', 'database_ids': list(self.scope)}))
        self.enterContext(patch.object(search, 'resolve_notion_read_scope', return_value=(self.scope, True)))
        self.enterContext(patch.object(read_api, 'fetch_notion_database_title', return_value='업무'))
        self.enterContext(patch.object(notion_schema, 'notion_property_schema', return_value={'제목': {'type': 'title'}}))
        self.enterContext(patch.object(transport, 'notion_post_json', side_effect=self.query))
        self.enterContext(patch('backend.notion.crawl_transport.request_json', side_effect=self.read_page))

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

    def test_customer_topic_list_revalidates_title_hits_before_spending_time_on_bodies(self):
        self.add('verified', title='[한투] 조건검색 검증용 문서(서버)', database='db0')
        self.add('wrong', title='[신한투자증권] 조건검색 서버', database='db1')
        self.add('renamed', title='한투 서버 조건검색', database='db2')
        self.current['renamed']['properties']['제목']['title'][0]['plain_text'] = '한투 사내 일정'
        with patch.object(search, 'find_indexed_body_candidates') as bodies:
            result = self.run_search('한투에서 조건검색 서버 관련 문서 찾아줘', limit=1,
                planned_terms=['조건검색 서버'], constraints={'filter_clauses': [
                    {'field': 'customer', 'operator': 'equals', 'value': '한투'}]})
        self.assertEqual(result['meta']['terms'], ['조건검색', '서버'])
        self.assertEqual([row['id'] for row in result['notion_pages']], ['verified'])
        self.assertFalse(result['meta']['candidate_count_complete'])
        self.assertTrue(result['meta']['title_search_only'])
        self.assertLessEqual(len(self.live_queries), 3)
        bodies.assert_not_called()

    def test_underfilled_customer_title_results_are_kept_while_searching_properties(self):
        self.scope.clear()
        self.scope.update({'db0': 'db0', 'db1': 'db1'})
        title = self.add('title', title='한투 서버의 조건검색 개선', database='db0')
        other = self.add('property', title='한투 운영 기록', database='db1')
        other['properties']['메모'] = {'type': 'rich_text', 'rich_text': [{'plain_text': '조건검색 서버'}]}

        def query(url, payload, **_kwargs):
            if '/db0/' in url:
                rows = [title]
            else:
                rows = [] if 'filter' in payload else [other]
            result = requests.Response()
            result.status_code = 200
            result._content = json.dumps({'results': rows, 'has_more': False}).encode()
            return result
        with (patch.object(transport, 'notion_post_json', side_effect=query),
              patch.object(search, 'find_indexed_body_candidates', return_value=None)):
            result = self.run_search('한투에서 조건검색 서버 관련 문서 찾아줘', limit=2,
                planned_terms=['조건검색', '서버'], constraints={'filter_clauses': [
                    {'field': 'customer', 'operator': 'equals', 'value': '한투'}]})
        self.assertEqual([row['id'] for row in result['notion_pages']], ['title', 'property'])

    def test_related_issue_search_can_open_an_underfilled_title_window_without_proving_absence(self):
        self.add('ticket', title='한투 비교차트 업데이트 현상')
        with patch.object(search, 'find_indexed_body_candidates', side_effect=AssertionError('전수 검색')) as scan:
            result = self.run_search('비교차트 업데이트', limit=5, planned_terms=['비교차트', '업데이트'],
                constraints={'filter_clauses': [{'field': 'customer', 'operator': 'equals', 'value': '한투'}]},
                derive_text_constraints=False, prefer_catalog_titles=True)
        self.assertEqual([p['id'] for p in result['notion_pages']], ['ticket'])
        self.assertFalse(result['meta']['search_complete'])
        self.assertFalse(result['meta']['candidate_count_complete'])
        self.assertFalse(result['meta']['result_window_complete'])
        scan.assert_not_called()

    def test_guide_rechecks_current_content_ownership_customer_and_excludes_the_opened_page(self):
        from backend.notion import related_guides as guides
        from test_notion_body_index import segment
        for pid, title in [('opened', 'NH 덤프파일 기록'), ('wrong', 'NH 덤프파일 가이드'),
                           ('moved', 'NH 덤프파일 매뉴얼'), ('valid', 'NH 덤프파일 확인 방법')]:
            self.add(pid, title=title, database='db' + str(len(self.current)))
        expected = '덤프 파일을 분석하려면 메모리 창을 선택합니다.'
        checked = []

        def current(entry, allowed, **kw):
            pid = entry['page_id']
            checked.append(pid)
            self.assertIn(entry['database_id'], allowed)
            self.assertGreaterEqual(kw['limits'].max_seconds, 20)
            if pid == 'moved':
                return None
            raw = deepcopy(self.current[pid])
            raw['properties']['고객사'] = {'type': 'select', 'select': {'name': '신한' if pid == 'wrong' else 'NH'}}
            return {'raw_page': raw, 'accessible_content_complete': True,
                    'segments': [segment('다른 페이지의 비공개 지침', owner='other'), segment(expected, owner=pid)]}

        with (patch.object(guides, 'resolve_notion_settings', return_value={'database_ids': list(self.scope), 'token': 't'}),
              patch.object(guides, 'resolve_notion_read_scope', return_value=(self.scope, True)),
              patch.object(guides, 'open_body_index', return_value=self.index),
              patch.object(guides, 'fetch_notion_database_title', return_value='업무'),
              patch.object(guides, 'validate_index_candidate', side_effect=current)):
            page, excerpt, report = guides.find_alternate_guide(['덤프파일'], {'filter_clauses': [
                {'field': 'customer', 'operator': 'equals', 'value': 'NH'}]}, {'opened'}, {})
            self.assertEqual((page['id'], excerpt, report['outcome']), ('valid', expected, 'found'))
            self.assertEqual(page['content'], expected)
            self.assertEqual(report['candidate_count'], 3)
            self.assertNotIn('opened', checked)
            found, _, negative = guides.find_alternate_guide(['덤프파일'], {'filter_clauses': [
                {'field': 'customer', 'operator': 'equals', 'value': 'NH'}]}, {'opened', 'valid'}, {})
            self.assertIsNone(found)
            self.assertEqual(negative['checked_count'], 2)

    def test_guide_body_requires_a_topic_and_instructions_with_body_reads_enabled(self):
        from backend.notion import related_guides as guides
        self.assertEqual(guides.topic_terms('비교차트 실시간 업데이트 오류', ['실시간', '업데이트', '비교차트']), ['비교차트'])
        for title, body in [('비교차트 사용법', ''), ('비교차트 사용법', '비교차트 사용법'),
                            ('iOS 가이드 요청', '비교차트 업데이트를 요청합니다.'),
                            ('다른 기능 가이드', '비교차트 이슈를 접수했습니다.\n다른 기능은 설치합니다.')]:
            self.assertFalse(guides.guide_passage(title, body, ['비교차트'])[1])
        with (patch.dict('os.environ', {'NOTION_FETCH_PAGE_BLOCKS': 'false'}),
              patch.object(guides, 'resolve_notion_settings') as settings):
            page, _, report = guides.find_alternate_guide(['비교차트'], {}, set(), {})
            self.assertIsNone(page)
            self.assertEqual(report['outcome'], 'body_disabled')
            settings.assert_not_called()

    def test_topic_title_with_different_spacing_is_checked_on_the_current_page(self):
        self.add('spaced', title='[한투] 조건 검색 문서 서버', database='db0')
        empty = requests.Response()
        empty.status_code = 200
        empty._content = b'{"results": [], "has_more": false}'
        with (patch.object(transport, 'notion_post_json', return_value=empty),
              patch.object(search, 'find_indexed_body_candidates', return_value=None)):
            result = self.run_search('한투에서 조건검색 서버 관련 문서 찾아줘', limit=1,
                planned_terms=['조건검색', '서버'], constraints={'filter_clauses': [
                    {'field': 'customer', 'operator': 'equals', 'value': '한투'}]})
        self.assertEqual([row['id'] for row in result['notion_pages']], ['spaced'])
        self.assertEqual(self.page_reads, ['spaced'])

    def test_partial_title_verification_survives_a_subsequent_live_timeout(self):
        from backend.execution import CURRENT_BUDGET, checkpoint

        self.scope.clear()
        self.scope.update({'db0': 'db0', 'db1': 'db1'})
        self.add('verified', title='한투 서버 조건검색', database='db0')
        self.add('unavailable', title='한투 조건검색 서버', database='db1')
        self.current['unavailable'] = _ReadFailure('http_503')
        attempts = []

        def query(url, payload, **kwargs):
            attempts.append(url)
            if len(attempts) == 1:
                return self.query(url, payload, **kwargs)
            if len(attempts) == 2:
                raise requests.ConnectionError('second catalog database unavailable')
            CURRENT_BUDGET.get().deadline = 0
            checkpoint()
        with (patch.object(transport, 'notion_post_json', side_effect=query),
              patch.dict('os.environ', {'NOTION_SEARCH_MAX_WORKERS': '1'}),
              patch.object(search, 'find_indexed_body_candidates', return_value=None)):
            result = self.run_search('한투에서 조건검색 서버 관련 문서 찾아줘', limit=2,
                planned_terms=['조건검색', '서버'], constraints={'filter_clauses': [
                    {'field': 'customer', 'operator': 'equals', 'value': '한투'}]})
        self.assertEqual([row['id'] for row in result['notion_pages']], ['verified'])
        self.assertFalse(result['meta']['search_complete'])
        self.assertIn('live_search_time_limit', result['meta']['incomplete_reasons'])

    def test_rate_limited_database_query_uses_current_page_ids_from_fresh_catalog(self):
        from backend.execution import CURRENT_BUDGET, checkpoint

        self.add('one', title='한투 조건검색 서버', database='db0')
        self.add('two', title='한투 서버와 조건검색', database='db1')
        self.add('revoked', title='한투 조건검색 서버 예전 자료', database='db2')
        self.current['revoked'] = _ReadFailure('http_403')

        def timeout(*_args, **_kwargs):
            CURRENT_BUDGET.get().deadline = 0
            checkpoint()

        with (patch.object(notion_schema, 'notion_property_schema', return_value={}),
              patch.object(transport, 'notion_post_json', side_effect=timeout),
              patch.object(search, 'find_indexed_body_candidates', return_value=None)):
            result = self.run_search('한투에서 조건검색 서버 관련 문서 찾아줘', limit=2,
                planned_terms=['조건검색', '서버'], constraints={'filter_clauses': [
                    {'field': 'customer', 'operator': 'equals', 'value': '한투'}]})
        self.assertEqual({p['id'] for p in result['notion_pages']}, {'one', 'two'})
        self.assertEqual(set(self.page_reads), {'one', 'two', 'revoked'})
        self.assertFalse(result['meta']['search_complete'])
        self.assertTrue(result['meta']['result_window_complete'])

    def test_expired_customer_title_hints_require_live_access_and_survive_timed_out_scan(self):
        from backend.execution import CURRENT_BUDGET, checkpoint

        self.add('readable', title='한투 조건검색 검증용 서버', database='db0')
        self.add('renamed', title='한투 서버 조건검색', database='db1')
        self.add('revoked', title='한투 조건검색 서버 이력', database='db2')
        self.add('temporary', title='한투 조건검색 서버 작업', database='db3')
        self.add('wrong-id', title='한투 조건검색 서버 검증', database='db4')
        self.current['renamed']['properties']['제목']['title'][0]['plain_text'] = '한투 다른 문서'
        self.current['revoked'] = _ReadFailure('http_403')
        self.current['temporary'] = _ReadFailure('http_503')
        self.current['wrong-id']['id'] = 'other-page'
        with closing(sqlite3.connect(self.index.path)) as db, db:
            db.execute("UPDATE catalogs SET updated_at='2020-01-01T00:00:00+00:00'")

        def timeout(*_args, **_kwargs):
            CURRENT_BUDGET.get().deadline = 0
            checkpoint()

        with (patch.object(transport, 'notion_post_json', side_effect=timeout),
              patch.object(search, 'find_indexed_body_candidates', return_value=None)):
            result = self.run_search('한투에서 조건검색 서버 관련 문서 찾아줘', limit=5,
                planned_terms=['조건검색 서버'], constraints={'filter_clauses': [
                    {'field': 'customer', 'operator': 'equals', 'value': '한투'}]})
        self.assertEqual([p['id'] for p in result['notion_pages']], ['readable'])
        self.assertEqual(set(self.page_reads), {'readable', 'renamed', 'revoked', 'temporary', 'wrong-id'})
        self.assertEqual(result['meta']['revalidated_title_hint_count'], 1)
        self.assertFalse(result['meta']['search_complete'])
        self.assertIn('live_search_time_limit', result['meta']['incomplete_reasons'])

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
        with patch.object(transport, 'notion_post_json', side_effect=query):
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
        with patch.object(transport, 'notion_post_json', side_effect=query):
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

    def test_only_an_unusable_catalog_requests_background_recovery(self):
        from backend.notion import cache_maintenance

        self.add()
        with patch.object(cache_maintenance, 'request_catalog_refresh', create=True) as request:
            self.run_search()
            request.assert_not_called()
            with closing(sqlite3.connect(self.index.path)) as db, db:
                db.execute("UPDATE catalogs SET updated_at='2020-01-01T00:00:00+00:00'")
            self.run_search()
            request.assert_called_once_with()

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
        with patch.object(read_api, 'fetch_notion_page_blocks', return_value='현재 본문\n' + NOTION_CONTENT_INCOMPLETE):
            result = search.execute_notion_search("노션에서 '배포 계획' 문서 보여줘", include_blocks=True)
        self.assertIn('현재 본문', result['notion_pages'][0]['content'])
        self.assertFalse(result['meta']['content_complete'])
        self.assertEqual(result['meta']['content_incomplete_page_ids'], ['p1'])

    def test_body_conditions_keep_the_body_validation_path(self):
        self.add()
        with patch('backend.notion.read_api.fetch_notion_page_content', return_value={'text': '다른 본문', 'segments': []}):
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
