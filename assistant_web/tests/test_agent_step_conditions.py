"""조회 단계별 조건·근거·수치 모집단 경계 회귀."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend.evaluation.agent_queries import verify_query_results
from backend.tools.catalog import agent_registry
from test_tool_condition_arguments import arguments, context_for, scanned


def focus(ctx, identifier=43, **extra):
    return {**arguments(ctx), 'purpose': 'investigate', 'filters': [*arguments(ctx)['filters'],
        {'field': 'issue_id', 'operator': 'equals', 'value': identifier, 'evidence_ref': f'M#{identifier}'}], **extra}


def narrowed():
    stage = scanned()
    stage['issues_list'] = [{'id': 43, 'summary': '제목 43'}]
    stage['meta'].update(found_count=1, matched_issue_ids=[43], ordered_matched_ids=[43])
    return stage


class AgentStepConditionTests(unittest.TestCase):
    def setUp(self):
        self.ctx = context_for()
        self.registry = agent_registry(query_tools=True)

    def call(self, name, args):
        return self.registry.execute(name, args, '', self.ctx)

    def seed(self):
        return self.call('list_mantis_issues', {**arguments(self.ctx), 'limit': 3})

    def test_model_focus_has_own_reference_scan_and_preserves_answer_population(self):
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scanned(), narrowed()]) as scan:
            root = self.seed()
            step = self.call('count_mantis_issues', focus(self.ctx))
            self.assertTrue(step['ok'], step)
            again = self.call('list_mantis_issues', {'condition_ref': step['condition_ref'], 'limit': 1})
        self.assertTrue(again['ok'], again)
        self.assertNotEqual(root['condition_ref'], step['condition_ref'])
        self.assertEqual(step['total_count'], 1)
        self.assertEqual(again['ordered_ids'], [43])
        self.assertEqual(scan.call_count, 2)
        self.assertEqual([r['total_count'] for r in self.ctx.ledger.query_results()], [4])
        text = '적용 조건에 맞는 Mantis 이슈는 4건입니다.\n1. #44 제목 44\n2. #43 제목 43\n3. #42 제목 42'
        stages = [s.stage for s in self.ctx.ledger.stages]
        self.assertEqual(verify_query_results(text, stages, 3)['status'], 'pass')
        self.assertEqual(step['condition_step']['purpose'], 'investigate')
        self.assertIn({'field': 'issue_id', 'origin': 'ledger', 'evidence_ref': 'M#43'}, step['argument_validation']['origins'])

    def test_narrower_result_cannot_be_labelled_as_original_answer(self):
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()) as scan:
            self.seed()
            bad = self.call('count_mantis_issues', focus(self.ctx, purpose='answer'))
        self.assertFalse(bad['ok'], bad)
        self.assertEqual(scan.call_count, 1)

    def test_missing_required_status_and_foreign_or_invented_evidence_never_reach_source(self):
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()) as scan:
            self.seed()
            for mutation in ('status', 'unknown', 'wrong_field', 'negative', 'wrong_source'):
                args = focus(self.ctx)
                if mutation == 'status':
                    args['filters'] = [f for f in args['filters'] if f['field'] != 'status']
                elif mutation == 'unknown':
                    args['filters'][-1]['value'] = 999
                    args['filters'][-1]['evidence_ref'] = 'M#999'
                elif mutation == 'wrong_field':
                    args['filters'][-1]['field'] = 'screen_number'
                elif mutation == 'negative':
                    args['filters'][-1]['operator'] = 'not_equals'
                else:
                    args['filters'][-1]['evidence_ref'] = 'N1'
                with self.subTest(mutation=mutation):
                    result = self.call('count_mantis_issues', args)
                    self.assertFalse(result['ok'], result)
        self.assertEqual(scan.call_count, 1)

    def test_focus_reference_cannot_be_promoted_or_reused_by_another_request_or_source(self):
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scanned(), narrowed()]):
            self.seed()
            step = self.call('count_mantis_issues', focus(self.ctx))
        self.assertTrue(step['ok'], step)
        for args in ({'condition_ref': step['condition_ref'], 'purpose': 'answer'},
                     {'condition_ref': step['condition_ref'], 'filters': []}):
            self.assertFalse(self.call('count_mantis_issues', args)['ok'])
        self.assertFalse(self.registry.execute('count_mantis_issues', {'condition_ref': step['condition_ref']}, '', context_for())['ok'])
        self.assertFalse(self.call('search_notion_pages', {'query': '가이드', 'condition_ref': step['condition_ref']})['ok'])

    def test_selected_ordinal_requires_actual_list_and_cannot_focus_another_issue(self):
        self.ctx.related_ordinal = 2
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scanned(), narrowed()]) as scan:
            self.assertFalse(self.call('count_mantis_issues', focus(self.ctx))['ok'])
            self.seed()
            self.assertFalse(self.call('count_mantis_issues', focus(self.ctx, 44))['ok'])
            self.assertTrue(self.call('count_mantis_issues', focus(self.ctx))['ok'])
        self.assertEqual(scan.call_count, 2)

    def test_different_sort_and_window_get_separate_cache(self):
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()) as scan:
            self.seed()
            step = self.call('list_mantis_issues', {**arguments(self.ctx), 'purpose': 'investigate',
                'sort': {'field': 'created_at', 'direction': 'asc'}, 'limit': 30})
        self.assertTrue(step['ok'], step)
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(scan.call_args.args[0], 30)
        self.assertEqual(scan.call_args.args[1]['sort']['direction'], 'asc')

    def test_investigation_zero_or_partial_does_not_prove_request_empty(self):
        for complete in (True, False):
            stage = scanned()
            stage['issues_list'] = []
            stage['meta'].update(found_count=0, matched_issue_ids=[], ordered_matched_ids=[], search_complete=complete)
            ctx = context_for()
            with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage):
                result = self.registry.execute('count_mantis_issues', {**arguments(ctx), 'purpose': 'investigate'}, '', ctx)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['total_count'], 0 if complete else None)
            self.assertFalse(ctx.ledger.query_results())
            self.assertNotEqual(ctx.ledger.query_outcome(['mantis']), 'complete_empty')

    def test_question_grounding_cannot_be_fabricated(self):
        args = {**arguments(self.ctx), 'purpose': 'investigate'}
        args['filters'].append({'field': 'title', 'operator': 'contains', 'value': '임의제목', 'evidence': '임의제목'})
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search') as scan:
            self.assertFalse(self.call('count_mantis_issues', args)['ok'])
        scan.assert_not_called()

    def test_stage_metadata_does_not_mutate_cached_snapshot(self):
        original = scanned()
        untouched = deepcopy(original)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=original):
            self.seed()
        self.assertEqual(original, untouched)

    def test_step_numbers_and_scope_tampering_block_delivery(self):
        from backend.evaluation.agent_steps import verify_condition_steps
        from backend.evaluation.delivery import decide_delivery
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scanned(), narrowed()]):
            self.seed()
            self.call('count_mantis_issues', focus(self.ctx))
        contracts = {**{c.ref: c for c in self.ctx.query_contracts.values()}, **self.ctx.condition_refs}
        self.assertEqual(verify_condition_steps(self.ctx.ledger, contracts)['status'], 'pass')
        for corruption in ('total', 'purpose', 'filter', 'promotion', 'missing_step'):
            ledger = deepcopy(self.ctx.ledger)
            meta = ledger.stages[-1].stage['meta']
            if corruption == 'total':
                meta['investigation_query_result']['total_count'] = 0
            elif corruption == 'purpose':
                meta['condition_step']['purpose'] = 'answer'
            elif corruption == 'filter':
                meta['investigation_query_result']['applied_filters'] = []
            elif corruption == 'promotion':
                meta['query_result'] = meta.pop('investigation_query_result')
            else:
                meta.pop('condition_step')
            check = verify_condition_steps(ledger, contracts)
            self.assertEqual(check['status'], 'fail', corruption)
            self.assertFalse(decide_delivery({'checks': [check]}).allowed)

    def test_normal_agent_keeps_original_numbers_after_focused_count(self):
        from agent_harness_cases import FakeModel, FakeSources, agent_environment, chat_request, explain_context, final, tool_call, turn
        from backend.intent.unified_validation import validate_plan
        from backend.services.agent.policy import request_transfer_scope
        from backend.services.agent.route import run_agent_if_eligible
        from test_tool_condition_arguments import QUESTION, PROPOSAL
        plan = validate_plan(QUESTION, deepcopy(PROPOSAL), {'uid': 'u'}, {})
        model = FakeModel([turn(tool_call('list_mantis_issues', {**arguments(self.ctx), 'limit': 3})),
            turn(tool_call('count_mantis_issues', focus(self.ctx))), final('전체 1건입니다.')])
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scanned(), narrowed()])):
            pipeline = explain_context(QUESTION, source='mantis', constraints=plan['resolved_constraints'])
            pipeline.intent_plan = plan
            pipeline.structured_tool_conditions = True
            result = run_agent_if_eligible(chat_request(QUESTION), pipeline)
        self.assertIn('이슈는 4건입니다.', result['content'])
        self.assertNotIn('전체 1건', result['content'])
        self.assertEqual([s['issue_id'] for s in result['sourcesForFollowUps']], [44, 43, 42])
        self.assertEqual(next(c for c in result['agentChecks'] if c['id'] == 'agent_step_conditions')['status'], 'pass')
        self.assertEqual([s['purpose'] for s in result['agentReview']['conditionSteps']], ['answer', 'investigate'])

    def test_focused_aggregation_does_not_replace_requested_groups(self):
        from backend.intent.unified_validation import validate_plan
        from backend.evaluation.agent_steps import verify_condition_steps
        from test_tool_condition_arguments import PROPOSAL
        from test_agent_query_primary import aggregate_arguments
        q = '전사 한투 미해결 이슈를 상태별로 몇 건인지 알려줘.'
        plan = validate_plan(q, {**deepcopy(PROPOSAL), 'operation': 'aggregate', 'group_by': 'status', 'limit': None}, {'uid': 'u'}, {})
        self.ctx = context_for(plan, q)
        all_rows = scanned()
        all_rows['meta'].update(group_counts={'신규': 3, '할당': 1}, matched_groups={41: '신규', 42: '신규', 43: '신규', 44: '할당'})
        subset = narrowed()
        subset['meta'].update(group_counts={'신규': 1}, matched_groups={43: '신규'})
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[all_rows, subset]) as scan:
            self.seed()
            root = self.call('aggregate_mantis_issues', aggregate_arguments(self.ctx))
            sub = self.call('aggregate_mantis_issues', {**aggregate_arguments(self.ctx), **focus(self.ctx)})
        self.assertTrue(root['ok'], root)
        self.assertTrue(sub['ok'], sub)
        self.assertEqual(sub['totals'], {'issue_count': 1})
        self.assertEqual([r['total_count'] for r in self.ctx.ledger.aggregation_results()], [4])
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(verify_condition_steps(self.ctx.ledger, self.ctx.condition_refs)['status'], 'pass')

    def test_focused_detail_cannot_reuse_a_cached_row_that_fails_its_new_filter(self):
        self.ctx.ledger.add('mantis', {'source': 'mantis_explanation_focus', 'status': 'ok',
            'issues_list': [{'id': 43, 'summary': '제목 43', 'answer_evidence': {'complete': True}}]},
            grade='scoped', unapplied=(), call_id='seed')
        with patch('backend.tools.mantis_tools.execute_mantis_id_lookup', return_value={
                'source': 'mantis', 'status': 'ok', 'issues_list': [], 'meta': {'missing_ids': []}}) as read:
            result = self.call('get_mantis_issue_detail', {**focus(self.ctx), 'issue_id': 43})
        self.assertFalse(result['ok'], result)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args[1]['filter_clauses'][-1]['value'], '43')

    def test_investigation_reference_must_match_detail_target(self):
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scanned(), narrowed()]):
            self.seed()
            result = self.call('count_mantis_issues', focus(self.ctx))
        with patch('backend.tools.mantis_tools.execute_mantis_id_lookup') as read:
            denied = self.call('get_mantis_issue_detail', {'condition_ref': result['condition_ref'], 'issue_id': 44})
        self.assertFalse(denied['ok'], denied)
        read.assert_not_called()

    def test_common_notion_search_and_page_read_keep_same_reference_conditions(self):
        from backend.tools import notion_tools
        self.ctx.related_ordinal = 2
        stage = scanned()
        stage['issues_list'][1]['summary'] = '비교차트 업데이트'
        page = {'id': 'guide', 'database_id': 'db', 'title': '비교차트 가이드', 'content': '',
                'content_incomplete': True}
        body = {**page, 'content': '비교차트에서 종목 추가 버튼을 선택합니다.', '_verified_body': True,
                'content_incomplete': False, '_verified_page_links': []}
        with (patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage),
              patch.object(notion_tools, 'execute_notion_body_search', return_value={
                  'source': 'notion', 'status': 'ok', 'notion_pages': [page],
                  'meta': {'coverage': 'top_k', 'search_complete': False}}),
              patch.object(notion_tools, 'notion_scope', return_value={'db': 'db'}),
              patch.object(notion_tools, 'read_current_page', return_value=body)):
            self.seed()
            found = self.call('search_notion_body', {'query': '비교차트 사용 방법', 'reference_scope': 'common'})
            self.assertTrue(found['ok'], found)
            read = self.call('get_notion_page_content', {'page_ref': 'N1'})
        self.assertTrue(read['ok'], read)
        self.assertEqual(found['condition_ref'], read['condition_ref'])
        for result in (found, read):
            self.assertEqual(result['condition_step']['evidence_grade'], 'reference')
            self.assertIn('고객사', result['condition_step']['unapplied_conditions'])
            self.assertIn('상태', result['condition_step']['unapplied_conditions'])
            self.assertEqual(result['condition_step']['filters'], [])
        self.assertEqual(self.ctx.ledger.query_results()[0]['total_count'], 4)

    def test_period_remains_on_focused_count_and_numerical_check(self):
        from test_agent_query_primary import aggregation_plan
        from backend.evaluation.agent_steps import verify_condition_steps
        q, plan = aggregation_plan()
        self.ctx = context_for(plan, q)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scanned(), narrowed()]) as scan:
            self.seed()
            result = self.call('count_mantis_issues', focus(self.ctx))
        self.assertTrue(result['ok'], result)
        self.assertIn({'field': 'created_at', 'operator': 'between', 'value': ['2026-10-01', '2026-10-31']},
                      scan.call_args.args[1]['filter_clauses'])
        self.assertEqual(verify_condition_steps(self.ctx.ledger, self.ctx.condition_refs)['status'], 'pass')

    def test_aggregate_group_can_ground_a_drilldown_but_arbitrary_groups_cannot(self):
        from backend.intent.unified_validation import validate_plan
        from test_tool_condition_arguments import PROPOSAL
        from test_agent_query_primary import aggregate_arguments
        q = '전사 한투 미해결 이슈를 상태별로 몇 건인지 알려줘.'
        plan = validate_plan(q, {**deepcopy(PROPOSAL), 'operation': 'aggregate', 'group_by': 'status', 'limit': None}, {'uid': 'u'}, {})
        self.ctx = context_for(plan, q)
        stage = scanned()
        stage['meta'].update(group_counts={'신규': 3, '할당된 이슈': 1},
            matched_groups={41: '신규', 42: '신규', 43: '신규', 44: '할당된 이슈'})
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[stage, narrowed()]) as scan:
            grouped = self.call('aggregate_mantis_issues', aggregate_arguments(self.ctx))
            args = {**arguments(self.ctx), 'purpose': 'investigate'}
            args['filters'].append({'field': 'status', 'operator': 'equals', 'value': 'assigned',
                                    'evidence_ref': grouped['condition_ref']})
            good = self.call('count_mantis_issues', args)
            self.assertTrue(good['ok'], good)
            for field, value in [('status', 'closed'), ('owner', 'assigned')]:
                bad = deepcopy(args)
                bad['filters'][-1].update(field=field, value=value)
                self.assertFalse(self.call('count_mantis_issues', bad)['ok'])
        self.assertEqual(scan.call_count, 2)
        self.assertEqual(self.ctx.ledger.aggregation_results()[0]['total_count'], 4)

    def test_routing_fixtures_keep_required_filters_for_each_step(self):
        import json
        from pathlib import Path
        from backend.intent.unified_validation import validate_plan
        from test_tool_condition_arguments import PROPOSAL
        cases = [c for c in map(json.loads, (Path(__file__).parent / 'fixtures/routing_cases.jsonl').read_text().splitlines())
                 if c.get('agent_step_required_fields')]
        self.assertEqual(len(cases), 3)
        for case in cases:
            proposal = {**deepcopy(PROPOSAL), 'source': 'both' if 'Notion' in case['q'] else 'mantis',
                        'operation': 'explain' if 'Notion' in case['q'] else 'aggregate',
                        'group_by': None if 'Notion' in case['q'] else 'status'}
            self.ctx = context_for(validate_plan(case['q'], proposal, {'uid': 'u'}, {}), case['q'])
            for field in case['agent_step_required_fields']:
                args = {**arguments(self.ctx), 'purpose': 'investigate'}
                args['filters'] = [c for c in args['filters'] if c['field'] != field]
                with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search') as scan:
                    self.assertFalse(self.call('count_mantis_issues', args)['ok'])
                    scan.assert_not_called()

