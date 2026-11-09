"""남은 보고서 도구의 조건·날짜·내부 모델 호출 경계."""
from contextlib import nullcontext
from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch

from agent_harness_cases import raw_issue
from backend.intent.unified_validation import validate_plan
from backend.services.agent.eligibility import supports_read_plan
from backend.sources.mantis_collection import IssueBatch
from backend.tools.catalog import agent_registry
from test_tool_condition_arguments import PROPOSAL, arguments, context_for


NAMES = {'profile': '고객사 프로필', 'sla': '평균 처리시간', 'effort_sla': '공수와 평균 처리시간',
         'similar': '#31505와 유사한 과거 이슈 원인과 해결 비교', 'triage': '차트 장애 초동 대응',
         'priority': '급한 업무 우선순위', 'aging': '오래 묵은 업무', 'due': '이번 주 마감 업무',
         'comparison': '지난주보다 미해결 업무가 줄었는지 비교', 'completed': '이번 주 완료한 업무',
         'waiting': '고객 회신 대기 업무', 'customers': '고객사별 현황',
         'pivot': '고객사별 월별 교차표', 'spike': '고객사별 이슈 급증'}


def planned(kind, *, filters=None, question=None):
    q = question or f'전사 한투 {NAMES[kind]} 보여줘.'
    proposal = {**deepcopy(PROPOSAL), 'operation': 'report', 'report_kind': kind, 'limit': None,
                'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
                'filters': filters if filters is not None else deepcopy(PROPOSAL['filters'])}
    if kind == 'similar':
        proposal['filters'].append({'field': 'issue_id', 'operator': 'equals', 'value': 31505, 'evidence': '31505'})
    if kind == 'effort_sla':
        proposal['effort'] = {'type': 'total', 'order': 'desc', 'show_all': False}
    plan = validate_plan(q, proposal, {'uid': 'u'}, {})
    return q, plan, context_for(plan, q)


def report_args(ctx, kind):
    return {**arguments(ctx), **ctx.query_contracts['mantis'].report}


def issue(identifier, *, status='closed', created='2026-10-01', updated='2026-10-11', project='117Hantu'):
    row = raw_issue(identifier, '차트 문제', '차트 원문', status=status, project=project)
    row.update(created_at=created, updated_at=updated)
    return row


class ExtendedReportTests(unittest.TestCase):
    def test_all_remaining_report_kinds_enter_agent_and_require_report_result(self):
        from backend.services.agent.query_answers import missing_query_results
        from backend.tools.ledger import EvidenceLedger
        for kind in NAMES:
            with self.subTest(kind=kind):
                plan = {'source': 'mantis', 'operation': 'report', 'report_kind': kind}
                self.assertTrue(supports_read_plan(plan))
                self.assertEqual(missing_query_results(plan, EvidenceLedger()), ['보고서(build_issue_report)'])

    def test_new_kinds_have_executable_grounded_contracts(self):
        for kind in NAMES:
            with self.subTest(kind=kind):
                _, _, ctx = planned(kind)
                self.assertEqual(ctx.query_contracts['mantis'].report['kind'], kind)

    def test_profile_preserves_conditions_and_distinguishes_complete_zero_from_partial(self):
        for complete in (True, False):
            q, plan, ctx = planned('profile')
            with patch('backend.reports.customer.stream_complete_store', return_value=nullcontext(IssueBatch([], {'search_complete': complete}))):
                result = agent_registry(query_tools=True).execute('build_issue_report', report_args(ctx, 'profile'), '', ctx)
            self.assertTrue(result['ok'], result)
            self.assertEqual(result['search_complete'], complete)
            self.assertEqual(ctx.ledger.reports[-1]['outcome'], 'complete_empty' if complete else 'search_incomplete')
            self.assertEqual(ctx.ledger.reports[-1]['report']['retrieval']['constraints']['filter_clauses'],
                             plan['resolved_constraints']['filter_clauses'])

    def test_duration_keeps_customer_filters_and_excludes_invalid_dates(self):
        _, _, ctx = planned('sla')
        data = IssueBatch([issue(1), issue(2, created=''), issue(3, updated='2026-09-01'),
                           issue(4, status='assigned'), issue(5, project='NH')], {'search_complete': True})
        with patch('backend.reports.sla.fetch_mantis_recent_issues', return_value=data):
            result = agent_registry(query_tools=True).execute('build_issue_report', report_args(ctx, 'sla'), '', ctx)
        self.assertTrue(result['ok'], result)
        report = ctx.ledger.reports[-1]['report']
        self.assertIn('10.0일', report['content'])
        self.assertEqual({s['issue_id'] for s in report['sources']}, {1})
        self.assertEqual(report['retrieval']['stages'][0]['meta']['invalid_duration_count'], 2)
        self.assertEqual(result['report_contract']['duration_basis'], 'created_at_to_updated_at_approximation')

    def test_date_contract_and_kind_cannot_be_changed_by_model(self):
        _, _, ctx = planned('sla')
        args = report_args(ctx, 'sla')
        with patch('backend.reports.sla.fetch_mantis_recent_issues') as source:
            for changed in ({**args, 'kind': 'profile'}, {**args, 'options': {**args['options'], 'duration_basis': 'closed_at'}}):
                result = agent_registry(query_tools=True).execute('build_issue_report', changed, '', ctx)
                self.assertFalse(result['ok'])
        source.assert_not_called()

    def test_structured_priority_keeps_explicit_all_scope_and_created_period(self):
        from backend.reports.work_queries import build_work_query_report
        rows = IssueBatch([issue(1, status='assigned'), issue(2, created='2026-09-01', status='assigned')],
                          {'search_complete': True, 'snapshot_complete': True})
        c = {'mantis_team_scope': 'all', 'created_at': {'from': '2026-10-01', 'to': '2026-10-31'},
             'filter_clauses': [{'field': 'created_at', 'operator': 'between', 'value': ['2026-10-01', '2026-10-31']}]}
        with patch('backend.reports.work_queries.read_scoped_store', return_value=rows):
            report = build_work_query_report('전사 2026년 10월 등록 업무 우선순위', c, operation='priority',
                                              report_options={'as_of': '2026-11-09'}, today=date(2026, 11, 9))
        applied = report['retrieval']['constraints']
        self.assertEqual(applied['created_at'], c['created_at'])
        self.assertNotIn('mantis_team_members', applied)
        self.assertEqual(report['retrieval']['stages'][0]['meta']['matched_ids'], [1])

    def test_triage_reference_uses_confirmed_platform_and_preserves_other_conditions(self):
        from backend.reports.incident_triage import build_incident_triage_report
        c = {'customer_key': '한투', 'platform_key': 'mts', 'platform_aliases': ['MTS'],
             'statuses': ['open'], 'mantis_team_scope': 'all', 'filter_clauses': []}
        with patch('backend.reports.incident_triage.execute_chroma_search', return_value={'status': 'ok', 'documents': []}) as search:
            report = build_incident_triage_report('한투 MTS 차트 초동 대응', constraints=c, topic_terms=['차트'])
        self.assertEqual(report['retrieval']['constraints'], c)
        self.assertEqual(search.call_args.args[2]['platform_key'], 'mts')
        self.assertNotIn('customer_key', search.call_args.args[2])
        self.assertNotIn('statuses', search.call_args.args[2])

    def test_due_keeps_registration_period_and_skips_missing_invalid_target_dates(self):
        from backend.reports.work_queries import build_work_query_report
        from backend.tools.report_contracts import report_contract
        q = '전사 한투 2026년 10월 등록 이슈 중 마감일 이번 주인 업무를 보여줘.'
        filters = [*deepcopy(PROPOSAL['filters']), {'field': 'created_at', 'operator': 'between',
            'value': ['2026-10-01', '2026-10-31'], 'evidence': '2026년 10월'}]
        _, plan, _ = planned('due', filters=filters, question=q)
        with patch('backend.tools.report_contracts.date') as clock:
            clock.today.return_value = date(2026, 11, 9)
            options = report_contract(q, plan)['options']
        self.assertEqual(options['due_period']['from'], '2026-11-09')
        rows = [issue(i, status='assigned') for i in range(1, 5)]
        rows[0]['custom_fields'] += [{'field': {'name': '고객사전달 목표일'}, 'value': '잘못된 날짜'},
                                    {'field': {'name': '개발완료 목표일'}, 'value': '2026-11-11'}]
        rows[1]['custom_fields'] += [{'field': {'name': '고객사전달 목표일'}, 'value': '2026-12-01'},
                                    {'field': {'name': '개발완료 목표일'}, 'value': '2026-11-11'}]
        rows[2]['custom_fields'] += [{'field': {'name': '고객사전달일'}, 'value': '2026-11-11'}]
        rows[3]['created_at'] = '2026-09-01'
        rows[3]['custom_fields'] += [{'field': {'name': '개발완료 목표일'}, 'value': '2026-11-11'}]
        with patch('backend.reports.work_queries.read_scoped_store', return_value=IssueBatch(rows,
                {'search_complete': True, 'snapshot_complete': True})):
            result = build_work_query_report(q, plan['resolved_constraints'], operation='due',
                                             report_options=options, today=date(2026, 11, 9))
        self.assertEqual(result['retrieval']['stages'][0]['meta']['matched_ids'], [1])
        self.assertIn('1건은 포함 여부를 알 수 없습니다', result['content'])
        from backend.evaluation.verification import build_verification_report
        from backend.evaluation.delivery import decide_delivery
        check = build_verification_report(q, result['content'], result['sources'], result['retrieval'], result['raw']['mode'])
        self.assertTrue(decide_delivery(check).allowed, check)
        damaged = build_verification_report(q, result['content'].replace('이슈 1건', '이슈 99건'),
                                           result['sources'], result['retrieval'], result['raw']['mode'])
        self.assertFalse(decide_delivery(damaged).allowed)

    def test_pivot_and_spike_keep_group_and_scan_completeness(self):
        from backend.tools.report_contracts import report_contract
        contract = report_contract('화면번호별 월별 교차표', {'operation': 'report', 'report_kind': 'pivot'})
        self.assertEqual(contract['options']['group_by'], 'screen')
        with self.assertRaises(ValueError):
            report_contract('상태별 급증', {'operation': 'report', 'report_kind': 'spike', 'group_by': 'status'})
        snapshot = {'available': True, 'scanned_count': 12, 'matched_count': 5,
                    'group_counts': {'117Hantu\x1f2026-09': 1, '117Hantu\x1f2026-10': 4}}
        for kind in ('pivot', 'spike'):
            for complete in (True, False):
                _, _, ctx = planned(kind)
                with patch('backend.reports.aggregate.scan_mantis_documents', return_value={**snapshot, 'search_complete': complete}):
                    result = agent_registry(query_tools=True).execute('build_issue_report', report_args(ctx, kind), '', ctx)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['search_complete'], complete)
                self.assertEqual(ctx.ledger.reports[-1]['outcome'], 'sufficient_evidence' if complete else 'partial_evidence')

    def test_missing_status_history_remains_incomplete(self):
        for kind in ('completed', 'comparison', 'waiting'):
            _, _, ctx = planned(kind)
            with patch('backend.reports.work_queries.read_scoped_store') as source:
                result = agent_registry(query_tools=True).execute('build_issue_report', report_args(ctx, kind), '', ctx)
            self.assertTrue(result['ok'], result)
            self.assertFalse(result['search_complete'])
            self.assertEqual(ctx.ledger.reports[-1]['outcome'], 'search_incomplete')
            source.assert_not_called()

    def test_reference_role_and_disabled_search_cannot_be_promoted(self):
        from backend.sources.mantis_fields import normalize_issue
        from backend.evaluation.agent_steps import verify_condition_steps
        _, _, ctx = planned('similar')
        with patch('backend.reports.similar.fetch_mantis_issue_by_id') as source:
            rejected = agent_registry(query_tools=True).execute('build_issue_report', report_args(ctx, 'similar'), '', ctx)
        self.assertFalse(rejected['ok'])
        source.assert_not_called()
        _, _, ctx = planned('similar')
        ctx.enabled_sources['vectorDb'] = True
        with patch('backend.reports.similar.fetch_mantis_issue_by_id', return_value=issue(31505)), \
             patch('backend.reports.similar._search_similar_candidates', return_value={'status': 'ok',
                   'documents': [{'issue_id': 31504, 'latest_issue': normalize_issue(issue(31504))}]}):
            accepted = agent_registry(query_tools=True).execute('build_issue_report', report_args(ctx, 'similar'), '', ctx)
        self.assertTrue(accepted['ok'], accepted)
        self.assertEqual(verify_condition_steps(ctx.ledger, ctx.condition_refs)['status'], 'pass')
        candidate = next(s for s in ctx.ledger.stages if s.grade == 'reference')
        candidate.stage['meta']['report_evidence']['scope'] = 'team'
        self.assertEqual(verify_condition_steps(ctx.ledger, ctx.condition_refs)['status'], 'fail')

    def test_default_effort_period_is_limited_to_server_current_year(self):
        _, plan, ctx = planned('effort_sla')
        self.assertIn({'field': 'delivered_at', 'origin': 'server_default'}, ctx.query_contracts['mantis'].origins)
        for location in (plan['filter_clauses'], plan['resolved_constraints']['filter_clauses']):
            next(c for c in location if c['field'] == 'delivered_at')['value'] = ['1999-01-01', '1999-12-31']
        with self.assertRaises(ValueError):
            context_for(plan, plan['grounding_text'])
        # 모델이 명시 연도를 빼먹은 경우를 기간 생략으로 취급하지 않는다.
        with self.assertRaises(ValueError):
            planned('effort_sla', question='전사 한투 2025년 공수와 평균 처리시간을 알려줘.')

    def test_effort_and_duration_share_filters_and_keep_numeric_verification(self):
        from agent_harness_cases import FakeModel, FakeSources, agent_environment, chat_request, explain_context, final, tool_call, turn
        from backend.evaluation.delivery import decide_delivery
        from backend.evaluation.verification import build_verification_report
        from backend.services.agent.policy import request_transfer_scope
        from backend.services.agent.route import run_agent_if_eligible
        q = '전사 한투 2026년 10월 공수와 평균 처리시간을 알려줘.'
        filters = [*deepcopy(PROPOSAL['filters']), {'field': 'delivered_at', 'operator': 'between',
            'value': ['2026-10-01', '2026-10-31'], 'evidence': '2026년 10월'}]
        _, plan, ctx = planned('effort_sla', question=q, filters=filters)
        rows = [issue(1), issue(2, project='NH'), issue(3)]
        for row in rows:
            row['custom_fields'] += [{'field': {'name': '고객사전달일'}, 'value': '2026-11-07' if row['id'] == 3 else '2026-10-07'},
                                     {'field': {'name': '1문의공수'}, 'value': '석지원(2.5)'}]
        model = FakeModel([turn(tool_call('build_issue_report', report_args(ctx, 'effort_sla'))), final('총 공수 999')])
        with agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'), \
             patch('backend.reports.effort.effort_issues', return_value=nullcontext((rows, {'search_complete': True}))), \
             patch('backend.reports.sla.fetch_mantis_recent_issues', return_value=IssueBatch(rows, {'search_complete': True})):
            context = explain_context(q, source='mantis', constraints=plan['resolved_constraints'])
            context.intent_plan, context.structured_tool_conditions = plan, True
            result = run_agent_if_eligible(chat_request(q), context)
        self.assertEqual(result['agentReview']['finalization'], 'report_tool')
        self.assertEqual(result['retrieval']['sla_source_ids'], [1])
        self.assertEqual(result['retrieval']['effort_source_ids'], [1])
        self.assertIn('10.0일', result['content'])
        self.assertIn('총 공수: **2.50', result['content'])
        check = build_verification_report(q, result['content'], result['sourcesForFollowUps'], result['retrieval'], result['raw']['mode'])
        self.assertTrue(decide_delivery(check).allowed, check)
        checks = {c['id']: c['status'] for c in check['checks']}
        self.assertEqual(checks['effort_recalculation'], 'pass')
        damaged = build_verification_report(q, result['content'].replace('총 공수: **2.50', '총 공수: **99.00'),
                                           result['sourcesForFollowUps'], result['retrieval'], result['raw']['mode'])
        self.assertFalse(decide_delivery(damaged).allowed)

    def test_report_failure_does_not_deliver_model_freeform_as_finished_report(self):
        from agent_harness_cases import FakeModel, FakeSources, agent_environment, chat_request, explain_context, final, tool_call, turn
        from backend.services.agent.policy import request_transfer_scope
        from backend.services.agent.route import run_agent_if_eligible
        q, plan, ctx = planned('profile')
        model = FakeModel([turn(tool_call('build_issue_report', report_args(ctx, 'profile'))), final('보고서 완성: 999건입니다.')])
        with agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'), \
             patch('backend.tools.issue_reports.calculate_report', return_value=None):
            context = explain_context(q, source='mantis', constraints=plan['resolved_constraints'])
            context.intent_plan, context.structured_tool_conditions = plan, True
            result = run_agent_if_eligible(chat_request(q), context)
        self.assertNotIn('999', result['content'])
        self.assertEqual(result['agentReview']['retrievalOutcome'], 'search_incomplete')
        self.assertNotEqual(result['agentReview']['finalization'], 'report_tool')

    def test_extended_report_parts_compose_without_bypassing_part_validation(self):
        from backend.evaluation.delivery import decide_delivery
        from backend.evaluation.verification import build_verification_report
        from backend.services.agent.response_parts import compose_parts
        q, plan, ctx = planned('profile')
        rows = [issue(1)]
        with patch('backend.reports.customer.stream_complete_store', return_value=nullcontext(IssueBatch(rows,
                {'search_complete': True, 'snapshot_complete': True}))):
            result = agent_registry(query_tools=True).execute('build_issue_report', report_args(ctx, 'profile'), '', ctx)
        self.assertTrue(result['ok'], result)
        report = ctx.ledger.reports[-1]['report']
        report['retrieval'].update(query_plan=plan, intent_plan=plan)
        report['sourcesForFollowUps'] = report['sources']
        from backend.reports.sla import build_sla_report
        with patch('backend.reports.sla.fetch_mantis_recent_issues', return_value=IssueBatch(rows, {'search_complete': True})):
            duration = build_sla_report(q, mode='duration', constraints=plan['resolved_constraints'])
        duration['sourcesForFollowUps'] = duration['sources']
        duration['retrieval'].update(query_plan=plan, intent_plan=plan)
        # 독립된 두 결과를 함께 보여 주어도 각 결과의 기존 검증이 반드시 실행된다.
        combined = compose_parts([('report', report), ('duration', duration)], {})
        checked = build_verification_report(q, combined['content'], combined['sources'], combined['retrieval'], 'agent_composed')
        self.assertTrue(decide_delivery(checked).allowed, checked)
        damaged = build_verification_report(q, combined['content'] + '\n999건', combined['sources'],
                                            combined['retrieval'], 'agent_composed')
        self.assertFalse(decide_delivery(damaged).allowed)


if __name__ == '__main__':
    unittest.main()
