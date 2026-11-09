"""호출 중/실패/취소/충돌 상태가 요청의 확정 근거로 섞이지 않아야 한다."""
from copy import deepcopy
from concurrent.futures import CancelledError, ThreadPoolExecutor
from threading import Barrier, Event
import time
import unittest
from unittest.mock import patch

import requests
from pydantic import BaseModel

from backend import execution, llm
from backend.execution import ExecutionBudget, RequestStopped, execution_scope
from backend.services.agent.orchestrator import AgentBudget, AgentBudgetExhausted
from backend.tools.ledger import EvidenceLedger
from backend.tools.registry import ToolBudgetExhausted, ToolRegistry, ToolSpec
from test_tool_condition_arguments import context_for


class ReadArgs(BaseModel):
    key: int


def append(context, key):
    context.ledger.add('mantis', {'source': 'mantis_snapshot', 'status': 'ok',
        'issues_list': [{'id': key, 'summary': f'제목 {key}'}], 'meta': {'found_count': 1, 'search_complete': True}},
        grade='scoped', unapplied=(), call_id=context.call_id)
    context.notion_read_cache[str(key)] = {'content': str(key)}


def registry(handler):
    return ToolRegistry([ToolSpec('read', '읽기', ReadArgs, 'mantis', handler)])


class ToolOwnershipTests(unittest.TestCase):
    def test_results_are_private_until_publish_and_late_alias_mutation_cannot_change_evidence(self):
        root = context_for()
        retained = []
        def read(args, local):
            append(local, args.key)
            retained.append(local)
            self.assertEqual(root.ledger.issue_rows(), [])
            self.assertEqual(root.notion_read_cache, {})
            return {'ok': True, 'results': []}
        self.assertTrue(registry(read).execute('read', {'key': 1}, '', root)['ok'])
        self.assertEqual([r['id'] for r in root.ledger.issue_rows()], [1])
        append(retained[0], 99)
        retained[0].notion_read_cache['1']['content'] = '변조'
        self.assertEqual([r['id'] for r in root.ledger.issue_rows()], [1])
        self.assertEqual(root.notion_read_cache['1']['content'], '1')

    def test_exception_discards_provisional_stages_and_caches(self):
        root = context_for()
        def read(args, local):
            append(local, args.key)
            raise requests.Timeout()
        result = registry(read).execute('read', {'key': 2}, '', root)
        self.assertEqual(result['error']['code'], 'tool_error')
        self.assertEqual(root.ledger.issue_rows(), [])
        self.assertEqual(root.notion_read_cache, {})

    def test_cancelled_tool_cannot_publish_even_if_handler_returns_success(self):
        root = context_for()
        budget = ExecutionBudget(time.monotonic() + 30)
        def read(args, local):
            append(local, args.key)
            budget.cancel()
            return {'ok': True}
        with execution_scope(budget), self.assertRaises(RequestStopped):
            registry(read).execute('read', {'key': 3}, '', root)
        self.assertEqual(root.ledger.issue_rows(), [])
        self.assertEqual(root.condition_refs, {})
        self.assertEqual(root.signatures, {})

    def test_concurrent_stale_result_is_rejected_without_overwriting_another_call(self):
        root = context_for()
        started, release = Event(), Event()
        def read(args, local):
            append(local, args.key)
            if args.key == 1:
                started.set()
                self.assertTrue(release.wait(2))
            return {'ok': True}
        tools = registry(read)
        with ThreadPoolExecutor(2) as pool:
            slow = pool.submit(tools.execute, 'read', {'key': 1}, '', root.for_call('slow'))
            try:
                self.assertTrue(started.wait(2))
                fast = tools.execute('read', {'key': 2}, '', root.for_call('fast'))
            finally:
                release.set()
            late = slow.result(2)
        self.assertTrue(fast['ok'])
        self.assertEqual(late['error']['code'], 'state_conflict')
        self.assertEqual([r['id'] for r in root.ledger.issue_rows()], [2])
        self.assertEqual(root.ledger.stages[0].call_id, 'fast')
        self.assertEqual(root.call_id, '')

    def test_step_ownership_never_annotates_other_call_stages(self):
        ledger = EvidenceLedger()
        for call_id in ('a', 'b'):
            ledger.add('mantis', {'meta': {}}, grade='scoped', unapplied=(), call_id=call_id)
        ledger.record_step({'purpose': 'answer', 'filters': []}, 0, {'ok': True}, 'read', 'a')
        self.assertEqual(ledger.condition_steps[0]['stage_indices'], [0])
        self.assertNotIn('condition_step', ledger.stages[1].stage['meta'])

    def test_planning_state_is_not_committed_after_its_deadline(self):
        from backend.services.agent import planning
        from test_tool_condition_arguments import PROPOSAL, QUESTION
        root = context_for()
        root.enabled_sources['planning'] = True
        root.tool_timeout_seconds = 1
        session = planning.PlanningSession(QUESTION, {}, {})
        submit = planning.PlanningSession.submit
        now = [100.0]
        def late(local, **kwargs):
            result = submit(local, **kwargs)
            now[0] = 102.0
            return result
        with patch.object(execution.time, 'monotonic', side_effect=lambda: now[0]), \
             patch.object(planning.PlanningSession, 'submit', late), execution_scope(ExecutionBudget(150)):
            result = planning.planning_registry(session).execute('prepare_query', {'proposal': deepcopy(PROPOSAL)}, '', root)
        self.assertEqual(result['error']['code'], 'tool_timeout')
        self.assertIsNone(session.plan)
        self.assertFalse(getattr(root, 'local_state', {}))


class AtomicBudgetTests(unittest.TestCase):
    def test_final_time_reserve_is_reported_as_deadline_not_call_limit(self):
        from types import SimpleNamespace
        from backend.services.agent import orchestrator as agent
        budget = AgentBudget(8, 8, 2, 10)
        model = {'id': 'company'}
        run = SimpleNamespace(budget=budget, text_models=[model], failed_models={'decision': set(), 'text': set()})
        with patch.object(execution.time, 'monotonic', return_value=100), execution_scope(ExecutionBudget(105)):
            with self.assertRaises(agent.AgentStop) as caught:
                agent._request_turn(run, {}, budget.investigation_seconds, 'decision', retries=0, candidates=[model])
        self.assertEqual(caught.exception.reason, 'deadline')
        self.assertEqual(budget.model_requests, 0)

    def test_bound_request_does_not_hide_a_shorter_step_deadline(self):
        with patch.object(execution.time, 'monotonic', return_value=100.0) as clock, execution_scope(ExecutionBudget(200)):
            budget = AgentBudget(8, 8, 2, 1)
            def late():
                clock.return_value = 102
                budget.charge_model()
            with self.assertRaises(execution.StepTimeout):
                execution.run_with_deadline(1, late)
        self.assertEqual(budget.model_requests, 0)

    def test_concurrent_tool_charges_never_exceed_the_request_limit(self):
        budget = AgentBudget(8, 3, 2, 1)
        gate = Barrier(12)
        def charge(_):
            gate.wait(2)
            try:
                budget.charge_tool()
                return True
            except ToolBudgetExhausted:
                return False
        with ThreadPoolExecutor(12) as pool:
            self.assertEqual(sum(pool.map(charge, range(12))), 3)
        self.assertEqual(budget.tool_calls, 3)

    def test_concurrent_investigation_charges_preserve_final_reserve(self):
        budget = AgentBudget(8, 5, 2, 10)
        gate = Barrier(12)
        def charge(_):
            gate.wait(2)
            try:
                budget.charge_model(investigation=True)
                return True
            except AgentBudgetExhausted:
                return False
        with ThreadPoolExecutor(12) as pool:
            results = list(pool.map(charge, range(12)))
        self.assertEqual(sum(results), 6)
        budget.charge_model()
        budget.charge_model()
        with self.assertRaises(AgentBudgetExhausted):
            budget.charge_model()
        self.assertEqual(budget.model_requests, 8)

    def test_bound_request_cancellation_prevents_charging_outside_context(self):
        request = ExecutionBudget(time.monotonic() + 10)
        with execution_scope(request):
            budget = AgentBudget(8, 8, 2, 1)
        request.cancel()
        for charge in (budget.charge_model, budget.charge_tool):
            with self.assertRaises(RequestStopped):
                charge()
        self.assertEqual((budget.model_requests, budget.tool_calls), (0, 0))

    def test_noncooperative_late_return_is_a_step_timeout(self):
        with patch.object(execution.time, 'monotonic', return_value=100.0) as clock:
            def late():
                clock.return_value = 102
                return 'late'
            with execution_scope(ExecutionBudget(200)), self.assertRaises(execution.StepTimeout):
                execution.run_with_deadline(1, late)

    def test_worker_inherits_policy_and_rejects_result_after_cancel(self):
        budget = ExecutionBudget(time.monotonic() + 10)
        started, release = Event(), Event()
        seen = []
        def work():
            seen.append((execution.CURRENT_BUDGET.get() is budget, llm.model_transfer_allowed({'id': 'forbidden'})))
            started.set()
            release.wait(2)
            return 'late'
        with ThreadPoolExecutor(1) as pool, execution_scope(budget), llm.model_transfer_scope(lambda model: False):
            future = execution.submit_with_context(pool, work)
            try:
                self.assertTrue(started.wait(2))
                budget.cancel()
            finally:
                release.set()
            with self.assertRaises(RequestStopped):
                future.result(2)
        self.assertEqual(seen, [(True, False)])

    def test_queued_work_and_publication_are_rejected_after_cancellation(self):
        budget = ExecutionBudget(time.monotonic() + 10)
        started, release = Event(), Event()
        writes = []
        def occupy():
            started.set()
            release.wait(2)
        with ThreadPoolExecutor(1) as pool, execution_scope(budget):
            running = execution.submit_with_context(pool, occupy)
            try:
                self.assertTrue(started.wait(2))
                queued = execution.submit_with_context(pool, writes.append, 'queued')
                budget.cancel()
                with self.assertRaises(RequestStopped):
                    budget.publish(lambda: writes.append('published'))
                with self.assertRaises(CancelledError):
                    queued.result(1)
            finally:
                release.set()
            with self.assertRaises(RequestStopped):
                running.result(2)
        self.assertEqual(writes, [])
