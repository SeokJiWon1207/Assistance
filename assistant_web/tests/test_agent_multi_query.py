"""독립 비교 범위의 조건·숫자·중복·미완료 경계."""
from copy import deepcopy
import calendar
from datetime import date, timedelta
import unittest
from unittest.mock import patch

from backend.services.agent.planning import PlanningSession
from test_tool_condition_arguments import PROPOSAL


CUSTOMERS = '전사 한투와 KB의 미해결 이슈 건수를 각각 조회해서 비교해줘.'


def customer_proposal(**changes):
    return {**deepcopy(PROPOSAL), 'operation': 'count', 'limit': None, 'filters': [],
            'comparison': {'axis': 'customer', 'cohorts': [
                {'value': '한투', 'evidence': '한투'}, {'value': 'KB', 'evidence': 'KB'}]}, **changes}


def period_proposal():
    today = date.today()
    start = today.replace(day=1)
    last = start - timedelta(days=1)
    return {**customer_proposal(), 'filters': deepcopy(PROPOSAL['filters']),
            'comparison': {'axis': 'created_at', 'cohorts': [
                {'value': [start.isoformat(), today.replace(day=calendar.monthrange(today.year, today.month)[1]).isoformat()], 'evidence': '이번 달'},
                {'value': [last.replace(day=1).isoformat(), last.isoformat()], 'evidence': '지난달'}]}}


def session(proposal=None, question=CUSTOMERS):
    s = PlanningSession(question, {'uid': 'u'}, {})
    result = s.submit(proposal=proposal or customer_proposal())
    return s, result


def comparison_context(proposal=None, question=CUSTOMERS):
    from test_tool_condition_arguments import context_for
    s, result = session(proposal, question)
    assert result['ok'], result
    plan = s.plan
    first = plan['query_scopes'][0]
    ctx = context_for(first['plan'], first['question'])
    ctx.question = question
    ctx.query_contracts = {}
    for scope in plan['query_scopes']:
        child = context_for(scope['plan'], scope['question'])
        contract = child.query_contracts['mantis']
        contract.scope_id = scope['id']
        ctx.query_scopes[scope['id']] = contract
    return plan, ctx


def scope_args(ctx, scope_id):
    contract = ctx.query_scopes[scope_id]
    return {'query_scope_id': scope_id, 'filters': deepcopy(contract.filters), 'scope': contract.scope}


def scan(ids, *, complete=True):
    return {'source': 'mantis_snapshot', 'status': 'ok',
            'issues_list': [{'id': i, 'summary': f'제목 {i}'} for i in reversed(ids)],
            'meta': {'search_complete': complete, 'found_count': len(ids), 'matched_issue_ids': ids,
                     'ordered_matched_ids': list(reversed(ids)), 'fetchedAt': '2026-11-06T00:00:00Z'}}


def evaluated(plan, ctx):
    from backend.evaluation.agent_comparison import verify_agent_comparison
    from backend.services.agent.query_comparison import render_comparison
    retrieval = ctx.ledger.as_retrieval_result(ctx.question, plan['resolved_constraints'], plan, 5)
    content, _, result = render_comparison(plan, retrieval['stages'])
    retrieval['comparison_result'] = result
    report = verify_agent_comparison(ctx.question, content, retrieval, 'agent_comparison_results')
    return content, retrieval, report


class MultiQueryBoundaryTests(unittest.TestCase):
    def setUp(self):
        from backend.tools.catalog import agent_registry
        self.registry = agent_registry(query_tools=True)
        self.plan, self.ctx = comparison_context()

    def call(self, scope_id, tool='count_mantis_issues', **extra):
        return self.registry.execute(tool, {**scope_args(self.ctx, scope_id), **extra}, '', self.ctx)

    def test_cross_scope_reference_filter_swap_and_scope_omission_cannot_execute(self):
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scan([41])) as source:
            first = self.call('S1')
            self.assertTrue(first['ok'], first)
            for args in [scope_args(self.ctx, 'S1') | {'query_scope_id': 'S2'},
                         {'condition_ref': first['condition_ref'], 'query_scope_id': 'S2'},
                         {'condition_ref': 'Q012345678901'}, {'filters': [], 'scope': 'all'},
                         scope_args(self.ctx, 'S2') | {'scope': 'team'},
                         scope_args(self.ctx, 'S2') | {'filters': self.ctx.query_scopes['S2'].filters[:1]}]:
                with self.subTest(args=args):
                    denied = self.registry.execute('count_mantis_issues', args, '', self.ctx)
                    self.assertFalse(denied['ok'], denied)
            self.assertEqual(source.call_count, 1)
        self.assertEqual(self.ctx.ledger.query_results(), [])
        self.assertEqual(len(self.ctx.ledger.query_results('S1')), 1)
        self.assertEqual(self.ctx.ledger.query_results('S2'), [])

    def test_overlap_is_calculated_from_all_ids_not_displayed_window(self):
        question = '전사 한투와 KB의 미해결 이슈를 각각 최근 50건 목록으로 비교해줘.'
        self.plan, self.ctx = comparison_context(customer_proposal(operation='search', limit=50), question)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scan(list(range(1, 71))), scan(list(range(50, 101)))]) as source:
            for scope_id in ('S1', 'S2'):
                counted = self.call(scope_id)
                listed = self.registry.execute('list_mantis_issues', {'condition_ref': counted['condition_ref'], 'limit': 50}, '', self.ctx)
                self.assertTrue(listed['ok'], listed)
            self.assertEqual(source.call_count, 2)
        content, retrieval, report = evaluated(self.plan, self.ctx)
        self.assertEqual(report['status'], 'pass', report)
        result = retrieval['comparison_result']
        self.assertEqual((result['arithmetic_total'], result['union_count'], result['duplicate_memberships']), (121, 100, 21))
        self.assertEqual(result['differences'][0]['value'], 19)
        self.assertIn('50. #21', content)
        self.assertIn('50. #51', content)
        self.assertNotIn('51. #', content)

    def test_complete_zero_partial_zero_and_missing_scope_are_distinct(self):
        from backend.services.agent.query_comparison import comparison_outcome
        from backend.evaluation.delivery import decide_delivery
        for variant in ('complete', 'partial', 'missing'):
            with self.subTest(variant=variant):
                self.plan, self.ctx = comparison_context()
                with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scan([]), scan([], complete=variant != 'partial')]):
                    self.call('S1')
                    if variant != 'missing':
                        self.call('S2')
                content, retrieval, report = evaluated(self.plan, self.ctx)
                self.assertTrue(decide_delivery(report).allowed, report)
                outcome = comparison_outcome(self.plan, retrieval['stages'])
                if variant == 'complete':
                    self.assertEqual(outcome, 'complete_empty')
                    self.assertEqual(report['status'], 'pass')
                    self.assertIn('중복 제외 합계: 0건', content)
                else:
                    self.assertEqual(report['status'], 'warning')
                    self.assertNotEqual(outcome, 'complete_empty')
                    self.assertIn('| KB | 미확정 | 조회 미완료 |', content)
                    self.assertNotIn('중복 제외 합계:', content)
                    self.assertFalse(retrieval['comparison_result']['differences'])

    def test_numeric_scope_id_source_set_and_snapshot_drift_block_delivery(self):
        from backend.evaluation.agent_comparison import verify_agent_comparison
        from backend.evaluation.delivery import decide_delivery
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scan([1, 2, 3]), scan([4])]):
            self.call('S1'); self.call('S2')
        content, retrieval, report = evaluated(self.plan, self.ctx)
        self.assertEqual(report['status'], 'pass', report)
        for kind in ('answer', 'computed', 'scope', 'snapshot', 'ids', 'contract'):
            altered = deepcopy(retrieval)
            text = content
            if kind == 'answer': text = content.replace('+2건', '+999건')
            elif kind == 'computed': altered['comparison_result']['union_count'] = 999
            elif kind == 'scope': altered['stages'][0]['meta']['condition_step']['query_scope_id'] = 'S2'
            elif kind == 'snapshot': altered['stages'][0]['meta']['fetchedAt'] = '2026-10-01T00:00:00Z'
            elif kind == 'ids': altered['stages'][0]['meta']['matched_issue_ids'] = [1]
            else: altered['query_plan']['query_scopes'][1]['plan']['filter_clauses'] = []
            with self.subTest(kind=kind):
                checked = verify_agent_comparison(CUSTOMERS, text, altered, 'agent_comparison_results')
                self.assertFalse(decide_delivery(checked).allowed, checked)

    def test_investigation_does_not_replace_scope_counts(self):
        from backend.evaluation.agent_steps import verify_condition_steps
        self.plan, self.ctx = comparison_context(customer_proposal(operation='search', limit=1),
            '전사 한투와 KB의 미해결 이슈를 각각 최근 1건 목록으로 비교해줘.')
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[scan([1, 2]), scan([3]), scan([2])]):
            self.call('S1', 'list_mantis_issues', limit=1)
            self.call('S2', 'list_mantis_issues', limit=1)
            args = scope_args(self.ctx, 'S1')
            args['filters'].append({'field': 'issue_id', 'operator': 'equals', 'value': 2, 'evidence_ref': 'M#2'})
            narrowed = self.registry.execute('count_mantis_issues', args | {'purpose': 'investigate'}, '', self.ctx)
            self.assertTrue(narrowed['ok'], narrowed)
        content, _, report = evaluated(self.plan, self.ctx)
        self.assertEqual(report['status'], 'pass', report)
        self.assertIn('| 한투 | 2 |', content)
        self.assertEqual(narrowed['condition_step']['query_scope_id'], 'S1')
        self.assertEqual(verify_condition_steps(self.ctx.ledger, self.ctx.condition_refs)['status'], 'pass')

    def test_each_scope_grouping_is_verified_and_missing_groups_stay_incomplete(self):
        question = '전사 한투와 KB의 미해결 이슈를 상태별 건수로 비교해줘.'
        self.plan, self.ctx = comparison_context(customer_proposal(operation='aggregate', group_by='status'), question)
        first, second = scan([1, 2, 3]), scan([4])
        first['meta'].update(group_counts={'할당': 2, '신규': 1}, matched_groups={'1': '할당', '2': '할당', '3': '신규'})
        second['meta'].update(group_counts={'할당': 1}, matched_groups={'4': '할당'})
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=[first, second]):
            self.call('S1'); self.call('S2')
            self.call('S1', 'aggregate_mantis_issues', group_by=['status'])
            content, _, report = evaluated(self.plan, self.ctx)
            self.assertIn('집계 미완료', content)
            self.assertEqual(report['status'], 'warning', report)
            self.call('S2', 'aggregate_mantis_issues', group_by=['status'])
        content, _, report = evaluated(self.plan, self.ctx)
        self.assertEqual(report['status'], 'pass', report)
        self.assertIn('| 할당 | 2 |', content)
        self.assertIn('| 할당 | 1 |', content)

    def test_comparison_followup_cannot_inherit_an_unscoped_population(self):
        followup = {**deepcopy(PROPOSAL), 'followup': True, 'scope': 'inherit', 'filters': [],
                    'status': {'mode': 'inherit', 'values': [], 'evidence': ''}}
        s = PlanningSession('같은 조건으로 다시 조회해줘.', {'uid': 'u'}, self.plan | {'audit_id': 'a' * 32})
        self.assertFalse(s.submit(proposal=followup)['ok'])
        self.assertIsNone(s.plan)

    def test_ambiguous_ordinal_after_two_lists_never_resolves_flattened_sources(self):
        from agent_harness_cases import chat_request
        from backend.services.unified_planning import prepare_context
        q = '그중 두 번째 이슈 번호와 제목만 다시 알려줘.'
        request = chat_request(q)
        request.contextAuditId = 'a' * 32
        request.messages = [type(request.messages[0])(role='user', content=CUSTOMERS), *request.messages]
        with (patch('backend.services.agent.policy.active_agent_policy', return_value=object()),
              patch('backend.services.unified_planning.load_audit_query_plan', return_value=self.plan),
              patch('backend.services.conversation_context.build_conversation_context', return_value={}),
              patch('backend.services.unified_planning.resolve_reference_from_context', return_value='0000002') as resolver):
            ctx = prepare_context(request, q, {'uid': 'u'}, {'id': 'test'}, 'key', [], [])
        resolver.assert_not_called()
        self.assertEqual(ctx.intent_plan['operation'], 'clarify')
        self.assertIsNone(ctx.ordinal_source_id)


class MultiQueryPlanningTests(unittest.TestCase):
    def test_null_comparison_keeps_existing_compound_question_on_single_scope(self):
        question = '전사 범위에서 한투 미해결 이슈가 몇 건이야? 최근 3건을 보여주고, 두 번째 이슈와 관련된 Notion 가이드도 설명해줘.'
        proposal = {**deepcopy(PROPOSAL), 'source': 'both', 'operation': 'explain', 'comparison': None}
        s = PlanningSession(question, {'uid': 'u'}, {}, ordinal_guide_tools=True)
        result = s.submit(proposal=proposal)
        self.assertTrue(result['ok'], result)
        self.assertEqual(s.plan['operation'], 'explain')
        self.assertFalse(s.plan.get('query_scopes'))

    def test_a_proposed_comparison_cannot_be_discarded_during_repair(self):
        s, result = session(customer_proposal(comparison={'axis': 'customer', 'cohorts': []}))
        self.assertFalse(result['ok'])
        self.assertFalse(s.submit(changes={'comparison': None})['ok'])
        self.assertIsNone(s.plan)

    def test_period_comparison_repair_names_the_conflicting_aggregation_fields(self):
        p = period_proposal()
        p.update(operation='aggregate', filters=[], aggregation={
            'group_by': [], 'metrics': ['issue_count'], 'period': None,
            'comparison': None, 'top_n': 10, 'explain': True})
        s, result = session(p, '전사 한투 미해결 이슈를 이번 달과 지난달 등록 건수로 비교해줘.')
        self.assertFalse(result['ok'])
        self.assertIn('"aggregation":null', result['error']['message'])
        self.assertIn('"operation":"count"', result['error']['message'])
        fixed = s.submit(changes={'aggregation': None, 'operation': 'count',
                                  'filters': deepcopy(PROPOSAL['filters'])})
        self.assertTrue(fixed['ok'], fixed)
        self.assertTrue(all(s['plan']['resolved_constraints']['statuses'] == ['open'] for s in s.plan['query_scopes']))

    def test_comparison_aggregate_requires_an_executable_group(self):
        _, result = session(customer_proposal(operation='aggregate', group_by=None))
        self.assertFalse(result['ok'], result)

    def test_comparison_routing_fixtures_validate_each_scope(self):
        import json
        from pathlib import Path
        cases = [c for c in map(json.loads, (Path(__file__).parent / 'fixtures/routing_cases.jsonl').read_text().splitlines())
                 if c.get('agent_comparison')]
        self.assertEqual(len(cases), 2)
        for case in cases:
            s, result = session(case['agent_comparison'], case['q'])
            self.assertTrue(result['ok'], result)
            self.assertEqual(len(s.plan['query_scopes']), 2)
            self.assertTrue(all(x['plan']['resolved_constraints']['statuses'] == ['open'] for x in s.plan['query_scopes']))

    def test_relative_current_month_accepts_today_as_existing_calendar_validator_does(self):
        p = period_proposal()
        p['comparison']['cohorts'][0]['value'][1] = date.today().isoformat()
        _, result = session(p, '전사 한투 미해결 이슈를 이번 달과 지난달 등록 건수로 비교해줘.')
        self.assertTrue(result['ok'], result)

    def test_customer_scopes_are_independent_and_preserve_open(self):
        s, result = session()
        self.assertTrue(result['ok'], result)
        scopes = s.plan.get('query_scopes') or []
        self.assertEqual(len(scopes), 2)
        self.assertEqual([c['plan']['resolved_constraints']['customer_key'] for c in scopes], ['한투', 'kb'])
        self.assertTrue(all(c['plan']['resolved_constraints']['statuses'] == ['open'] for c in scopes))
        self.assertFalse(s.plan['resolved_constraints']['customer_key'])

    def test_each_period_gets_its_own_validated_calendar_range(self):
        proposal = period_proposal()
        s, result = session(proposal, '전사 한투 미해결 이슈를 이번 달과 지난달 등록 건수로 비교해줘.')
        self.assertTrue(result['ok'], result)
        scopes = s.plan.get('query_scopes') or []
        self.assertEqual(len(scopes), 2)
        self.assertEqual([c['plan']['resolved_constraints']['created_at'] for c in scopes],
            [{'from': x['value'][0], 'to': x['value'][1]} for x in proposal['comparison']['cohorts']])
        self.assertTrue(all(c['plan']['resolved_constraints']['customer_key'] == '한투' for c in scopes))

    def test_invalid_comparison_cannot_silently_turn_into_a_single_query(self):
        for mutation in ('missing', 'duplicate', 'invented', 'foreign_evidence', 'status_axis', 'source', 'conflict', 'fifth'):
            p = customer_proposal()
            c = p['comparison']
            if mutation == 'missing': c['cohorts'] = c['cohorts'][:1]
            elif mutation == 'duplicate': c['cohorts'][1] = deepcopy(c['cohorts'][0])
            elif mutation == 'invented': c['cohorts'][1] = {'value': '신한', 'evidence': 'KB'}
            elif mutation == 'foreign_evidence': c['cohorts'][1]['evidence'] = '없는 구절'
            elif mutation == 'status_axis': c['axis'] = 'status'
            elif mutation == 'source': p['source'] = 'notion'
            elif mutation == 'conflict': p['filters'] = deepcopy(PROPOSAL['filters'])
            else: c['cohorts'] *= 3
            with self.subTest(mutation=mutation):
                s, result = session(p)
                self.assertFalse(result['ok'], result)
                self.assertIsNone(s.plan)

    def test_omitting_a_named_customer_is_rejected_even_when_two_other_scopes_exist(self):
        _, result = session(question='전사 한투와 KB와 신한의 미해결 이슈 건수를 비교해줘.')
        self.assertFalse(result['ok'], result)

    def test_wrong_relative_dates_and_missing_open_are_rejected(self):
        p = period_proposal()
        p['comparison']['cohorts'][0]['value'][0] = '2001-01-01'
        _, result = session(p, '전사 한투 미해결 이슈를 이번 달과 지난달 등록 건수로 비교해줘.')
        self.assertFalse(result['ok'], result)
        _, result = session(customer_proposal(status={'mode': 'unspecified', 'values': [], 'evidence': ''}))
        self.assertFalse(result['ok'], result)

    def test_partial_repair_preserves_shared_conditions(self):
        s, result = session(customer_proposal(comparison={'axis': 'customer', 'cohorts': [{'value': '한투', 'evidence': '한투'}]}))
        self.assertFalse(result['ok'], result)
        fixed = s.submit(changes={'comparison': customer_proposal()['comparison']})
        self.assertTrue(fixed['ok'], fixed)
        self.assertEqual([c['plan']['resolved_constraints']['statuses'] for c in s.plan['query_scopes']], [['open'], ['open']])


class MultiQueryExecutionTests(unittest.TestCase):
    def test_model_can_choose_each_scope_and_server_computes_differences(self):
        from agent_harness_cases import FakeModel, FakeSources, agent_environment, chat_request, final, tool_call, turn
        from backend.services.chat_service import ChatService
        from test_tool_condition_arguments import scanned
        first, second = scanned(), scanned()
        second['meta'].update(found_count=2, matched_issue_ids=[80, 81], ordered_matched_ids=[81, 80])
        second['issues_list'] = [{'id': 81, 'summary': 'KB 81'}, {'id': 80, 'summary': 'KB 80'}]
        seen = []
        def source(limit, constraints, **kwargs):
            seen.append(constraints['customer_key'])
            return first if constraints['customer_key'] == '한투' else second
        def filters(name):
            return [{'field': 'customer', 'operator': 'equals', 'value': name}, {'field': 'status', 'operator': 'in', 'value': ['open']}]
        model = FakeModel([turn(tool_call('count_mantis_issues', {'query_scope_id': 'S2', 'filters': filters('KB'), 'scope': 'all'})),
            turn(tool_call('count_mantis_issues', {'query_scope_id': 'S1', 'filters': filters('한투'), 'scope': 'all'})),
            final('한투가 999건 많습니다.')], planner=customer_proposal())
        with (agent_environment(model=model, sources=FakeSources(), extra_env={'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=source)):
            result = ChatService().build_response(chat_request(CUSTOMERS, tools={'mantis': True, 'notion': False, 'vectorDb': False}), user={'uid': 'multi'})
        self.assertEqual(result['raw']['mode'], 'agent_comparison_results', result['content'])
        self.assertEqual(seen, ['kb', '한투'])
        self.assertIn('| 한투 | 4 |', result['content'])
        self.assertIn('| KB | 2 |', result['content'])
        self.assertIn('한투 − KB: +2건', result['content'])
        self.assertIn('중복 제외 합계: 6건', result['content'])
        self.assertNotIn('999', result['content'])
        self.assertEqual(result['verification']['delivery'], 'allowed')
