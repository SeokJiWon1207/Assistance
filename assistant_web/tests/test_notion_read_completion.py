"""늦게 연 연결 문서의 시간 소진을 원천 장애·완전 조회와 구별한다."""
import unittest
from unittest.mock import patch

from test_tool_condition_arguments import context_for


class NotionReadCompletionTests(unittest.TestCase):
    def test_final_answer_discloses_a_failed_body_even_if_the_model_omits_it(self):
        from types import SimpleNamespace
        from backend.services.agent import orchestrator as agent
        from backend.tools.ledger import EvidenceLedger
        ledger = EvidenceLedger()
        ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': [
            {'id': 'failed', 'title': '버전처리 <서버>', '_candidate_only': True,
             '_body_read_failure': 'http_429'}]}, grade='scoped', unapplied=(), call_id='read')
        run = SimpleNamespace(ledger=ledger, model={'id': 'test'}, request=SimpleNamespace(showSources=False, modelId='test'),
                              stop_reason='deadline', finalization='free_form')
        with (patch.object(agent, '_reference_section', return_value=''),
              patch.object(agent, '_scoped_sources', return_value=[]),
              patch.object(agent, '_reference_sources', return_value=[]),
              patch.object(agent, '_retrieval', return_value={}),
              patch.object(agent, '_summary', return_value={}),
              patch.object(agent, '_checks', return_value=[])):
            result = agent._response(run, '확인한 기록입니다.', 'agent_model')
            self.assertIn('본문 조회 미완료', result['content'])
            self.assertIn('버전처리 &lt;서버&gt;', result['content'])
            self.assertIn('요청 제한 대기', result['content'])
            ledger.notion_page('failed').pop('_body_read_failure')
            result = agent._response(run, '확인한 기록입니다.', 'agent_model')
            self.assertNotIn('본문 조회 미완료', result['content'])

    def test_common_search_identifies_unread_procedures_for_the_selected_feature(self):
        from backend.tools import notion_tools
        from backend.tools.catalog import agent_registry
        ctx = context_for()
        ctx.related_ordinal = 1
        ctx.ledger.add('mantis', {'source': 'mantis_snapshot', 'status': 'ok',
            'issues_list': [{'id': 42, 'summary': '비교차트 실시간 업데이트'}],
            'meta': {'query_result': {'operation': 'list', 'ordered_ids': [42]}}},
            grade='scoped', unapplied=(), call_id='list')
        pages = [
            {'id': 'same', 'title': '구축 진행상황', '_candidate_only': True, '_candidate_body_guide': True,
             '_candidate_text': '비교차트 설정 파일을 복사하여 적용합니다.'},
            {'id': 'other', 'title': '인증서 갱신', '_candidate_only': True, '_candidate_body_guide': True,
             '_candidate_text': '인증서 갱신 파일을 복사하여 적용합니다.'}]
        with patch.object(notion_tools, 'execute_notion_body_search', return_value={
                'source': 'notion', 'status': 'ok', 'notion_pages': pages, 'meta': {}}):
            result = agent_registry(query_tools=True).execute('search_notion_body',
                {'query': '비교차트 실시간 갱신', 'reference_scope': 'common'}, '', ctx)
        self.assertEqual(result['unread_procedure_refs'], ['N1'])
        self.assertFalse(result['results'][1]['procedure_candidate'])
        self.assertEqual(result['evidence_grade'], 'reference')
        self.assertIn('고객사', result['unapplied_conditions'])
        self.assertEqual(ctx.ledger.notion_evidence_pages(), [])

    def test_common_guide_answer_discloses_unapplied_customer_condition(self):
        from types import SimpleNamespace
        from backend.services.agent.orchestrator import _reference_section
        ctx = context_for()
        ctx.related_ordinal = 1
        ctx.ledger.add('mantis', {'source': 'mantis_snapshot', 'status': 'ok',
            'issues_list': [{'id': 42, 'summary': '비교차트'}],
            'meta': {'query_result': {'operation': 'list', 'ordered_ids': [42]}}},
            grade='scoped', unapplied=(), call_id='list')
        ctx.ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': [
            {'id': 'guide', 'title': '타 고객사 구축 기록', 'content': '설정 절차', '_verified_body': True,
             '_read_conditions': {'unapplied': ['고객사', '상태']}}]},
            grade='reference', unapplied=('고객사', '상태'), call_id='body')
        ctx.ledger.related_pages['guide'] = {'issue_id': 42, 'body_checked': True, 'guide': True,
            'title': '타 고객사 구축 기록', 'ref': 'N1', 'excerpt': '설정 절차'}
        content = _reference_section(SimpleNamespace(tools=ctx, ledger=ctx.ledger))
        self.assertIn('조건 미적용: 고객사, 상태', content)
        self.assertIn('대상 이슈의 실제 원인·조치·완료를 확정하는 근거가 아닙니다', content)

    def test_procedure_hint_survives_candidate_validation_without_becoming_current_evidence(self):
        from backend.notion import semantic_search as search
        from backend.notion.crawl_transport import CrawlBudget
        from backend.tools.notion_tools import _page_view
        ctx = context_for()
        record = {'page_id': 'procedure', 'body_guide_hint': True,
                  'text': '진행 기록\n설정 파일을 복사하여 적용합니다.'}
        with (patch.object(search, 'page_record', return_value={'raw_page': {}, 'database_id': 'db'}),
              patch.object(search, 'read_full_properties', return_value={}),
              patch.object(search, 'normalize_scoped_page', return_value={'id': 'procedure', 'title': '진행 기록'}),
              patch.object(search, 'notion_page_matches_and_enrich', return_value=True)):
            page = search.read_candidate_page(record, {'db': 'db'}, {}, CrawlBudget(float('inf'), 10))
        ctx.ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': [page]},
                       grade='reference', unapplied=('고객사',), call_id='candidate')
        view = _page_view(ctx, page)
        self.assertTrue(view['procedure_candidate'])
        self.assertFalse(view['body_checked'])
        self.assertEqual(ctx.ledger.notion_evidence_pages(), [])
        self.assertFalse(ctx.ledger.related_pages)

    def test_feature_procedure_in_a_status_document_is_found_without_a_guide_heading(self):
        from backend.notion.related_guides import guide_passage
        body = ('구축 일정\n별도 협의\n비교차트\n선택코드저장\n'
                '화면 로드시 4자리 업종코드를 신규 코드로 변환합니다.\n설정 파일\n'
                'USER_GROUPLIST.dat 파일에 사용자 종목을 저장합니다.\n'
                'BASE_GROUPLIST.dat를 복사하여 기본 목록에 적용합니다.\n'
                '스프레드차트\n다른 기능의 설명')
        passage, is_guide = guide_passage('거래소 호환성 작업 현황', body, ['비교차트'])
        self.assertTrue(is_guide)
        self.assertIn('USER_GROUPLIST.dat', passage)
        self.assertNotIn('다른 기능', passage)
        for wrong in [
            '비교차트\n작업 예정\nUSER_GROUPLIST.dat 수정 가이드 작성 요청',
            '비교차트\n지원 예정\n스프레드차트\n화면 로드시 코드를 변환합니다.\n'
            'USER_GROUPLIST.dat 파일에 종목을 저장합니다.\nBASE_GROUPLIST.dat를 복사하여 적용합니다.'
        ]:
            self.assertFalse(guide_passage('개발 요청', wrong, ['비교차트'])[1])

    def test_body_procedure_candidate_outranks_empty_title_matches_without_promoting_stored_text_to_evidence(self):
        import tempfile
        from pathlib import Path
        from test_notion_body_index import page, segment
        from backend.notion.body_index import BodyIndex
        from backend.notion.retrieval_candidates import hybrid_candidates
        with tempfile.TemporaryDirectory() as directory:
            index = BodyIndex(Path(directory) / 'body.sqlite3', fingerprint='unit')
            for i in range(80):
                index.upsert_page('db', page(f'ticket{i}', database='db', title=f'비교차트 오류 {i}'))
            guide = page('procedure', database='db', title='호환성 개발 진행상황')
            index.upsert_page('db', guide)
            body = ('비교차트\n화면 로드시 업종코드를 변환합니다.\n'
                    'USER_GROUPLIST.dat 파일에 종목을 저장합니다.\nBASE_GROUPLIST.dat를 복사하여 적용합니다.')
            index.replace_content('procedure', [segment(body, owner='procedure')], complete=True,
                                  expected_edited_time=guide['last_edited_time'])
            semantic = [{'page_id': f'ticket{i}', 'database_id': 'db', 'score': .9, 'text': '이슈 제목'} for i in range(20)]
            found = hybrid_candidates('비교차트 가이드', ['db'], index=index, semantic=semantic, limit=5)
        self.assertEqual(found[0]['page_id'], 'procedure')
        self.assertIn('USER_GROUPLIST.dat', found[0]['text'])
        self.assertTrue(found[0]['candidate_only'])

    def test_verified_body_leaves_the_last_link_read_time_available_but_unverified_hints_do_not(self):
        from types import SimpleNamespace
        from agent_harness_cases import final
        from backend.services.agent import orchestrator as agent
        from backend.services.agent.policy import AgentSettings
        from backend.services.agent.protocol import parse_assistant_turn
        from backend.tools.ledger import EvidenceLedger
        for verified, incomplete, grade, continues in [(True, False, 'scoped', True),
                (False, False, 'scoped', False), (True, True, 'scoped', False), (True, False, 'reference', False)]:
            with self.subTest(verified=verified, incomplete=incomplete, grade=grade):
                ledger = EvidenceLedger()
                ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': [
                    {'id': 'root', 'content': '확인된 본문', '_candidate_only': True,
                     '_verified_body': verified, 'content_incomplete': incomplete}]},
                    grade=grade, unapplied=(), call_id='read')
                state = SimpleNamespace(settings=AgentSettings(), model={'id': 'test'},
                    budget=agent.AgentBudget(16, 16, 2, 45, model_requests=6), ledger=ledger,
                    registry=SimpleNamespace(definitions=lambda _: []), tools=SimpleNamespace(),
                    messages=[], stop_reason='', nudged=False, decision_models=[])
                with (patch.object(agent, '_system_prompt', return_value=''),
                      patch.object(agent, '_user_prompt', return_value=''),
                      patch.object(agent, '_query_outcome', return_value='sufficient_evidence'),
                      patch.object(agent.AgentBudget, 'remaining', return_value=40),
                      patch.object(agent, '_request_turn', return_value=parse_assistant_turn({'choices': [final('연결 본문 확인')]}))):
                    result = agent._investigate(state)
                self.assertEqual(result, '연결 본문 확인' if continues else None)
                self.assertEqual(state.budget.reserve_calls, 2)

    def test_observed_property_link_can_be_opened_without_crawling_the_source_body_first(self):
        from backend.tools import notion_tools
        from backend.tools.catalog import agent_registry
        from backend.notion import crawl_transport
        from test_notion_body_index import page
        ctx = context_for()
        target = 'b' * 32
        ctx.ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': [
            {'id': 'root', 'database_id': 'db', 'title': '한투 기록', '_candidate_only': True,
             '_verified_page_links': [{'page_id': target, 'kind': 'relation'}]}]},
            grade='reference', unapplied=('상태',), call_id='search')
        with (patch.object(notion_tools, 'notion_scope', return_value={'db': 'db'}),
              patch.object(notion_tools, 'read_current_page', side_effect=AssertionError('원본 본문 선행 수집')),
              patch.object(crawl_transport, 'request_json', return_value=page(target, database='db', title='한투 연결 문서'))):
            result = agent_registry(query_tools=True).execute('list_notion_page_links', {'page_ref': 'N1'}, '', ctx)
        self.assertTrue(result['ok'])
        self.assertEqual(result['results'][0]['title'], '한투 연결 문서')
        self.assertEqual(result['results'][0]['ref'], 'N2')
        self.assertFalse(ctx.ledger.notion_page('root').get('_verified_body'))
        self.assertEqual(ctx.ledger.notion_evidence_pages(), [])

    def test_unreadable_observed_link_is_counted_as_incomplete_not_an_empty_link_set(self):
        import time
        from backend.notion import page_links, crawl_transport
        source = {'id': 'a' * 32, '_verified_page_links': [{'page_id': 'b' * 32, 'kind': 'link'}]}
        with patch.object(crawl_transport, 'request_json', side_effect=crawl_transport.NotionReadFailure('http_404')):
            found, meta = page_links.follow_page_links(source, {'db': 'db'}, {}, deadline=time.monotonic() + 20)
        self.assertEqual(found, [])
        self.assertEqual(meta['unavailable_link_count'], 1)
        self.assertIn('http_404', meta['incomplete_reasons'])
        self.assertFalse(meta['search_complete'])
        from backend.tools.ledger import EvidenceLedger
        ledger = EvidenceLedger()
        ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': [
            {'id': 'a' * 32, 'content': '읽은 본문', '_verified_body': True}], 'meta': meta},
            grade='scoped', unapplied=(), call_id='links')
        self.assertEqual(ledger.query_outcome({'notion'}), 'partial_evidence')

    def test_outer_tool_deadline_marks_the_linked_body_incomplete_without_an_immediate_retry(self):
        from backend.execution import StepTimeout
        from backend.tools import registry as tool_registry
        from backend.tools.catalog import agent_registry
        ctx = context_for()
        ctx.step_seconds = lambda _: 1.0
        ctx.ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': [
            {'id': 'root', 'database_id': 'db', 'content': '확인한 기록', '_verified_body': True},
            {'id': 'linked', 'database_id': 'db', '_candidate_only': True}]},
            grade='scoped', unapplied=(), call_id='links')
        with patch.object(tool_registry, 'run_with_deadline', side_effect=StepTimeout('단계 마감')):
            result = agent_registry(query_tools=True).execute('get_notion_page_content', {'page_ref': 'N2'}, '', ctx)
        self.assertEqual(result['error']['code'], 'retrieval_budget_exhausted')
        self.assertFalse(result['error']['retryable'])
        self.assertEqual(ctx.ledger.query_outcome({'notion'}), 'partial_evidence')

    def test_expired_body_read_is_not_retryable_and_cannot_leave_stale_evidence(self):
        from backend.notion.semantic_search import CurrentPageUnavailable
        from backend.tools import notion_tools
        from backend.tools.catalog import agent_registry
        ctx = context_for()
        ctx.ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': [
            {'id': 'p', 'database_id': 'db', 'title': '연결 문서', 'content': '이전 본문'}]},
            grade='scoped', unapplied=(), call_id='links')
        with (patch.object(notion_tools, 'notion_scope', return_value={'db': 'db'}),
              patch.object(notion_tools, 'read_current_page', side_effect=CurrentPageUnavailable('time_limit'))):
            result = agent_registry(query_tools=True).execute('get_notion_page_content', {'page_ref': 'N1'}, '', ctx)
        self.assertEqual(result['error']['code'], 'retrieval_budget_exhausted')
        self.assertFalse(result['error']['retryable'])
        self.assertEqual(result['error']['reason'], 'time_limit')
        self.assertEqual(ctx.ledger.notion_evidence_pages(), [])
        self.assertEqual(ctx.ledger.query_outcome({'notion'}), 'search_incomplete')

    def test_failed_linked_body_keeps_partial_outcome_until_the_body_is_read(self):
        from backend.notion.semantic_search import CurrentPageUnavailable
        from backend.tools import notion_tools
        from backend.tools.catalog import agent_registry
        ctx = context_for()
        ctx.ledger.add('notion', {'source': 'notion', 'status': 'ok', 'notion_pages': [
            {'id': 'root', 'database_id': 'db', 'title': '기록', 'content': '연결 문서를 확인하세요.',
             '_verified_body': True, 'content_incomplete': False},
            {'id': 'linked', 'database_id': 'db', 'title': '연결 문서', '_candidate_only': True,
             'navigation': {'from_page_id': 'root', 'kind': 'link'}}]},
            grade='scoped', unapplied=(), call_id='links')
        registry = agent_registry(query_tools=True)
        with (patch.object(notion_tools, 'notion_scope', return_value={'db': 'db'}),
              patch.object(notion_tools, 'read_current_page', side_effect=[CurrentPageUnavailable('request_failed'),
                  {'id': 'linked', 'content': '확인한 연결 본문', '_verified_body': True, 'content_incomplete': False}])):
            first = registry.execute('get_notion_page_content', {'page_ref': 'N2'}, '', ctx)
            self.assertTrue(first['error']['retryable'])
            self.assertEqual(ctx.ledger.query_outcome({'notion'}), 'partial_evidence')
            second = registry.execute('get_notion_page_content', {'page_ref': 'N2'}, '', ctx)
        self.assertTrue(second['ok'])
        self.assertEqual(ctx.ledger.query_outcome({'notion'}), 'sufficient_evidence')

    def test_retry_after_longer_than_read_budget_is_not_reported_as_immediately_retryable(self):
        from backend.notion import crawl_transport as crawl, semantic_search as search, transport
        with (patch.object(transport.time, 'monotonic', return_value=100),
              patch.object(transport, '_notion_retry_after_until', 160),
              patch.object(search, 'open_body_index', return_value=None),
              patch.object(crawl.requests, 'get', side_effect=AssertionError('예산 밖의 요청'))):
            with self.assertRaises(search.CurrentPageUnavailable) as caught:
                search.read_current_page({'page_id': 'page', 'database_id': 'db'}, {'db': 'db'}, {}, deadline=130)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(str(caught.exception), 'http_429')


if __name__ == '__main__':
    unittest.main()
