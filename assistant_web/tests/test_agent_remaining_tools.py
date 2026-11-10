"""남은 결정형 도구의 조건·완전성·원문 경계."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend.intent.unified_validation import validate_plan
from backend.tools.catalog import agent_registry
from test_tool_condition_arguments import PROPOSAL, arguments, context_for
from agent_harness_cases import FakeModel, FakeSources, agent_environment, chat_request, explain_context, final, raw_issue, tool_call, turn


def notion_context(operation='count'):
    q = '노션에서 제목이 "조건검색 서버"인 문서는 몇 건이야?'
    proposal = {**deepcopy(PROPOSAL), 'source': 'notion', 'operation': operation, 'limit': None if operation == 'count' else 3,
               'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
               'filters': [{'field': 'title', 'operator': 'equals', 'value': '조건검색 서버', 'evidence': '조건검색 서버'}]}
    plan = validate_plan(q, proposal, {'uid': 'u'}, {})
    return context_for(plan, q)


def notion_stage(complete=True, total=0):
    return {'source': 'notion', 'status': 'ok', 'notion_pages': [],
            'meta': {'candidate_count': total, 'search_complete': complete,
                     'candidate_count_complete': complete, 'result_window_complete': True}}


class RemainingToolTests(unittest.TestCase):
    def test_native_notion_search_does_not_reparse_output_fields_as_filters(self):
        ctx = notion_context('get')
        with patch('backend.tools.notion_tools.execute_notion_search', return_value=notion_stage()) as engine:
            result = agent_registry(query_tools=True).execute('search_notion_pages',
                {'query': '조건검색 서버'}, '', ctx)
        self.assertTrue(result['ok'], result)
        self.assertFalse(engine.call_args.kwargs.get('derive_text_constraints', True))
        self.assertIn('get_notion_page_properties', agent_registry().specs)
    def test_property_verification_uses_grounded_filters_not_a_person_named_property(self):
        from backend.evaluation.verification import build_verification_report
        from backend.services.agent.deterministic_answers import render_notion_results
        q = '노션에서 제목이 "조건검색 서버"인 문서의 상태와 담당자 속성을 알려줘.'
        proposal = {**deepcopy(PROPOSAL), 'source': 'notion', 'operation': 'get',
                    'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
                    'filters': [{'field': 'title', 'operator': 'equals', 'value': '조건검색 서버', 'evidence': '조건검색 서버'}]}
        plan = validate_plan(q, proposal, {'uid': 'u'}, {})
        page = {'id': 'page-1', 'title': '조건검색 서버', 'owner': '김지은', 'status': '진행', '_verified_properties': True}
        stage = {'source': 'notion', 'status': 'ok', 'notion_pages': [page],
                 'meta': {'property_result': {'page_id': 'page-1', 'page_ref': 'N1', 'title': page['title'], 'values': {'owner': '김지은'}}}}
        retrieval = {'query': q, 'query_plan': plan, 'intent_plan': plan, 'constraints': plan['resolved_constraints'], 'stages': [stage]}
        sources = [{'type': 'notion', 'title': page['title'], 'detail': page}]
        content = render_notion_results(q, retrieval, 3)
        result = build_verification_report(q, content, sources, retrieval, 'agent_notion_results')
        self.assertEqual(next(c for c in result['checks'] if c['id'] == 'constraints')['status'], 'pass')
        damaged = [{**sources[0], 'title': '다른 문서'}]
        result = build_verification_report(q, content, damaged, retrieval, 'agent_notion_results')
        self.assertEqual(next(c for c in result['checks'] if c['id'] == 'constraints')['status'], 'fail')
    def test_notion_validated_sort_can_be_applied_without_losing_contract(self):
        from backend.tools.conditions import project_conditions
        from backend.tools.query_conditions import build_query_contract
        q = '노션에서 제목이 "조건검색 서버"인 문서 최근 3건 보여줘.'
        proposal = {**deepcopy(PROPOSAL), 'source': 'notion',
                    'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
                    'filters': [{'field': 'title', 'operator': 'equals', 'value': '조건검색 서버', 'evidence': '조건검색 서버'}]}
        plan = validate_plan(q, proposal, {'uid': 'u'}, {})
        plan['sort'] = {'field': 'created_at', 'direction': 'desc'}
        plan['resolved_constraints'].pop('sort', None)
        conditions = project_conditions(plan, {**plan['resolved_constraints'], 'sort': plan['sort']}, 'notion')
        contract = build_query_contract(q, plan, conditions, 3, {}, query_tools=True)
        self.assertEqual(contract.sort, plan['sort'])
    def test_notion_display_window_is_not_a_complete_zero_and_tampered_total_blocks(self):
        from backend.evaluation.verification import build_verification_report
        from backend.evaluation.delivery import decide_delivery
        from backend.services.agent.deterministic_answers import render_notion_results
        ctx = notion_context()
        stage = notion_stage(True)
        stage['meta']['candidate_count_complete'] = False
        with patch('backend.notion.search.execute_notion_search', return_value=stage):
            result = agent_registry(query_tools=True).execute('count_notion_pages',
                {'filters': ctx.query_contracts['notion'].filters}, '', ctx)
        self.assertIsNone(result['total_count'])
        retrieval = {'stages': [s.stage for s in ctx.ledger.stages], 'limit': 3}
        content = render_notion_results(ctx.question, retrieval, 3)
        self.assertNotIn('찾지 못했어요', content)
        self.assertNotIn('총 0건', content)
        report = build_verification_report(ctx.question, content + '\n총 99건입니다.', [], retrieval, 'agent_notion_results')
        self.assertIn('agent_notion_result', decide_delivery(report).blocked_checks)

    def test_population_selects_api_order_before_filters_and_reuses_window(self):
        from backend.evaluation.agent_steps import verify_condition_steps
        q = '전사 한투 미해결 이슈 최근 2건 기준으로 최근 3건 보여줘.'
        plan = validate_plan(q, deepcopy(PROPOSAL), {'uid': 'u'}, {})
        ctx = context_for(plan, q)
        rows = [raw_issue(8, '다른 고객사', '', status='assigned'),
                raw_issue(2, '창 안의 과거 번호', '', project='117Hantu', status='assigned'),
                raw_issue(99, '창 밖의 큰 번호', '', project='117Hantu', status='assigned')]
        registry = agent_registry(query_tools=True)
        with patch('backend.sources.mantis.fetch_mantis_issues', return_value=rows) as api:
            counted = registry.execute('count_mantis_issues', {**arguments(ctx), 'population_limit': 2}, '', ctx)
            listed = registry.execute('list_mantis_issues', {'condition_ref': counted.get('condition_ref'), 'limit': 3}, '', ctx)
        self.assertTrue(counted['ok'], counted)
        self.assertTrue(listed['ok'], listed)
        self.assertEqual(listed['ordered_ids'], [2])
        self.assertEqual(listed['total_count'], 1)
        self.assertEqual(api.call_count, 1)
        self.assertEqual(verify_condition_steps(ctx.ledger, ctx.condition_refs)['status'], 'pass')

    def test_population_timeout_does_not_use_complete_store_or_claim_zero(self):
        q = '전사 한투 미해결 이슈 최신 150건 기준으로 최근 3건 보여줘.'
        ctx = context_for(validate_plan(q, deepcopy(PROPOSAL), {'uid': 'u'}, {}), q)
        with patch('backend.tools.population.collect_mantis_live_ids_in_range', return_value={'status': 'error', 'issues': {}, 'complete': False}), \
             patch('backend.tools.mantis_queries.execute_mantis_snapshot_search') as store:
            result = agent_registry(query_tools=True).execute('count_mantis_issues', {**arguments(ctx), 'population_limit': 150}, '', ctx)
        self.assertIsNone(result['total_count'])
        self.assertFalse(result['search_complete'])
        store.assert_not_called()

    def test_current_property_values_are_verified_and_access_failure_drops_old_page(self):
        from backend.services.agent.deterministic_answers import notion_result_check, render_notion_results
        for allowed in (True, False):
            ctx = notion_context('get')
            page = {'id': 'page-1', 'title': '조건검색 서버', 'status': '대기', 'owner': '담당자'}
            ctx.ledger.add('notion', {'source': 'notion', 'notion_pages': [page]}, grade='scoped', unapplied=(), call_id='seed')
            current = {**page, 'status': '진행', '_candidate_only': True}
            with patch('backend.tools.notion_queries.notion_scope', return_value={'db': {}}), \
                 patch('backend.tools.notion_queries.read_candidate_page', return_value=current if allowed else None):
                result = agent_registry(query_tools=True).execute('get_notion_page_properties',
                    {'page_ref': 'N1', 'fields': ['status', 'due_date']}, '', ctx)
            self.assertEqual(result['ok'], allowed)
            if allowed:
                self.assertEqual(result['values'], {'status': '진행', 'due_date': ''})
                retrieval = {'stages': [s.stage for s in ctx.ledger.stages]}
                content = render_notion_results(ctx.question, retrieval, 5)
                self.assertIn('기한: 미기재', content)
                self.assertEqual(notion_result_check(ctx.question, content, retrieval)['status'], 'pass')
                self.assertEqual(notion_result_check(ctx.question, content.replace('진행', '완료'), retrieval)['status'], 'fail')
            else:
                self.assertFalse(ctx.ledger.notion_pages())

    def test_notion_count_uses_agent_and_ignores_model_invented_number(self):
        from backend.services.agent.route import run_agent_if_eligible
        from backend.services.agent.policy import request_transfer_scope
        ctx = notion_context()
        model = FakeModel([turn(tool_call('count_notion_pages', {'filters': ctx.query_contracts['notion'].filters})), final('총 999건')])
        proposal = {**deepcopy(PROPOSAL), 'source': 'notion', 'operation': 'count', 'limit': None,
                   'filters': [{'field': 'title', 'operator': 'equals', 'value': '조건검색 서버', 'evidence': '조건검색 서버'}],
                   'status': {'mode': 'unspecified', 'values': [], 'evidence': ''}}
        plan = validate_plan(ctx.question, proposal, {'uid': 'u'}, {})
        with agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'), \
             patch('backend.notion.search.execute_notion_search', return_value=notion_stage()):
            chat = explain_context(ctx.question, source='notion', constraints=plan['resolved_constraints'])
            chat.intent_plan = plan
            chat.structured_tool_conditions = True
            result = run_agent_if_eligible(chat_request(ctx.question), chat)
        self.assertEqual(result['raw']['mode'], 'agent_notion_results')
        self.assertIn('0건', result['content'])
        self.assertNotIn('999', result['content'])
        self.assertEqual(next(c for c in result['agentChecks'] if c['id'] == 'agent_step_conditions')['status'], 'pass')

    def test_notion_single_lookup_answers_from_searched_title_without_body(self):
        from backend.services.agent.route import run_agent_if_eligible
        from backend.services.agent.policy import request_transfer_scope
        ctx = notion_context()
        page = {'id': 'p1', 'database_id': 'db', 'title': '조건검색 서버', 'database_label': '기술 문서', 'status': '진행',
                'owner': '', 'content': '', 'properties_text': '상태: 진행', 'url': 'https://www.notion.so/p1',
                'last_edited_time': '2026-05-05T00:00:00Z'}
        stage = {'source': 'notion', 'status': 'ok', 'notion_pages': [page],
                 'meta': {'search_complete': True, 'displayed_count': 1, 'constraints': {}}}
        model = FakeModel([turn(tool_call('search_notion_pages', {'query': '조건검색 서버'})), final('본문 근거가 없습니다')])
        proposal = {**deepcopy(PROPOSAL), 'source': 'notion', 'operation': 'get', 'limit': None,
                    'filters': [{'field': 'title', 'operator': 'equals', 'value': '조건검색 서버', 'evidence': '조건검색 서버'}],
                    'status': {'mode': 'unspecified', 'values': [], 'evidence': ''}}
        plan = validate_plan(ctx.question, proposal, {'uid': 'u'}, {})
        with agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'), \
             patch('backend.tools.notion_tools.execute_notion_search', return_value=stage):
            chat = explain_context(ctx.question, source='notion', constraints=plan['resolved_constraints'])
            chat.intent_plan = plan
            chat.structured_tool_conditions = True
            result = run_agent_if_eligible(chat_request(ctx.question), chat)
        # 본문을 읽지 않아도 조건을 적용해 찾은 문서의 제목·속성으로 답한다.
        self.assertEqual(result['raw']['mode'], 'agent_notion_results', result['content'])
        self.assertIn('조건검색 서버', result['content'])
        self.assertNotIn('적용 조건에 맞는 Notion 항목은', result['content'])
        from backend.evaluation.delivery import decide_delivery
        from backend.evaluation.verification import build_verification_report
        report = build_verification_report(ctx.question, result['content'], result['sourcesForFollowUps'],
                                           result['retrieval'], 'agent_notion_results')
        self.assertTrue(decide_delivery(report).allowed, [c for c in report['checks'] if c['status'] == 'fail'])

    def test_notion_count_distinguishes_complete_zero_from_incomplete_window(self):
        for complete in (True, False):
            ctx = notion_context()
            with self.subTest(complete=complete), patch('backend.notion.search.execute_notion_search', return_value=notion_stage(complete)):
                result = agent_registry(query_tools=True).execute('count_notion_pages',
                    {'filters': ctx.query_contracts['notion'].filters}, '', ctx)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['total_count'], 0 if complete else None)
            self.assertEqual(result['search_complete'], complete)

    def test_notion_list_must_not_drop_title_filter(self):
        ctx = notion_context('search')
        with patch('backend.notion.search.execute_notion_search') as engine:
            result = agent_registry(query_tools=True).execute('list_notion_pages', {'filters': [], 'limit': 3}, '', ctx)
        self.assertEqual(result['error']['code'], 'condition_mismatch')
        engine.assert_not_called()

    def test_notion_property_requires_a_current_request_reference(self):
        ctx = notion_context('get')
        result = agent_registry(query_tools=True).execute('get_notion_page_properties', {'page_ref': 'N1'}, '', ctx)
        self.assertEqual(result['error']['code'], 'not_in_scope')

    def test_population_is_separate_from_display_and_cannot_be_omitted(self):
        q = '전사 한투 미해결 이슈 최신 150건 기준으로 최근 3건 보여줘.'
        plan = validate_plan(q, deepcopy(PROPOSAL), {'uid': 'u'}, {})
        ctx = context_for(plan, q)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search') as full_scan:
            result = agent_registry(query_tools=True).execute('list_mantis_issues', {**arguments(ctx), 'limit': 3}, '', ctx)
        self.assertFalse(result['ok'], result)
        full_scan.assert_not_called()

    def test_report_tool_cannot_change_kind_or_omit_required_status(self):
        q = '전사 한투 미해결 이슈만 2026년 10월 월간 고객사 리포트로 보여줘.'
        proposal = {**deepcopy(PROPOSAL), 'operation': 'report', 'report_kind': 'monthly',
                   'filters': [*deepcopy(PROPOSAL['filters']), {'field': 'created_at', 'operator': 'between',
                      'value': ['2026-10-01', '2026-10-31'], 'evidence': '2026년 10월'}]}
        ctx = context_for(validate_plan(q, proposal, {'uid': 'u'}, {}), q)
        with patch('backend.reports.monthly_customer.build_monthly_customer_report') as engine:
            for args in ({**arguments(ctx), 'kind': 'weekly'},
                         {**arguments(ctx), 'kind': 'monthly', 'filters': []}):
                result = agent_registry(query_tools=True).execute('build_issue_report', args, '', ctx)
                self.assertFalse(result['ok'], result)
                self.assertEqual(result['error']['code'], 'condition_mismatch')
        engine.assert_not_called()
