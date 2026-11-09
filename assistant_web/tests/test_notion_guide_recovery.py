"""본문 후보 누락과 429 이후의 탐색 복구를 결과로 검사한다."""
import os
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from test_notion_body_index import page, segment
from test_tool_condition_arguments import context_for


class NotionGuideRecoveryTests(unittest.TestCase):
    def test_generic_stock_words_do_not_turn_an_unrelated_fix_record_into_the_requested_guide(self):
        from backend.notion.related_guides import topic_terms, guide_passage
        terms = topic_terms('[MTS] 비교차트 내 추가된 종목의 실시간 업데이트 되지 않는 현상',
                            ['MTS','비교차트','추가된','종목','실시간','업데이트','되지','않는'])
        wrong = '종목 실시간 등록이 되지 않는 현상\n종목 처리 방법\n다른 방법을 선택하여 Map에 종목 코드를 등록해야 합니다.'
        self.assertFalse(guide_passage('맵 차트 CPU 문제', wrong, terms)[1])
        self.assertTrue(guide_passage('비교차트 사용 방법', '비교차트에서 종목 추가 버튼을 선택합니다.', terms)[1])

    def test_shared_cooldown_survives_a_different_process_and_connection_isolation(self):
        import json
        import subprocess
        import sys
        from backend.notion.rate_state import shared_delay
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'NOTION_SHARED_RATE_LIMIT_ENABLED':'true', 'NOTION_RATE_STATE_DIR':directory,
                'NOTION_API_TOKEN':'unit-connection-a'}):
            subprocess.run([sys.executable, '-c',
                'from backend.notion.rate_state import shared_delay; shared_delay(defer=30)'],
                check=True, timeout=10)
            delay, limited = shared_delay()
            self.assertTrue(limited); self.assertGreater(delay, 25)
            self.assertNotIn('unit-connection', ''.join(p.read_text() for p in Path(directory).iterdir()))
            with patch.dict(os.environ, {'NOTION_API_TOKEN':'unit-connection-b'}):
                self.assertEqual(shared_delay(), (0.0, False))
                shared_delay(interval=.36)
                self.assertGreater(shared_delay()[0], .3)
            self.assertTrue(all(isinstance(json.loads(p.read_text()), dict) for p in Path(directory).iterdir()))

    def test_body_rate_limit_also_delays_title_requests(self):
        from backend.notion import crawl_transport as crawl, transport
        now = [100.0]
        limited = Mock(status_code=429, headers={'Retry-After': '30'})
        ok = Mock(status_code=200, headers={})
        sent = []
        with (patch.dict(os.environ, {'NOTION_MIN_REQUEST_INTERVAL_SECONDS': '0', 'NOTION_SHARED_RATE_LIMIT_ENABLED': 'false'}),
              patch.object(transport, '_notion_next_request_at', 0.0),
              patch.object(transport, '_notion_retry_after_until', 0.0, create=True),
              patch.object(crawl, '_retry_after_until', 0.0, create=True),
              patch.object(transport.time, 'monotonic', side_effect=lambda: now[0]),
              patch.object(transport, 'interruptible_sleep', side_effect=lambda n: now.__setitem__(0, now[0]+n)),
              patch.object(crawl.requests, 'get', return_value=limited),
              patch.object(transport.requests, 'post', side_effect=lambda *a, **k: sent.append(now[0]) or ok)):
            with self.assertRaisesRegex(crawl.NotionReadFailure, 'http_429'):
                crawl.request_json('pages/a', crawl.CrawlBudget(110, 3))
            transport.notion_post_json('https://api.notion.test/search', {})
        self.assertGreaterEqual(sent[0], 130)

    def test_title_rate_limit_stops_body_reads_without_spending_request_budget(self):
        from backend.notion import crawl_transport as crawl, transport
        with (patch.dict(os.environ, {'NOTION_MIN_REQUEST_INTERVAL_SECONDS': '0', 'NOTION_SHARED_RATE_LIMIT_ENABLED': 'false'}),
              patch.object(transport.time, 'monotonic', return_value=100),
              patch.object(transport, '_notion_retry_after_until', 130, create=True),
              patch.object(crawl.requests, 'get') as get):
            budget = crawl.CrawlBudget(110, 3)
            with self.assertRaisesRegex(crawl.NotionReadFailure, 'http_429'):
                crawl.request_json('pages/a', budget)
        get.assert_not_called()
        self.assertEqual(budget.requests_left, 3)

    def test_sixty_second_retry_after_can_resume_inside_the_remaining_budget(self):
        from backend.notion import crawl_transport as crawl, transport
        now = [100.0]; sent = []
        limited = Mock(status_code=429, headers={'Retry-After':'60'})
        ok = Mock(status_code=200, headers={}); ok.json.return_value = {'id':'page'}
        def receive(*args, **kwargs):
            sent.append(now[0]); return limited if len(sent) == 1 else ok
        with (patch.object(transport, '_notion_next_request_at', 0.0),
              patch.object(transport, '_notion_retry_after_until', 0.0),
              patch.object(transport.time, 'monotonic', side_effect=lambda: now[0]),
              patch.object(crawl, 'interruptible_sleep', side_effect=lambda n: now.__setitem__(0, now[0]+n)),
              patch.object(crawl.requests, 'get', side_effect=receive)):
            result = crawl.request_json('pages/page', crawl.CrawlBudget(180, 3))
        self.assertEqual(result, {'id':'page'})
        self.assertEqual(sent, [100, 160])

    def test_title_candidates_without_embeddings_are_checked_and_old_body_is_not_evidence(self):
        from backend.notion.body_index import BodyIndex
        from backend.notion.retrieval_candidates import hybrid_candidates
        with tempfile.TemporaryDirectory() as directory:
            index = BodyIndex(Path(directory)/'body.sqlite3', fingerprint='test')
            index.upsert_page('db', page('pending', database='db', title='비교차트 사용 방법'))
            index.upsert_page('secret', page('private', database='secret', title='비교차트 사용 방법'))
            body = page('body', database='db', title='모바일 화면 설명서')
            index.upsert_page('db', body)
            index.replace_content('body', [segment('비교차트에서 종목을 선택합니다.', owner='body')],
                                  complete=True, expected_edited_time=body['last_edited_time'])
            found = hybrid_candidates('비교차트 사용 방법', ['db'], index=index, semantic=[])
        self.assertEqual({r['page_id'] for r in found}, {'pending', 'body'})
        self.assertTrue(all(r['candidate_only'] for r in found))

    def test_many_literal_issue_titles_do_not_hide_a_semantic_guide(self):
        from backend.notion.body_index import BodyIndex
        from backend.notion.retrieval_candidates import hybrid_candidates
        with tempfile.TemporaryDirectory() as directory:
            index = BodyIndex(Path(directory)/'body.sqlite3', fingerprint='test')
            for n in range(80):
                index.upsert_page('db', page(f'issue{n}', database='db', title=f'비교차트 오류 현상 {n}'))
            semantic = [{'page_id':'guide','database_id':'db','score':.8,'text':'종목 그래프 사용 절차'}]
            found = hybrid_candidates('비교차트 가이드', ['db'], index=index, semantic=semantic, limit=5)
        self.assertIn('guide', [r['page_id'] for r in found])

    def test_common_reference_is_explicit_bound_to_issue_and_keeps_issue_filters(self):
        from backend.tools import notion_tools
        from backend.tools.catalog import agent_registry
        ctx = context_for(); before = deepcopy(ctx.conditions)
        registry = agent_registry(query_tools=True)
        args = {'query': '비교차트 사용 방법', 'reference_scope': 'common'}
        stage = {'source':'notion','status':'ok','notion_pages':[], 'meta':{'search_complete':False}}
        with patch.object(notion_tools, 'execute_notion_body_search', return_value=stage) as source:
            rejected = registry.execute('search_notion_body', args, '', ctx)
            self.assertFalse(rejected['ok']); source.assert_not_called()
            ctx.related_ordinal = 1
            ctx.ledger.add('mantis', {'source':'mantis_snapshot','status':'ok',
                'issues_list':[{'id':42,'summary':'비교차트 업데이트'}],
                'meta':{'query_result':{'operation':'list','ordered_ids':[42]}}},
                grade='scoped', unapplied=(), call_id='list')
            result = registry.execute('search_notion_body', args, '', ctx)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['evidence_grade'], 'reference')
        self.assertIn('고객사', result['unapplied_conditions'])
        self.assertFalse(any(c['field']=='customer' for c in source.call_args.args[2]['filter_clauses']))
        self.assertEqual(ctx.conditions, before)

    def test_transient_read_failure_keeps_the_ref_retryable_without_exposing_old_body(self):
        from backend.tools import notion_tools
        from backend.tools.catalog import agent_registry
        from backend.notion.semantic_search import CurrentPageUnavailable
        ctx = context_for(); registry = agent_registry(query_tools=True)
        ctx.ledger.add('notion', {'source':'notion','status':'ok','notion_pages':[
            {'id':'p','database_id':'db','title':'한투 가이드','content':'이전 본문'}], 'meta':{}},
            grade='reference', unapplied=('상태',), call_id='search')
        current = {'id':'p','title':'한투 가이드','content':'현재 본문','content_incomplete':False,'_verified_body':True}
        with (patch.object(notion_tools, 'notion_scope', return_value={'db':'db'}),
              patch.object(notion_tools, 'read_current_page', side_effect=[CurrentPageUnavailable('http_429'), current])):
            first = registry.execute('get_notion_page_content', {'page_ref':'N1'}, '', ctx)
            self.assertFalse(first['ok']); self.assertTrue(first['error']['retryable'])
            self.assertNotIn('이전 본문', str(first))
            self.assertFalse(ctx.ledger.notion_page('p').get('content'))
            second = registry.execute('get_notion_page_content', {'page_ref':'N1'}, '', ctx)
        self.assertTrue(second['ok'], second)
        self.assertEqual(second['results'][0]['content'], '현재 본문')

    def test_common_reference_navigation_and_body_read_keep_the_same_visible_conditions(self):
        from backend.tools import notion_tools
        from backend.tools.catalog import agent_registry
        from dataclasses import asdict
        from backend.tools.conditions import SourceConditions
        ctx = context_for(); ctx.related_ordinal = 1
        ctx.ledger.add('mantis', {'source':'mantis_snapshot','status':'ok',
            'issues_list':[{'id':42,'summary':'비교차트 업데이트'}],
            'meta':{'query_result':{'operation':'list','ordered_ids':[42]}}},
            grade='scoped', unapplied=(), call_id='list')
        reference = SourceConditions('notion', {'filter_clauses':[]}, (), ('상태','고객사'), False)
        root = {'id':'root','database_id':'db','title':'비교차트','_verified_body':True,
                '_verified_page_links':[{'page_id':'guide','kind':'child'}], '_read_conditions':asdict(reference)}
        child = {'id':'guide','database_id':'db','title':'비교차트 사용 방법'}
        ctx.ledger.add('notion', {'source':'notion','status':'ok','notion_pages':[root], 'meta':{}},
            grade='reference', unapplied=reference.unapplied, call_id='search')
        ctx.ledger.related_pages['root'] = {'issue_id':42, 'terms':['비교차트']}
        registry = agent_registry(query_tools=True)
        with (patch.object(notion_tools, 'notion_scope', return_value={'db':'db'}),
              patch.object(notion_tools, 'follow_page_links', return_value=([child], {})) as navigate,
              patch.object(notion_tools, 'read_current_page', return_value={**child, '_verified_body':True,
                  'content':'비교차트에서 종목 추가 버튼을 선택합니다.', 'content_incomplete':False}) as read):
            registry.execute('list_notion_page_links', {'page_ref':'N1'}, '', ctx)
            result = registry.execute('get_notion_page_content', {'page_ref':'N2'}, '', ctx)
        self.assertEqual(navigate.call_args.args[2], reference.constraints)
        self.assertEqual(read.call_args.args[2], reference.constraints)
        self.assertEqual(result['evidence_grade'], 'reference')
        self.assertEqual(ctx.ledger.unapplied_conditions('notion'), ('상태','고객사'))
        self.assertTrue(ctx.has_related_guide())

    def test_investigative_title_search_never_falls_back_to_a_full_remote_database_scan(self):
        from backend.tools import notion_tools
        from backend.tools.catalog import agent_registry
        ctx = context_for(); ctx.investigative_notion = True
        with (patch.object(notion_tools, 'execute_notion_search', side_effect=AssertionError('full DB scan')),
              patch.object(notion_tools, 'execute_notion_body_search', return_value={
                  'source':'notion','status':'ok','notion_pages':[], 'meta':{'coverage':'top_k','search_complete':False}}) as candidate):
            result = agent_registry(query_tools=True).execute('search_notion_pages', {'query':'조건검색 서버'}, '', ctx)
        self.assertTrue(candidate.call_args.kwargs['title_only'])
        self.assertFalse(result['search_complete'])

    def test_verified_body_cache_avoids_repeated_api_reads_but_preserves_filters(self):
        from backend.notion import semantic_search as search
        current = {'id':'page','title':'한투 서버 가이드','content':'서버에 접속합니다.',
                   'content_incomplete':False, '_verified_body':True}
        cache = {'page': current}
        candidates = [{'page_id':'page','database_id':'db','score':.9}]
        with (patch.object(search, 'resolve_notion_settings', return_value={'database_ids':['db'],'token':'unit'}),
              patch.object(search, 'resolve_notion_read_scope', return_value=({'db':'db'}, True)),
              patch.object(search, 'semantic_candidates', return_value=candidates),
              patch.object(search, 'hybrid_candidates', return_value=candidates),
              patch.object(search, 'read_candidate_page', side_effect=AssertionError('재조회')) as read):
            first = search.search_notion_body('서버 접속', 5, {}, read_cache=cache)
            second = search.search_notion_body('접속 방법', 5, {}, read_cache=cache)
            denied = search.search_notion_body('접속 방법', 5,
                {'filter_clauses':[{'field':'customer','operator':'equals','value':'NH'}]}, read_cache=cache)
        self.assertEqual(read.call_count, 0)
        self.assertTrue(first['notion_pages']); self.assertTrue(second['notion_pages'])
        self.assertEqual(denied['notion_pages'], [])

    def test_candidate_search_checks_current_access_but_leaves_body_reads_to_the_model(self):
        from backend.notion import semantic_search as search
        from backend.notion import crawl_transport
        candidates = [{'page_id':'page','database_id':'db','score':.9,'text':'예전 제목\n이전 저장본의 본문'}]
        with (patch.object(search, 'resolve_notion_settings', return_value={'database_ids':['db'],'token':'unit'}),
              patch.object(search, 'resolve_notion_read_scope', return_value=({'db':'db'}, True)),
              patch.object(search, 'semantic_candidates', return_value=candidates),
              patch.object(search, 'hybrid_candidates', return_value=candidates),
              patch.object(crawl_transport, 'request_json', return_value=page('page', database='db', title='현재 가이드')) as get,
              patch.object(search, 'read_current_page', side_effect=AssertionError('후보 전체 본문 읽기'))):
            result = search.search_notion_body('비교차트 사용법', 5, {})
        self.assertEqual(get.call_args.args[0], 'pages/page')
        self.assertEqual(result['notion_pages'][0]['title'], '현재 가이드')
        self.assertEqual(result['notion_pages'][0]['content'], '')
        self.assertTrue(result['notion_pages'][0]['content_incomplete'])
        self.assertNotIn('semantic_excerpt', result['notion_pages'][0])
        self.assertFalse(result['meta']['search_complete'])
        from backend.tools.ledger import EvidenceLedger
        ledger = EvidenceLedger()
        ledger.add('notion', result, grade='scoped', unapplied=(), call_id='candidate')
        self.assertEqual(ledger.refs(), {})
        self.assertEqual(ledger.query_outcome({'notion'}), 'search_incomplete')
        self.assertEqual(ledger.notion_evidence_pages(), [])
        self.assertIsNotNone(ledger.notion_page('page'))

    def test_default_call_budget_allows_search_and_linked_page_reads(self):
        from backend.services.agent.policy import AgentSettings
        with patch.dict(os.environ, {}, clear=True):
            settings = AgentSettings.from_env()
        self.assertEqual(settings.max_model_requests, 16)
        self.assertEqual(settings.max_tool_calls, 16)


if __name__ == '__main__':
    unittest.main()
