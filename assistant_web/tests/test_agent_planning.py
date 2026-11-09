"""에이전트의 조건 확정·부분 보정과 조회 전 검증 경계."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from agent_harness_cases import FakeModel, FakeSources, agent_environment, chat_request, final, tool_call, turn
from backend.services.agent.policy import request_transfer_scope
from backend.services.agent.route import run_agent_if_eligible
from backend.services.unified_planning import prepare_context
from test_tool_condition_arguments import PROPOSAL, scanned


QUESTION = '2026년 10월 전사 한투 미해결 이슈를 매체별로 집계해줘. 표만 보여줘.'
STATUS_QUESTION = '전사 한투 미해결 이슈를 상태별로 몇 건인지 알려줘.'


def conflicting_proposal():
    return {**deepcopy(PROPOSAL), 'operation': 'aggregate', 'limit': None, 'group_by': 'platform',
            'aggregation': {'group_by': ['platform'], 'metrics': ['issue_count'],
                'period': {'from': '2026-10-01', 'to': '2026-10-31', 'evidence': '2026년 10월'},
                'comparison': None, 'top_n': 10, 'explain': False}}


def periodless_status_proposal():
    proposal = conflicting_proposal()
    proposal['group_by'] = None
    proposal['aggregation']['group_by'] = ['status']
    proposal['aggregation']['period'] = None
    return proposal


def prepared(question, *, tools=None):
    from backend import llm
    request = chat_request(question, **({'tools': tools} if tools is not None else {}))
    ctx = prepare_context(request, question, {'uid': 'u'}, llm.get_private_model('company-test'),
                          'company-key', [m.model_dump() for m in request.messages], [])
    return request, ctx


class AgentPlanningTests(unittest.TestCase):
    def test_preparation_routing_fixtures_preserve_or_reject_conditions(self):
        import json
        from pathlib import Path
        from backend.services.agent.planning import PlanningSession
        for case in map(json.loads, (Path(__file__).parent / 'fixtures/routing_cases.jsonl').read_text().splitlines()):
            if 'agent_preparation' not in case:
                continue
            session = PlanningSession(case['q'], {}, {})
            session.submit(proposal=case['agent_preparation'])
            for changes in case['agent_changes']:
                result = session.submit(changes=changes)
            if 'agent_rejected' in case:
                self.assertFalse(result['ok'])
                self.assertIn(case['agent_rejected'], result['error']['message'])
                self.assertIsNone(session.plan)
            else:
                self.assertTrue(result['ok'], result)
                self.assertEqual(session.plan['resolved_constraints']['statuses'], case['agent_expected_statuses'])
                groups = (session.plan.get('aggregation') or {}).get('group_by', [session.plan['group_by']])
                self.assertEqual(groups, case['agent_expected_group'])

    def test_period_repair_cannot_accept_an_aggregate_without_its_group(self):
        from backend.services.agent.planning import PlanningSession
        periods = [None, {'from': '2026-11-01', 'to': '2026-11-09', 'evidence': ''}]
        for period in periods:
            with self.subTest(period=period):
                proposal = periodless_status_proposal()
                proposal['aggregation']['period'] = period
                session = PlanningSession(STATUS_QUESTION, {}, {})
                rejected = session.submit(proposal=proposal)
                self.assertFalse(rejected['ok'])
                self.assertIn('집계 기간', rejected['error']['message'])
                missing = session.submit(changes={'aggregation': None})
                self.assertFalse(missing['ok'], missing)
                self.assertIn('집계 기준', missing['error']['message'])
                self.assertIsNone(session.plan)
                self.assertEqual(session.candidate, proposal)
                fixed = session.submit(changes={'aggregation': None, 'group_by': 'status'})
                self.assertTrue(fixed['ok'], fixed)
                self.assertEqual(session.plan['group_by'], 'status')
                self.assertFalse(session.plan.get('aggregation'))
                constraints = session.plan['resolved_constraints']
                self.assertEqual(constraints['statuses'], ['open'])
                self.assertEqual(constraints['customer_key'], '한투')
                self.assertEqual(constraints['mantis_team_scope'], 'all')
                self.assertFalse(constraints.get('created_at'))

    def test_empty_aggregate_is_rejected_at_preparation_and_execution(self):
        from backend.intent.execution_contract import validate_execution_contract
        from backend.intent.unified_validation import validate_plan
        from backend.services.agent.planning import PlanningSession
        simple = {**deepcopy(PROPOSAL), 'operation': 'aggregate', 'group_by': 'status', 'limit': None}
        plan = validate_plan(STATUS_QUESTION, simple, {}, {})
        self.assertTrue(validate_execution_contract(STATUS_QUESTION, plan)['valid'])
        plan['group_by'] = None
        self.assertFalse(validate_execution_contract(STATUS_QUESTION, plan)['valid'])
        simple['group_by'] = None
        session = PlanningSession(STATUS_QUESTION, {}, {})
        self.assertFalse(session.submit(proposal=simple)['ok'])
        self.assertIsNone(session.plan)

    def test_repaired_simple_aggregation_uses_the_original_population_and_numeric_gate(self):
        from backend.evaluation.verification import build_verification_report
        from backend.evaluation.delivery import decide_delivery
        model = FakeModel([
            turn(tool_call('prepare_query', {'proposal': periodless_status_proposal()})),
            turn(tool_call('prepare_query', {'changes': {'aggregation': None}})),
            turn(tool_call('prepare_query', {'changes': {'aggregation': None, 'group_by': 'status'}})),
            turn(tool_call('aggregate_mantis_issues', {
                'filters': [{'field': 'customer', 'operator': 'equals', 'value': '한투'},
                            {'field': 'status', 'operator': 'in', 'value': ['open']}],
                'scope': 'all', 'group_by': ['status'], 'metrics': ['issue_count']})), final('999건')])
        stage = scanned()
        stage['meta'].update(group_counts={'정보부족': 3, '할당된이슈': 1},
                             matched_groups={'41': '정보부족', '42': '정보부족', '43': '정보부족', '44': '할당된이슈'})
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage) as source):
            request, ctx = prepared(STATUS_QUESTION)
            result = run_agent_if_eligible(request, ctx)
        self.assertEqual(source.call_count, 1)
        self.assertEqual(source.call_args.args[1]['statuses'], ['open'])
        self.assertEqual(source.call_args.args[1]['customer_key'], '한투')
        self.assertEqual(result['raw']['mode'], 'agent_aggregation_results')
        self.assertIn('| 정보부족 | 3 |', result['content'])
        self.assertNotIn('999', result['content'])
        self.assertEqual([a['status'] for a in result['agentReview']['planning']['attempts']],
                         ['rejected', 'rejected', 'accepted'])
        verified = build_verification_report(STATUS_QUESTION, result['content'], [], result['retrieval'], result['raw']['mode'])
        self.assertTrue(decide_delivery(verified).allowed, verified)
        wrong = build_verification_report(STATUS_QUESTION, result['content'].replace('| 정보부족 | 3 |', '| 정보부족 | 2 |'),
                                          [], result['retrieval'], result['raw']['mode'])
        self.assertFalse(decide_delivery(wrong).allowed)

    def test_unconfirmed_context_cannot_fall_through_to_legacy_search(self):
        from backend.services.unified_planning import execute_plan
        with agent_environment(), request_transfer_scope('company-test'):
            request, ctx = prepared('전사 한투 미해결 이슈가 몇 건이야?')
            response = execute_plan(request, ctx, {'uid': 'u'})
        self.assertIsNotNone(response)
        self.assertIn('확정', response['content'])
        self.assertNotIn('꺼져', response['content'])
        self.assertEqual(response['sources'], [])

    def test_repaired_aggregation_executes_original_open_scope_and_verifies_table(self):
        from backend.reports.aggregation import aggregate_rows
        from backend.evaluation.verification import build_verification_report
        from backend.evaluation.delivery import decide_delivery
        proposed = conflicting_proposal()
        filters = [{'field': 'customer', 'operator': 'equals', 'value': '한투'},
                   {'field': 'status', 'operator': 'in', 'value': ['open']}]
        model = FakeModel([turn(tool_call('prepare_query', {'proposal': proposed})),
            turn(tool_call('prepare_query', {'changes': {'group_by': None}})),
            turn(tool_call('aggregate_mantis_issues', {'filters': filters, 'scope': 'all',
                'group_by': ['platform'], 'metrics': ['issue_count'],
                'period': {'from': '2026-10-01', 'to': '2026-10-31'}, 'comparison': None, 'top_n': 10})), final('999건')])

        def aggregate(plan, **kwargs):
            self.assertEqual(plan['resolved_constraints']['statuses'], ['open'])
            result = aggregate_rows([(None, None, None, {'id': 1, 'created_at': '2026-10-05', 'media_type': 'mts'})],
                                    plan['aggregation'], plan['resolved_constraints'])
            result.update(source={'search_complete': True}, filter_audit={'fully_applied': True})
            return result

        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              patch('backend.tools.mantis_aggregations.execute_aggregation', side_effect=aggregate)):
            request, ctx = prepared(QUESTION)
            result = run_agent_if_eligible(request, ctx)
        self.assertEqual(result['raw']['mode'], 'agent_aggregation_results')
        self.assertIn('| mts | 1 |', result['content'])
        self.assertNotIn('999', result['content'])
        self.assertEqual([a['status'] for a in result['agentReview']['planning']['attempts']], ['rejected', 'accepted'])
        verified = build_verification_report(QUESTION, result['content'], [], result['retrieval'], result['raw']['mode'])
        self.assertTrue(decide_delivery(verified).allowed, verified)
        wrong = build_verification_report(QUESTION, result['content'].replace('| mts | 1 |', '| mts | 99 |'),
                                          [], result['retrieval'], result['raw']['mode'])
        self.assertFalse(decide_delivery(wrong).allowed)

    def test_general_and_write_handoff_preserve_existing_execution_boundary(self):
        from backend.services.unified_planning import execute_plan
        common = {'scope': 'default', 'followup': False, 'filters': [], 'terms': [], 'clear_fields': [],
                  'group_by': None, 'limit': None, 'status': {'mode': 'unspecified', 'values': [], 'evidence': ''}}
        for source, operation, question in [('general', 'explain', 'HTTP 404 오류가 뭐야?'),
                                            ('notion', 'write', '노션에 점검 공지를 작성해줘')]:
            with self.subTest(source=source):
                model = FakeModel([turn(tool_call('prepare_query', {'proposal': {
                    **common, 'source': source, 'operation': operation}}))])
                with agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'):
                    request, ctx = prepared(question)
                    self.assertIsNone(run_agent_if_eligible(request, ctx))
                    self.assertEqual(ctx.intent_plan['source'], source)
                    self.assertEqual(ctx.intent_plan['operation'], operation)
                    if source == 'notion':
                        seen = []

                        def write(text, messages, *, seen=seen, **kwargs):
                            seen.append(kwargs['allow_write'])
                            return {'content': '권한 확인', 'sources': []}

                        with patch('backend.services.unified_planning.run_notion_write_command', side_effect=write):
                            execute_plan(request, ctx, {'uid': 'u', 'role': 'member'})
                        self.assertEqual(seen, [False])

    def test_condition_repair_calls_share_the_same_budget_and_never_query_on_failure(self):
        invalid = {**deepcopy(PROPOSAL), 'status': {'mode': 'unspecified', 'values': [], 'evidence': ''}}
        model = FakeModel([turn(tool_call('prepare_query', {'proposal': invalid})),
                           *[turn(tool_call('prepare_query', {'changes': {'limit': n}})) for n in range(1, 8)]])
        with (agent_environment(model=model, sources=FakeSources(), extra_env={'AGENT_MAX_MODEL_REQUESTS': '5'}),
              request_transfer_scope('company-test')):
            request, ctx = prepared('전사 한투 미해결 이슈 최근 3건을 보여줘.')
            result = run_agent_if_eligible(request, ctx)
        self.assertEqual(result['raw']['mode'], 'agent_plan_clarification')
        self.assertEqual(result['agentReview']['stopReason'], 'call_limit')
        self.assertLessEqual(result['agentReview']['modelRequests'], 5)
        self.assertEqual(result['agentReview']['queryResults'], [])

    def test_ordinary_request_does_not_require_a_separate_front_planner(self):
        q = '전사 한투 미해결 이슈 최근 3건을 보여줘.'
        model = FakeModel([turn(tool_call('prepare_query', {'proposal': deepcopy(PROPOSAL)})),
            turn(tool_call('list_mantis_issues', {'filters': [
                {'field': 'customer', 'operator': 'equals', 'value': '한투'},
                {'field': 'status', 'operator': 'in', 'value': ['open']}], 'scope': 'all', 'limit': 3})),
            final('999건')])
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              patch('backend.services.unified_planning.plan_question', side_effect=AssertionError('front planner')),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned())):
            request, ctx = prepared(q)
            result = run_agent_if_eligible(request, ctx)
        self.assertEqual(result['raw']['mode'], 'agent_query_results')
        self.assertIn('4건입니다', result['content'])
        self.assertNotIn('999', result['content'])
        self.assertEqual(ctx.query_constraints['statuses'], ['open'])
        self.assertEqual([s['issue_id'] for s in result['sourcesForFollowUps']], [44, 43, 42])
        self.assertEqual(result['agentReview']['modelRequests'], 3)

    def test_repair_keeps_status_customer_and_period_without_regenerating_them(self):
        from backend.services.agent.planning import PlanningSession
        session = PlanningSession(QUESTION, {'uid': 'u'}, {})
        first = session.submit(proposal=conflicting_proposal())
        self.assertFalse(first['ok'])
        self.assertIn('충돌', first['error']['message'])
        self.assertIsNone(session.plan)
        second = session.submit(changes={'group_by': None})
        self.assertTrue(second['ok'], second)
        self.assertEqual(session.plan['resolved_constraints']['statuses'], ['open'])
        self.assertEqual(session.plan['resolved_constraints']['customer_key'], '한투')
        self.assertEqual(session.plan['aggregation']['period']['from'], '2026-10-01')
        self.assertEqual(session.plan['interpretation']['status']['evidence'], '미해결')

    def test_repair_cannot_drop_explicit_open_condition(self):
        from backend.services.agent.planning import PlanningSession
        session = PlanningSession(QUESTION, {}, {})
        session.submit(proposal=conflicting_proposal())
        missing = session.submit(changes={'group_by': None,
            'status': {'mode': 'unspecified', 'values': [], 'evidence': ''}})
        self.assertFalse(missing['ok'])
        self.assertIn('상태 조건이 누락', missing['error']['message'])
        self.assertIsNone(session.plan)
        # 거부한 변경은 원본 초안에 반영하지 않아 다음 부분 보정도 상태를 잃지 않는다.
        fixed = session.submit(changes={'group_by': None})
        self.assertTrue(fixed['ok'], fixed)
        self.assertEqual(session.plan['resolved_constraints']['statuses'], ['open'])

    def test_no_data_tool_can_run_before_conditions_are_validated(self):
        model = FakeModel([turn(tool_call('count_mantis_issues', {'filters': [], 'scope': 'all'})), final('0건')])
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=AssertionError('unvalidated data'))):
            request, ctx = prepared('전사 한투 미해결 이슈가 몇 건이야?')
            result = run_agent_if_eligible(request, ctx)
        self.assertEqual(result['raw']['mode'], 'agent_plan_clarification')
        self.assertNotIn('0건', result['content'])
        self.assertFalse(result.get('sources'))

    def test_confirmed_plan_is_immutable_and_disabled_source_cannot_be_enabled(self):
        from backend.services.agent.planning import PlanningSession
        session = PlanningSession('전사 한투 미해결 이슈 최근 3건을 보여줘.', {}, {})
        self.assertTrue(session.submit(proposal=deepcopy(PROPOSAL))['ok'])
        self.assertFalse(session.submit(changes={'status': {'mode': 'all', 'values': [], 'evidence': '전사'}})['ok'])
        self.assertEqual(session.plan['resolved_constraints']['statuses'], ['open'])
        model = FakeModel([turn(tool_call('prepare_query', {'proposal': deepcopy(PROPOSAL)}))])
        with agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'):
            request, ctx = prepared('전사 한투 미해결 이슈 최근 3건을 보여줘.', tools={'mantis': False})
            result = run_agent_if_eligible(request, ctx)
        self.assertEqual(result['raw']['mode'], 'agent_plan_clarification')
        self.assertIn('꺼져', result['content'])


if __name__ == '__main__':
    unittest.main()
