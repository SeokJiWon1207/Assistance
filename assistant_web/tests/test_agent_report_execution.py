"""보고서 내부 모델 호출도 같은 예산·정책·취소·사용량 검증을 거친다."""
from contextlib import ExitStack
from copy import deepcopy
from functools import partial
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import requests

from agent_harness_cases import COMPANY, BACKUP, FakeModel, FakeSources, agent_environment, chat_request, explain_context, final, tool_call, turn
from backend import llm
from backend.execution import ExecutionBudget, RequestStopped, execution_scope
from backend.services.agent.orchestrator import AgentBudget, AgentStop, _request_turn
from backend.services.agent.policy import request_transfer_scope
from backend.services.agent.report_execution import report_chat
from backend.services.agent.route import run_agent_if_eligible
from backend.sources.mantis_fields import normalize_issue
from test_agent_extended_reports import issue, planned, report_args


def make_run(maximum=8, reserve=2):
    return SimpleNamespace(budget=AgentBudget(maximum, 8, reserve, 1),
        settings=SimpleNamespace(max_output_tokens=4096, model_timeout_seconds=20, max_context_chars=90000),
        model=deepcopy(COMPANY), text_models=[deepcopy(COMPANY), deepcopy(BACKUP)],
        failed_models={'text': set(), 'decision': set()}, models_used=set(), usage=[], trace=[], serving_fingerprints={})


def response(value):
    return (200, {'choices': [final(json.dumps(value))], 'usage': {'prompt_tokens': 11, 'completion_tokens': 7}})


class InternalReportTests(unittest.TestCase):
    def test_retry_and_fallback_are_charged_and_usage_is_not_silently_lost(self):
        run = make_run()
        with patch('backend.services.agent.orchestrator._post', side_effect=[(503, {}), (503, {}), response({'ok': True})]) as post:
            self.assertEqual(report_chat(run, _request_turn, None, None, '검토', {}, max_tokens=100, timeout=4), {'ok': True})
        self.assertEqual([c.args[0]['id'] for c in post.call_args_list], [COMPANY['id'], COMPANY['id'], BACKUP['id']])
        self.assertEqual(run.budget.model_requests, 3)
        self.assertEqual(len(run.usage), 3)
        self.assertEqual(sum(u.get('reported') is False for u in run.usage), 2)

    def test_internal_calls_cannot_spend_final_reserve(self):
        run = make_run(3, 2)
        with patch('backend.services.agent.orchestrator._post', return_value=response({})) as post:
            report_chat(run, _request_turn, None, None, '검토', {}, max_tokens=10, timeout=4)
            with self.assertRaises(requests.RequestException):
                report_chat(run, _request_turn, None, None, '검토', {}, max_tokens=10, timeout=4)
        self.assertEqual(post.call_count, 1)
        self.assertEqual(run.budget.model_requests, 1)

    def test_policy_refusal_and_cancellation_are_not_converted_to_optional_summary_failure(self):
        run = make_run()
        with patch('backend.services.agent.orchestrator._post', side_effect=llm.ModelTransferBlocked('blocked')):
            with self.assertRaises(AgentStop) as stop:
                report_chat(run, _request_turn, None, None, '검토', {}, max_tokens=10, timeout=4)
        self.assertEqual(stop.exception.reason, 'policy_blocked')
        budget = ExecutionBudget(time.monotonic()+30)
        with execution_scope(budget):
            run = make_run()
            def cancel(*args):
                budget.cancel()
                return response({})
            with patch('backend.services.agent.orchestrator._post', side_effect=cancel), self.assertRaises(RequestStopped):
                report_chat(run, _request_turn, None, None, '검토', {}, max_tokens=10, timeout=4)

    def test_summary_still_requires_independent_review_and_valid_fact_bindings(self):
        from test_comparison_narrative import ComparisonNarrativeTests
        from backend.reports.comparison_narrative import summarize_comparison
        fixture = ComparisonNarrativeTests(); fixture.setUp()
        run = make_run()
        with patch('backend.services.agent.orchestrator._post', side_effect=[response(fixture.draft), response({'supported': [False, True]})]), \
             patch('backend.reports.comparison_narrative.post_model_chat', side_effect=AssertionError('공통 실행기 우회')):
            content, meta = summarize_comparison('원인·조치 비교', fixture.issues, None, None, chat=partial(report_chat, run, _request_turn))
        self.assertEqual(content, '')
        self.assertEqual(meta['status'], 'rejected')
        self.assertEqual(run.budget.model_requests, 2)

    def test_similar_report_runs_fact_draft_review_inside_agent_and_separates_reference(self):
        q, plan, ctx = planned('similar')
        base, candidate = issue(31505), issue(31504)
        class Model(FakeModel):
            def grounded(self, payload):
                data = json.loads(payload['messages'][-1]['content'])
                if 'passages' in data:
                    value = {'issues': [{'issue_id': i, 'cause': [], 'uncertain_cause': [], 'action': []} for i in data['issue_ids']]}
                elif 'claims' in data:
                    value = {'supported': [True] * len(data['claims'])}
                else:
                    facts = [f for f in data['facts'] if f['kind'] == 'unknown']
                    value = {'claims': [{'text': f"#{f['issue_id']} 제공된 근거에서는 확정 원인과 실제 조치를 확인하지 못했습니다.",
                                          'fact_ids': [f['id']]} for f in facts]}
                return self.respond(final(json.dumps(value, ensure_ascii=False)))
        model = Model([turn(tool_call('build_issue_report', report_args(ctx, 'similar'))), final('999건입니다')])
        with ExitStack() as stack:
            stack.enter_context(execution_scope(ExecutionBudget(time.monotonic()+180)))
            stack.enter_context(agent_environment(model=model, sources=FakeSources()))
            stack.enter_context(request_transfer_scope('company-test'))
            stack.enter_context(patch('backend.reports.similar.fetch_mantis_issue_by_id', return_value=base))
            stack.enter_context(patch('backend.reports.similar._search_similar_candidates', return_value={
                'status': 'ok', 'documents': [{'issue_id': 31504, 'latest_issue': normalize_issue(candidate)}]}))
            stack.enter_context(patch('backend.reports.comparison_facts._needs_review', return_value=True))
            for module in ('comparison_facts', 'comparison_narrative'):
                stack.enter_context(patch(f'backend.reports.{module}.post_model_chat', side_effect=AssertionError('공통 실행기 우회')))
            context = explain_context(q, source='mantis', constraints=plan['resolved_constraints'])
            context.intent_plan, context.structured_tool_conditions = plan, True
            result = run_agent_if_eligible(chat_request(q), context)
        self.assertEqual(result['agentReview']['finalization'], 'report_tool')
        self.assertEqual(result['agentReview']['modelRequests'], 5, {k: result[k] for k in ('content', 'agentReview')})
        self.assertEqual(sum(t.get('step') == 'report_internal' for t in result['agentReview']['trace']), 3)
        self.assertEqual(result['agentReview']['usage']['reportedCalls'], 5)
        self.assertIn('M#31504', result['agentReview']['evidence']['reference'])
        self.assertNotIn('M#31504', result['agentReview']['evidence']['scoped'])
        self.assertNotIn('999건', result['content'])
        self.assertEqual(next(c for c in result['agentChecks'] if c['id'] == 'agent_step_conditions')['status'], 'pass')
