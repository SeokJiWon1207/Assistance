"""기존 보고서 계산과 검증을 에이전트에서도 재사용하는 회귀."""
from copy import deepcopy
from contextlib import nullcontext
import unittest
from unittest.mock import patch

from agent_harness_cases import FakeModel, FakeSources, agent_environment, chat_request, explain_context, final, raw_issue, tool_call, turn
from backend.evaluation.delivery import decide_delivery
from backend.evaluation.verification import build_verification_report
from backend.intent.unified_validation import validate_plan
from backend.services.agent.policy import request_transfer_scope
from backend.services.agent.route import run_agent_if_eligible
from backend.sources.mantis_collection import IssueBatch
from backend.tools.catalog import agent_registry
from test_tool_condition_arguments import PROPOSAL, arguments, context_for


def report_plan(kind):
    name = {'effort': '공수', 'weekly': '주간 보고서', 'monthly': '월간 고객사 리포트'}[kind]
    q = f'전사 한투 미해결 이슈 중 제목에 "차트" 포함된 항목만 2026년 10월 {name}로 보여줘.'
    filters = [*deepcopy(PROPOSAL['filters']), {'field': 'title', 'operator': 'contains', 'value': '차트', 'evidence': '차트'},
               {'field': 'delivered_at' if kind == 'effort' else 'created_at', 'operator': 'between',
                'value': ['2026-10-01', '2026-10-31'], 'evidence': '2026년 10월'}]
    proposal = {**deepcopy(PROPOSAL), 'operation': 'report', 'report_kind': kind, 'filters': filters, 'limit': None}
    if kind == 'effort':
        proposal['effort'] = {'type': 'total', 'order': 'desc', 'show_all': False}
    return q, validate_plan(q, proposal, {'uid': 'u'}, {})


def rows():
    result = []
    for identifier, title, status in [(1, '차트 설정', 'assigned'), (2, '차트 완료', 'closed'), (3, '주문 요청', 'assigned')]:
        row = raw_issue(identifier, title, '', project='117Hantu', status=status)
        row.update(created_at='2026-10-05', updated_at='2026-10-07')
        result.append(row)
    return result


class ReportToolTests(unittest.TestCase):
    def test_weekly_and_monthly_keep_keyword_and_explicit_open_filters(self):
        from backend.evaluation.agent_steps import verify_condition_steps
        for kind, module in [('weekly', 'weekly_customer'), ('monthly', 'monthly_customer')]:
            q, plan = report_plan(kind)
            ctx = context_for(plan, q)
            data = IssueBatch(rows(), {'search_complete': True, 'coverage': 'complete_store'})
            with patch(f'backend.reports.{module}.load_report_issues', return_value=data):
                result = agent_registry(query_tools=True).execute('build_issue_report', {**arguments(ctx), 'kind': kind}, '', ctx)
            self.assertTrue(result['ok'], result)
            self.assertTrue(result['search_complete'])
            report = ctx.ledger.reports[-1]['report']
            self.assertEqual(report['retrieval']['stages'][0]['meta']['current_count'], 1)
            self.assertEqual({s['issue_id'] for s in report['sources']}, {1})
            self.assertEqual(verify_condition_steps(ctx.ledger, ctx.condition_refs)['status'], 'pass')

    def test_report_zero_and_partial_are_distinguished(self):
        for kind, module in [('weekly', 'weekly_customer'), ('monthly', 'monthly_customer')]:
            for complete in (True, False):
                q, plan = report_plan(kind)
                ctx = context_for(plan, q)
                with patch(f'backend.reports.{module}.load_report_issues', return_value=IssueBatch([], {'search_complete': complete})):
                    result = agent_registry(query_tools=True).execute('build_issue_report', {**arguments(ctx), 'kind': kind}, '', ctx)
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['search_complete'], complete)
                content = ctx.ledger.reports[-1]['report']['content']
                self.assertEqual('전체 조회를 마치지 못했습니다' in content, not complete)

    def test_report_agent_keeps_original_verification_mode(self):
        for kind, module in [('weekly', 'weekly_customer'), ('monthly', 'monthly_customer')]:
            q, plan = report_plan(kind)
            ctx = context_for(plan, q)
            model = FakeModel([turn(tool_call('build_issue_report', {**arguments(ctx), 'kind': kind})), final('신규 999건')])
            with agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'), \
                 patch(f'backend.reports.{module}.load_report_issues', return_value=IssueBatch(rows(), {'search_complete': True})):
                chat = explain_context(q, source='mantis', constraints=plan['resolved_constraints'])
                chat.intent_plan = plan
                chat.structured_tool_conditions = True
                result = run_agent_if_eligible(chat_request(q), chat)
            self.assertEqual(result['agentReview']['finalization'], 'report_tool')
            self.assertIn('deterministic_', result['raw']['mode'])
            self.assertNotIn('999', result['content'])
            report = build_verification_report(q, result['content'], result['sourcesForFollowUps'], result['retrieval'], result['raw']['mode'])
            self.assertTrue(decide_delivery(report).allowed, report['checks'])

    def test_effort_recalculation_is_not_bypassed_by_agent(self):
        q, plan = report_plan('effort')
        ctx = context_for(plan, q)
        effort = ctx.query_contracts['mantis'].report['effort']
        model = FakeModel([turn(tool_call('build_issue_report', {**arguments(ctx), 'kind': 'effort', 'effort': effort})), final('총 공수 999')])
        data = rows()
        for row in data:
            row['custom_fields'].extend([{'field': {'name': '고객사전달일'}, 'value': '2026-10-07'},
                                         {'field': {'name': '1문의공수'}, 'value': f"석지원({2.5 if row['id'] == 1 else 100})"}])
        with agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'), \
             patch('backend.reports.effort.effort_issues', return_value=nullcontext((data, {'search_complete': True}))):
            chat = explain_context(q, source='mantis', constraints=plan['resolved_constraints'])
            chat.intent_plan = plan
            chat.structured_tool_conditions = True
            result = run_agent_if_eligible(chat_request(q), chat)
        self.assertEqual(result['agentReview']['finalization'], 'report_tool')
        report = build_verification_report(q, result['content'], result['sourcesForFollowUps'], result['retrieval'], result['raw']['mode'])
        checks = {c['id']: c['status'] for c in report['checks']}
        self.assertEqual(checks['effort_recalculation'], 'pass')
        self.assertEqual(checks['effort_display'], 'pass')
        self.assertIn('총 공수: **2.50', result['content'])
        damaged = build_verification_report(q, result['content'].replace('총 공수: **2.50', '총 공수: **99.00'),
            result['sourcesForFollowUps'], result['retrieval'], result['raw']['mode'])
        self.assertFalse(decide_delivery(damaged).allowed)
