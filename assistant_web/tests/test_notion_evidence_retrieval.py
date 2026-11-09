"""읽은 구간과 순번 상세의 조건을 보존하는 검색 회귀."""
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from test_tool_condition_arguments import arguments, context_for, scanned


class EvidenceRetrievalTests(unittest.TestCase):
    def test_final_serialization_keeps_continuation_adjacent_to_the_delivered_body(self):
        from backend.tools.registry import fit_result
        result = {'ok': True, 'tool': 'get_notion_page_content', 'results': [{
            'ref': 'N1', 'content': '긴 원문 ' * 1000, 'content_offset': 200, 'next_offset': None}]}
        sent = json.loads(fit_result(result, 900))
        row = sent['results'][0]
        self.assertTrue(sent['truncated'])
        self.assertEqual(row['next_offset'], 200 + len(row['content']))

    def test_source_request_limit_stays_incomplete_and_reaches_the_tool_result(self):
        from backend.notion import semantic_search as search
        from backend.tools.notion_tools import _page_view
        from unittest.mock import patch
        current = {'raw_page': {'id': 'page'}, 'database_id': 'db',
                   'segments': [{'text': '확인된 일부 내용', 'page_id': 'page', 'owner_page_id': 'page'}],
                   'incomplete_reasons': ['request_limit', 'unsupported_block'], 'accessible_content_complete': False}
        with (patch.object(search, 'validate_index_candidate', return_value=current),
              patch.object(search, 'open_body_index', return_value=None),
              patch.object(search, 'normalize_scoped_page', return_value={'id': 'page'}),
              patch.object(search, 'notion_page_matches_and_enrich', return_value=True)):
            page = search.read_current_page({'page_id': 'page', 'database_id': 'db'}, {'db': 'db'}, {}, deadline=100)
        view = _page_view(context_for(), page)
        self.assertFalse(view['content_complete'])
        self.assertEqual(view['content_incomplete_reasons'], ['request_limit', 'unsupported_block'])

    def test_offset_read_reports_omitted_prefix_and_continues_without_refetch(self):
        from backend.tools.catalog import agent_registry
        from backend.tools.registry import fit_result
        ctx = context_for(); ctx.max_result_chars = 1400
        body = '앞부분 설명\n' * 100 + '관련 절차를 선택합니다.' + '뒤쪽 설명\n' * 200
        ctx.ledger.add('notion', {'status': 'ok', 'notion_pages': [
            {'id': 'page', 'title': '기능 가이드', 'content': body, '_verified_body': True,
             'content_incomplete': False, 'semantic_excerpt': '관련 절차를 선택합니다.'}],
            'meta': {'search_complete': False}}, grade='reference', unapplied=(), call_id='search')
        registry = agent_registry(query_tools=True)
        with patch('backend.tools.notion_tools.read_current_page') as fetch:
            first = json.loads(fit_result(registry.execute('get_notion_page_content', {'page_ref': 'N1'}, '', ctx), 1400))
            self.assertGreater(first['results'][0]['content_offset'], 0)
            self.assertTrue(first['truncated'])
            offset = first['results'][0]['next_offset']
            second = registry.execute('get_notion_page_content', {'page_ref': 'N1', 'offset': offset}, '', ctx)
            self.assertTrue(second['ok'], second)
            self.assertEqual(second['results'][0]['content_offset'], offset)
            self.assertEqual(second['results'][0]['content'], body[offset:offset + len(second['results'][0]['content'])])
            prefix = registry.execute('get_notion_page_content', {'page_ref': 'N1', 'offset': 0}, '', ctx)
            self.assertEqual(prefix['results'][0]['content_offset'], 0)
            invalid = registry.execute('get_notion_page_content', {'page_ref': 'N1', 'offset': len(body) + 1}, '', ctx)
            self.assertFalse(invalid['ok'])
        fetch.assert_not_called()

    def test_short_tail_is_truncated_even_if_total_body_fits_limit(self):
        from backend.tools.catalog import agent_registry
        ctx = context_for()
        body = '도입부' * 300 + '설정 메뉴를 선택합니다.'
        ctx.ledger.add('notion', {'status': 'ok', 'notion_pages': [
            {'id': 'page', 'title': '기능 가이드', 'content': body, '_verified_body': True,
             'content_incomplete': False, 'semantic_excerpt': '설정 메뉴를 선택합니다.'}],
            'meta': {'search_complete': False}}, grade='reference', unapplied=(), call_id='search')
        result = agent_registry().execute('get_notion_page_content', {'page_ref': 'N1'}, '', ctx)
        self.assertTrue(result['truncated'])
        self.assertIsNone(result['results'][0]['next_offset'])

    def test_selected_detail_hint_preserves_filters_and_cannot_authorize_changes(self):
        from backend.tools.catalog import agent_registry
        from backend.tools.mantis_tools import MantisDetailArgs
        from backend.tools.step_conditions import bind_step, read_target_matches
        ctx = context_for(); ctx.related_ordinal = 2
        registry = agent_registry(query_tools=True)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()):
            result = registry.execute('list_mantis_issues', {**arguments(ctx), 'limit': 3}, '', ctx)
        hint = result['selected_issue_detail']
        self.assertEqual(hint['tool'], 'get_mantis_issue_detail')
        self.assertEqual(hint['arguments']['issue_id'], 43)
        parsed = MantisDetailArgs(**hint['arguments'])
        contract, bound, _ = bind_step(ctx, 'mantis', parsed, explicit_required=False, list_limit=None)
        self.assertTrue(read_target_matches(parsed, bound))
        self.assertEqual(contract.purpose, 'investigate')
        self.assertEqual(contract.filters[:-1], ctx.query_contracts['mantis'].filters)
        for change in ('drop_status', 'wrong_target', 'foreign_issue'):
            args = deepcopy(hint['arguments'])
            if change == 'drop_status':
                args['filters'] = [f for f in args['filters'] if f['field'] != 'status']
            else:
                target = 44 if change == 'wrong_target' else 999
                args['issue_id'] = target
                args['filters'][-1].update(value=str(target), evidence_ref=f'M#{target}')
            with self.subTest(change=change), self.assertRaises(ValueError):
                bind_step(ctx, 'mantis', MantisDetailArgs(**args), explicit_required=False, list_limit=None)

    def test_lexical_candidates_need_specific_terms_instead_of_generic_screen_or_data(self):
        from backend.notion.body_index import BodyIndex
        from backend.notion.retrieval_candidates import hybrid_candidates
        from test_notion_guide_recovery import page
        with tempfile.TemporaryDirectory() as directory:
            index = BodyIndex(Path(directory) / 'body.sqlite3', fingerprint='test')
            for identifier, title in [('noise', '화면 데이터 사용 방법'), ('topic', '월주기 저가 검증 방법')]:
                index.upsert_page('db', page(identifier, database='db', title=title))
            found = hybrid_candidates('화면 데이터 월주기 저가 사용 방법', ['db'], semantic=[], index=index)
        self.assertEqual([r['page_id'] for r in found], ['topic'])

    def test_explicit_screen_number_remains_a_search_term(self):
        from backend.notion.body_index import BodyIndex
        from backend.notion.retrieval_candidates import hybrid_candidates
        from test_notion_guide_recovery import page
        with tempfile.TemporaryDirectory() as directory:
            index = BodyIndex(Path(directory) / 'body.sqlite3', fingerprint='test')
            index.upsert_page('db', page('screen', database='db', title='[0320] 사용 방법'))
            found = hybrid_candidates('[0320] 가이드', ['db'], semantic=[], index=index)
        self.assertEqual([r['page_id'] for r in found], ['screen'])


if __name__ == '__main__':
    unittest.main()
