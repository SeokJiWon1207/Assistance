"""최근 등록 목록은 본문 수집과 무관하게 속성으로 정렬·검증한다."""
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
from backend.retrieval import notion_question_needs_blocks
from test_notion_body_index import page


QUESTION = '노션에서 최근에 등록된 문서는 뭐야'


class RecentRegistrationTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.index = BodyIndex(Path(directory) / 'index.sqlite3', fingerprint='connection')
        self.scope = {f'db{i}': f'db{i}' for i in range(3)}
        for identifier in self.scope:
            self.index.upsert_catalog(identifier, [], complete=True)
        self.current = {}
        self.queries = []
        self.reads = []
        self.schema = {'제목': {'type': 'title'}}
        self.enterContext(patch.object(complete_snapshot, 'open_body_index', return_value=self.index))
        self.enterContext(patch.object(complete_snapshot, '_scope_current', return_value=True))
        self.enterContext(patch.object(search, 'get_notion_settings', return_value={
            'token': 'fixture-token', 'database_ids': list(self.scope)}))
        self.enterContext(patch.object(search, 'resolve_notion_read_scope', return_value=(self.scope, True)))
        self.enterContext(patch.object(search, 'fetch_notion_database_title', return_value='업무'))
        self.enterContext(patch.object(search, 'notion_property_schema', side_effect=lambda _db: self.schema))
        self.enterContext(patch.object(search, 'notion_post_json', side_effect=self.query))
        self.enterContext(patch('backend.notion.body_index_sync._request', side_effect=self.read))
        self.enterContext(patch.dict('os.environ', {'NOTION_TITLE_QUERY_CACHE_SECONDS': '0'}))

    def add(self, identifier, created, edited, *, database='db0', registered=None, title='업무 문서'):
        raw = page(identifier, revision=edited, database=database, title=title)
        raw['created_time'] = created
        if registered is not None:
            raw['properties']['등록일'] = {'type': 'date', 'date': {'start': registered}}
        # DB 전체 목록을 교체할 때 기존 문서도 포함한다.
        self.current[identifier] = raw
        rows = [r for r in self.current.values() if isinstance(r, dict)
                and r['parent'].get('database_id') == database]
        self.index.upsert_catalog(database, rows, complete=True)

    def read(self, endpoint, *_args, **_kwargs):
        self.reads.append(endpoint)
        if not endpoint.startswith('pages/') or endpoint.count('/') != 1:
            raise AssertionError(f'본문을 읽으면 안 됩니다: {endpoint}')
        value = self.current.get(endpoint.split('/')[1])
        if isinstance(value, Exception):
            raise value
        return deepcopy(value or {})

    def query(self, url, payload, **_kwargs):
        self.queries.append(deepcopy(payload))
        database = url.split('/databases/')[1].split('/')[0]
        rows = [deepcopy(r) for r in self.current.values() if isinstance(r, dict)
                and r['parent'].get('database_id') == database]
        if 'title' in (payload.get('filter') or {}):
            condition = payload['filter']['title']
            value = next(iter(condition.values()))
            rows = [r for r in rows if value in r['properties']['제목']['title'][0]['plain_text']]
        sort = payload['sorts'][0]
        rows.sort(key=lambda r: (r['properties'].get(sort.get('property'), {}).get('date') or {}).get('start', '')
                  if sort.get('property') else r.get(sort['timestamp'], ''),
                  reverse=sort['direction'] == 'descending')
        count = payload['page_size']
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({'results': rows[:count], 'has_more': len(rows) > count,
                                        'next_cursor': 'more' if len(rows) > count else None}).encode()
        return response

    def run_search(self, question=QUESTION, **kwargs):
        return search.execute_notion_search(question, include_blocks=notion_question_needs_blocks(question), **kwargs)

    def test_recent_registration_words_are_not_subject_keywords(self):
        for question in (QUESTION, '노션 최근 생성된 문서 보여줘', '노션 문서 등록순으로 보여줘',
                         '노션 최근 등록 문서 보여줘'):
            with self.subTest(question=question):
                constraints = search.extract_notion_constraints(question)
                self.assertEqual(search.build_notion_terms(question, constraints), [])
        self.assertIn('등록오류', search.build_notion_terms('노션 최근에 등록된 등록오류 문서 찾아줘',
                      search.extract_notion_constraints('노션 최근에 등록된 등록오류 문서 찾아줘')))
        self.assertIn('등록', search.build_notion_terms('사용자 등록 방법 문서'))
        title = "노션에서 '최근에 등록된 문서' 찾아줘"
        self.assertEqual(search.extract_notion_constraints(title)['exact_title'], '최근에 등록된 문서')

    def test_registration_subject_is_preserved_next_to_recent_registration_request(self):
        question = '노션 최근에 등록된 사용자 등록 방법 문서 찾아줘'
        terms = search.build_notion_terms(question, search.extract_notion_constraints(question))
        self.assertIn('등록', terms)
        self.assertNotIn('등록된', terms)

    def test_answer_shows_registration_order_date_and_catalog_time(self):
        from backend.notion.formatting import format_deterministic_notion_answer
        self.add('new', '2026-10-26T00:00:00Z', '2026-10-27T00:00:00Z')
        result = self.run_search()
        answer = format_deterministic_notion_answer(QUESTION, {'stages': [result]}, 5)
        self.assertIn('최근 등록순', answer)
        self.assertIn('등록 2026-10-26', answer)
        self.assertIn('목록 기준 시각', answer)
        self.assertNotIn('제목 일치 후보', answer)
        from backend.evaluation.data_scope import build_answer_data_scope
        notes = build_answer_data_scope({'stages': [result]})[0]['notes']
        self.assertFalse(any('제목 일치 후보' in note for note in notes))

    def test_empty_catalog_answer_discloses_snapshot_time(self):
        from backend.notion.formatting import format_deterministic_notion_answer
        result = self.run_search()
        answer = format_deterministic_notion_answer(QUESTION, {'stages': [result]}, 5)
        self.assertIn('목록 기준 시각', answer)
        self.assertEqual(self.queries, [])

    def test_recent_list_uses_metadata_even_with_240_unread_databases(self):
        for i in range(3, 240):
            self.scope[f'db{i}'] = f'db{i}'
            self.index.upsert_catalog(f'db{i}', [], complete=True)
        self.add('old', '2026-10-01T00:00:00Z', '2026-10-27T00:00:00Z')
        self.add('new', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z', database='db239')
        with patch.object(complete_snapshot, '_state', side_effect=AssertionError('본문 완전성 검사 금지')):
            result = self.run_search(limit=1, planned_terms=['등록된'])
        self.assertEqual([r['id'] for r in result['notion_pages']], ['new'])
        self.assertEqual(result['meta']['coverage_basis'], 'api_properties')
        self.assertFalse(result['meta']['body_index_used'])
        self.assertIsNone(result['meta']['content_complete'])
        self.assertTrue(result['meta']['search_complete'])
        self.assertEqual(self.queries, [])
        self.assertEqual(self.reads, ['pages/new'])

    def test_registration_property_and_same_day_time_take_precedence_over_creation(self):
        self.add('earlier', '2026-10-27T03:00:00Z', '2026-10-27T04:00:00Z', registered='2026-10-25T09:00:00+09:00')
        self.add('later', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', registered='2026-10-25T18:00:00+09:00')
        result = self.run_search(limit=2)
        self.assertEqual([r['id'] for r in result['notion_pages']], ['later', 'earlier'])

    def test_date_only_registration_is_kst_midnight(self):
        self.add('dateonly', '2026-10-27T03:00:00Z', '2026-10-27T04:00:00Z', registered='2026-10-25')
        self.add('timed', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', registered='2026-10-25T01:00:00+09:00')
        result = self.run_search(limit=2)
        self.assertEqual([r['id'] for r in result['notion_pages']], ['timed', 'dateonly'])

    def test_unified_retrieval_with_empty_model_terms_keeps_recent_list_fast(self):
        from backend.retrieval import run_retrieval_pipeline
        from backend.intent.unified_validation import validate_plan
        self.add('new', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z')
        plan = validate_plan(QUESTION, {
            'source': 'notion', 'operation': 'search', 'scope': 'team', 'followup': False,
            'filters': [], 'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
            'clear_fields': [], 'terms': [], 'group_by': None, 'limit': None,
        }, {}, {})
        result = run_retrieval_pipeline(QUESTION, 5, {'notion': True}, intent_plan=plan)
        self.assertEqual([r['id'] for r in result['stages'][0]['notion_pages']], ['new'])
        self.assertEqual(result['stages'][0]['meta']['tool'], 'notion_recent_catalog')
        self.assertEqual(self.queries, [])

    def test_created_time_fallback_respects_kst_boundary(self):
        self.add('before', '2026-10-26T14:59:00Z', '2026-10-27T08:00:00Z')
        self.add('after', '2026-10-26T15:01:00Z', '2026-10-27T00:00:00Z')
        result = self.run_search(constraints={'filter_clauses': [
            {'field': 'created_at', 'operator': 'equals', 'value': '2026-10-27'}]})
        self.assertEqual([r['id'] for r in result['notion_pages']], ['after'])

    def test_disabling_body_fetch_preserves_recent_catalog_path(self):
        self.add('new', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z')
        with patch.dict('os.environ', {'NOTION_FETCH_PAGE_BLOCKS': 'false'}):
            result = self.run_search()
        self.assertEqual([r['id'] for r in result['notion_pages']], ['new'])
        self.assertEqual(self.queries, [])

    def test_unrelated_truncated_title_does_not_block_date_only_selection(self):
        self.add('old', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')
        self.add('new', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z')
        self.current['old']['properties']['제목']['has_more'] = True
        self.index.upsert_catalog('db0', list(self.current.values()), complete=True)
        result = self.run_search(limit=1)
        self.assertEqual([r['id'] for r in result['notion_pages']], ['new'])
        self.assertEqual(self.queries, [])

    def test_unusable_catalog_falls_back_to_one_created_sorted_window_per_database(self):
        self.add('old', '2026-10-01T00:00:00Z', '2026-10-27T00:00:00Z')
        self.add('new', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z')
        for update in ("UPDATE catalogs SET complete=0", "UPDATE catalogs SET updated_at='2020-01-01T00:00:00Z'"):
            with self.subTest(update=update):
                with closing(sqlite3.connect(self.index.path)) as db, db:
                    db.execute(update)
                self.queries.clear()
                result = self.run_search(limit=1)
                self.assertEqual([r['id'] for r in result['notion_pages']], ['new'])
                self.assertEqual(len(self.queries), 3)
                self.assertTrue(all(p['page_size'] == 1 for p in self.queries))
                self.assertTrue(all(p['sorts'] == [{'timestamp': 'created_time', 'direction': 'descending'}]
                                    for p in self.queries))
                self.assertTrue(result['meta']['result_window_complete'])
                self.assertFalse(result['meta']['candidate_count_complete'])

    def test_api_fallback_uses_registration_property_sort(self):
        self.schema['등록일'] = {'type': 'date'}
        self.add('registered', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z', registered='2026-10-27')
        with patch.object(complete_snapshot, 'open_body_index', return_value=None):
            result = self.run_search(limit=1)
        self.assertEqual([r['id'] for r in result['notion_pages']], ['registered'])
        self.assertTrue(all(p['sorts'] == [{'property': '등록일', 'direction': 'descending'}] for p in self.queries))

    def test_revoked_document_is_not_returned_from_metadata(self):
        self.add('revoked', '2026-10-27T00:00:00Z', '2026-10-27T00:00:00Z')
        self.current['revoked'] = _ReadFailure('http_403')
        result = self.run_search()
        self.assertEqual(result['notion_pages'], [])

    def test_body_summary_does_not_take_metadata_list_shortcut(self):
        self.assertTrue(notion_question_needs_blocks('노션 최근 등록된 문서 내용 요약해줘'))
        self.assertFalse(notion_question_needs_blocks(QUESTION))

    def test_changed_registration_date_invalidates_cached_top_window(self):
        self.add('new', '2026-10-27T00:00:00Z', '2026-10-27T00:00:00Z')
        self.add('other', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z')
        self.current['new']['created_time'] = '2026-10-01T00:00:00Z'
        result = self.run_search(limit=1)
        self.assertEqual([r['id'] for r in result['notion_pages']], ['other'])
        self.assertNotEqual(result['meta'].get('tool'), 'notion_recent_catalog')

    def test_stale_child_metadata_does_not_inherit_fresh_parent_catalog(self):
        self.add('parent', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z')
        child = page('child', title='자식 문서', database='db0')
        child['created_time'] = '2026-10-27T00:00:00Z'
        child['parent'] = {'type': 'page_id', 'page_id': 'parent'}
        self.index.upsert_page('db0', child, parent_pages={'parent': self.current['parent']})
        self.current['child'] = child
        with closing(sqlite3.connect(self.index.path)) as db, db:
            db.execute("UPDATE pages SET updated_at='2020-01-01T00:00:00Z' WHERE page_id='child'")
        result = self.run_search()
        self.assertNotEqual(result['meta'].get('tool'), 'notion_recent_catalog')

    def test_body_keyword_summary_and_count_do_not_use_metadata_only_list(self):
        self.add('new', '2026-10-26T00:00:00Z', '2026-10-26T00:00:00Z', title='배포 계획')
        questions = ('노션 최근 등록된 배포 문서 찾아줘', '노션 최근 등록된 문서 내용 요약해줘',
                     '노션 최근 등록된 문서는 몇 개야', '노션 최근 등록된 문서 중 본문에 "등록된" 포함한 문서 찾아줘')
        with patch.object(complete_snapshot, '_search_snapshot_once', return_value=None), patch.object(
            search, 'find_indexed_body_candidates', return_value=None
        ), patch.object(search, 'fetch_notion_page_blocks', return_value='등록된 배포 내용'), patch(
            'backend.notion.recent_catalog.search_recent_catalog', side_effect=AssertionError('목록 전용 경로')
        ):
            for question in questions:
                with self.subTest(question=question):
                    result = self.run_search(question)
                    self.assertNotEqual(result['meta'].get('tool'), 'notion_recent_catalog')

