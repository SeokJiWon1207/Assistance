"""일반 요청의 조회·집계 실행과 서버 검증 경계."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from agent_harness_cases import FakeModel, FakeSources, agent_environment, chat_request, explain_context, final, tool_call, turn
from backend.intent.unified_validation import validate_plan
from backend.services.agent.policy import request_transfer_scope
from backend.services.agent.route import run_agent_if_eligible
from backend.tools.catalog import agent_registry
from test_tool_condition_arguments import PROPOSAL, arguments, context_for, scanned


def aggregation_plan():
    question = '2026년 10월 전사 한투 미해결 이슈를 매체별로 집계해줘. 표만 보여줘.'
    proposal = {**deepcopy(PROPOSAL), 'operation': 'aggregate', 'limit': None,
                'aggregation': {'group_by': ['platform'], 'metrics': ['issue_count'],
                    'period': {'from': '2026-10-01', 'to': '2026-10-31', 'evidence': '2026년 10월'},
                    'comparison': None, 'top_n': 10, 'explain': False}}
    return question, validate_plan(question, proposal, {'uid': 'u'}, {})


def aggregate_arguments(ctx):
    return {**arguments(ctx), **deepcopy(ctx.query_contracts['mantis'].model_view()['aggregation'])}


class AgentQueryPrimaryTests(unittest.TestCase):
    def test_latest_population_window_enters_agent_with_a_separate_population_contract(self):
        from backend.services.agent.route import agent_route_reason
        with agent_environment(), request_transfer_scope('company-test'):
            ctx = explain_context('전사 한투 이슈 최신 150건 기준으로 몇 건이야?', source='mantis')
            ctx.intent_plan['operation'] = 'count'
            self.assertEqual(agent_route_reason(ctx), '')

    def test_policy_and_disabled_sources_still_prevent_normal_query_entry(self):
        from backend.services.agent.route import agent_route_reason
        with agent_environment():
            ctx = explain_context('전사 한투 미해결 이슈 몇 건이야?', source='mantis')
            ctx.intent_plan['operation'] = 'count'
            self.assertEqual(agent_route_reason(ctx), 'not_candidate')
            with request_transfer_scope('company-test'):
                ctx.enabled_tools['mantis'] = False
                self.assertEqual(agent_route_reason(ctx), 'source_disabled')
                ctx.enabled_tools['mantis'] = True
                ctx.intent_plan['source'] = 'notion'
                self.assertEqual(agent_route_reason(ctx), '')

    def test_aggregate_reference_cannot_authorize_changed_metrics(self):
        q, plan = aggregation_plan()
        ctx = context_for(plan, q)
        registry = agent_registry(query_tools=True)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()):
            counted = registry.execute('count_mantis_issues', arguments(ctx), '', ctx)
        proposal = {**ctx.query_contracts['mantis'].aggregation_arguments(), 'condition_ref': counted['condition_ref'], 'metrics': ['effort']}
        with patch('backend.tools.mantis_aggregations.execute_aggregation') as engine:
            denied = registry.execute('aggregate_mantis_issues', proposal, '', ctx)
        self.assertFalse(denied['ok'])
        engine.assert_not_called()

    def test_normal_list_keeps_fifty_sources_and_verifies_the_entire_order(self):
        from backend.evaluation.verification import build_verification_report
        q = '전사 한투 미해결 이슈 최근 50건 보여줘.'
        plan = validate_plan(q, {**deepcopy(PROPOSAL), 'limit': 50}, {'uid': 'u'}, {})
        stage = scanned()
        stage['issues_list'] = [{'id': n, 'summary': str(n)} for n in range(70, 0, -1)]
        stage['meta'].update(found_count=70, matched_issue_ids=list(range(1, 71)), ordered_matched_ids=list(range(70, 0, -1)))
        model = FakeModel([turn(tool_call('list_mantis_issues', {**arguments(context_for(plan, q)), 'limit': 50})), final('완료')])
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage)):
            ctx = explain_context(q, source='mantis', constraints=plan['resolved_constraints'])
            ctx.intent_plan = plan
            ctx.structured_tool_conditions = True
            result = run_agent_if_eligible(chat_request(q), ctx)
        self.assertEqual([s['issue_id'] for s in result['sourcesForFollowUps']], list(range(70, 20, -1)))
        report = build_verification_report(q, result['content'], result['sourcesForFollowUps'], result['retrieval'], result['raw']['mode'])
        self.assertEqual(next(c for c in report['checks'] if c['id'] == 'agent_query_result')['status'], 'pass')

    def test_count_and_list_use_normal_route_and_ignore_invented_model_totals(self):
        for operation in ('count', 'search'):
            q = '전사 한투 미해결 이슈 ' + ('몇 건이야?' if operation == 'count' else '최근 3건을 보여줘.')
            proposal = {**deepcopy(PROPOSAL), 'operation': operation, 'limit': None if operation == 'count' else 3}
            plan = validate_plan(q, proposal, {'uid': 'u'}, {})
            name = 'count_mantis_issues' if operation == 'count' else 'list_mantis_issues'
            args = {'filters': plan['filter_clauses'], 'scope': 'all', **({'limit': 3} if operation == 'search' else {})}
            model = FakeModel([turn(tool_call(name, args)), final('999건입니다.')])
            with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
                  patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned())):
                ctx = explain_context(q, source='mantis', constraints=plan['resolved_constraints'])
                ctx.intent_plan = plan
                ctx.structured_tool_conditions = True
                result = run_agent_if_eligible(chat_request(q), ctx)
            self.assertIsNotNone(result)
            self.assertEqual(result['raw']['mode'], 'agent_query_results')
            self.assertIn('4건입니다', result['content'])
            self.assertNotIn('999', result['content'])
            if operation == 'search':
                self.assertEqual([s['issue_id'] for s in result['sourcesForFollowUps']], [44, 43, 42])

    def test_all_requested_rows_above_twenty_are_preserved_in_same_scan(self):
        q = '전사 한투 미해결 이슈 최근 50건 보여줘.'
        plan = validate_plan(q, {**deepcopy(PROPOSAL), 'limit': 50}, {'uid': 'u'}, {})
        ctx = context_for(plan, q)
        stage = scanned()
        stage['issues_list'] = [{'id': n, 'summary': str(n)} for n in range(70, 0, -1)]
        stage['meta'].update(found_count=70, matched_issue_ids=list(range(1, 71)), ordered_matched_ids=list(range(70, 0, -1)))
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage) as scan:
            result = agent_registry(query_tools=True).execute('list_mantis_issues', {**arguments(ctx), 'limit': 50}, '', ctx)
        self.assertTrue(result['ok'], result)
        self.assertEqual(len(result['ordered_ids']), 50)
        self.assertGreaterEqual(scan.call_args.args[0], 50)

    def test_aggregation_arguments_cannot_change_confirmed_spec_or_filters(self):
        q, plan = aggregation_plan()
        for change in ({'group_by': ['owner']}, {'metrics': ['effort']}, {'top_n': 3},
                       {'period': {'from': '2026-01-01', 'to': '2026-10-31'}}, {'filters': []},
                       {'comparison': 'previous_month'}, {'scope': 'team'}):
            ctx = context_for(plan, q)
            with patch('backend.tools.mantis_aggregations.execute_aggregation') as engine:
                result = agent_registry(query_tools=True).execute('aggregate_mantis_issues',
                    {**aggregate_arguments(ctx), **change}, '', ctx)
            self.assertFalse(result['ok'], result)
            engine.assert_not_called()

    def test_complete_aggregation_uses_existing_engine_and_ledger(self):
        from backend.reports.aggregation import aggregate_rows
        q, plan = aggregation_plan()
        ctx = context_for(plan, q)
        rows = [(None, None, None, {'id': i, 'created_at': '2026-10-05', 'media_type': platform})
                for i, platform in enumerate(['mts', 'mts', 'hts'], 1)]
        expected = aggregate_rows(rows, plan['aggregation'], plan['resolved_constraints'])
        expected.update(source={'search_complete': True, 'fetchedAt': '2026-11-06T00:00:00Z'},
                        filter_audit={'fully_applied': True})
        with patch('backend.tools.mantis_aggregations.execute_aggregation', return_value=expected) as engine:
            result = agent_registry(query_tools=True).execute('aggregate_mantis_issues', aggregate_arguments(ctx), '', ctx)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['totals'], {'issue_count': 3})
        self.assertEqual(engine.call_args.args[0]['aggregation'], plan['aggregation'])
        self.assertEqual(ctx.ledger.aggregation_results()[0]['result'], expected)

    def test_incomplete_aggregation_never_publishes_zero_or_success(self):
        q, plan = aggregation_plan()
        ctx = context_for(plan, q)
        for response in (LookupError('전체 조회 미완료'), {'totals': {'issue_count': 0}, 'source': {'search_complete': False}}):
            with patch('backend.tools.mantis_aggregations.execute_aggregation',
                       **({'side_effect': response} if isinstance(response, Exception) else {'return_value': response})):
                ctx.signatures.clear()
                result = agent_registry(query_tools=True).execute('aggregate_mantis_issues', aggregate_arguments(ctx), '', ctx)
            self.assertFalse(result['ok'], result)
            self.assertNotIn('totals', result)
            self.assertFalse(ctx.ledger.aggregation_results())

    def test_aggregation_requires_a_verified_contract_even_when_arguments_are_empty(self):
        ctx = context_for()
        result = agent_registry(query_tools=True).execute('aggregate_mantis_issues',
            {**arguments(ctx), 'group_by': ['customer']}, '', ctx)
        self.assertFalse(result['ok'])

    def test_period_aggregation_normal_route_renders_verified_table(self):
        from backend.reports.aggregation import aggregate_rows
        from backend.evaluation.verification import build_verification_report
        from backend.evaluation.delivery import decide_delivery
        q, plan = aggregation_plan()
        ctx = context_for(plan, q)
        expected = aggregate_rows([(None, None, None, {'id': 1, 'created_at': '2026-10-05', 'media_type': 'mts'})],
                                  plan['aggregation'], plan['resolved_constraints'])
        expected.update(source={'search_complete': True, 'fetchedAt': '2026-11-06T00:00:00Z'}, filter_audit={'fully_applied': True})
        model = FakeModel([turn(tool_call('aggregate_mantis_issues', aggregate_arguments(ctx))), final('999건')])
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              patch('backend.tools.mantis_aggregations.execute_aggregation', return_value=expected)):
            pipeline = explain_context(q, source='mantis', constraints=plan['resolved_constraints'])
            pipeline.intent_plan = plan
            pipeline.structured_tool_conditions = True
            result = run_agent_if_eligible(chat_request(q), pipeline)
        self.assertEqual(result['raw']['mode'], 'agent_aggregation_results')
        self.assertNotIn('999', result['content'])
        self.assertIn('| mts | 1 |', result['content'])
        self.assertEqual(result['agentReview']['retrievalOutcome'], 'sufficient_evidence')
        report = build_verification_report(q, result['content'], [], result['retrieval'], result['raw']['mode'])
        self.assertEqual(report['status'], 'pass', report)
        self.assertTrue(decide_delivery(report).allowed)
        tampered = build_verification_report(q, result['content'].replace('| mts | 1 |', '| mts | 99 |'), [], result['retrieval'], result['raw']['mode'])
        self.assertFalse(decide_delivery(tampered).allowed)

    def test_aggregation_period_is_applied_to_count_but_effort_does_not_change_date_basis(self):
        q, plan = aggregation_plan()
        ctx = context_for(plan, q)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()) as source:
            result = agent_registry(query_tools=True).execute('count_mantis_issues', arguments(ctx), '', ctx)
        self.assertTrue(result['ok'], result)
        self.assertIn({'field': 'created_at', 'operator': 'between', 'value': ['2026-10-01', '2026-10-31']},
                      source.call_args.args[1]['filter_clauses'])
        ctx.query_contracts['mantis'].aggregation['metrics'] = ['effort']
        ctx.signatures.clear()
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search') as source:
            result = agent_registry(query_tools=True).execute('count_mantis_issues', arguments(ctx), '', ctx)
        self.assertFalse(result['ok'])
        source.assert_not_called()

    def test_simple_aggregation_matches_all_ids_and_not_only_displayed_rows(self):
        from backend.evaluation.agent_queries import verify_agent_aggregation
        from backend.services.agent.query_answers import render_aggregation
        q = '전사 한투 미해결 이슈를 상태별로 몇 건인지 알려줘.'
        plan = validate_plan(q, {**deepcopy(PROPOSAL), 'operation': 'aggregate', 'group_by': 'status', 'limit': None}, {'uid': 'u'}, {})
        ctx = context_for(plan, q)
        stage = scanned()
        stage['issues_list'] = stage['issues_list'][:1]
        stage['meta'].update(group_counts={'신규': 3, '할당': 1}, matched_groups={41: '신규', 42: '신규', 43: '신규', 44: '할당'})
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage):
            result = agent_registry(query_tools=True).execute('aggregate_mantis_issues', aggregate_arguments(ctx), '', ctx)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['totals']['issue_count'], 4)
        content = render_aggregation(q, plan, ctx.ledger.aggregation_results()[0])['content']
        retrieval = ctx.ledger.as_retrieval_result(q, plan['resolved_constraints'], plan, 3)
        report = verify_agent_aggregation(q, content, retrieval, 'agent_aggregation_results')
        self.assertEqual(report['status'], 'pass', report)

    def test_model_cannot_finish_aggregation_after_only_counting(self):
        q, plan = aggregation_plan()
        ctx = context_for(plan, q)
        model = FakeModel([turn(tool_call('count_mantis_issues', arguments(ctx))), final('집계 완료'), final('집계 완료')])
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned())):
            pipeline = explain_context(q, source='mantis', constraints=plan['resolved_constraints'])
            pipeline.intent_plan = plan
            pipeline.structured_tool_conditions = True
            result = run_agent_if_eligible(chat_request(q), pipeline)
        self.assertEqual(result['agentReview']['retrievalOutcome'], 'partial_evidence')
        self.assertIn('집계 조회는 완료하지 못했습니다', result['content'])
        self.assertNotIn('집계 완료', result['content'])
