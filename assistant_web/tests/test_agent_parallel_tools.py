"""병렬 호출은 겹쳐 실행하되 조건·출처·순번·취소 경계는 순차 실행과 같아야 한다."""
from copy import deepcopy
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from types import SimpleNamespace
from threading import Barrier, Event
import time
import unittest
from unittest.mock import patch

from backend.execution import ExecutionBudget, RequestStopped, StepTimeout, execution_scope, submit_with_context
from backend.services.agent.orchestrator import AgentBudget, _run_tools
from backend.services.agent.policy import AgentSettings
from backend.services.agent.protocol import AssistantTurn, ToolCall
from backend.tools.catalog import agent_registry
from backend.tools.call_state import begin_call, publish_group
from backend.tools.parallel import Invocation, execute_calls, independent
from test_tool_condition_arguments import arguments, context_for, scanned


def calls(ctx):
    return [Invocation('count', 'count_mantis_issues', arguments(ctx)),
            Invocation('list', 'list_mantis_issues', {**arguments(ctx), 'limit': 3})]


class ParallelToolTests(unittest.TestCase):
    def test_overlapping_reads_merge_in_model_order_with_own_conditions(self):
        ctx = context_for(); gate = Barrier(2); release = Event(); retained = []
        tools = agent_registry(query_tools=True)
        original = tools._execute
        def run(name, args, error, local):
            result = original(name, args, error, local)
            retained.append(local)
            gate.wait(2)
            if name == 'count_mantis_issues':
                self.assertTrue(release.wait(2))
            else:
                release.set()
            self.assertFalse(ctx.ledger.stages)
            return result
        with patch.object(tools, '_execute', side_effect=run), \
             patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=lambda *a, **k: deepcopy(scanned())):
            results = execute_calls(tools, calls(ctx), ctx, concurrency=2)
        self.assertTrue(all(r.result['ok'] for r in results), results)
        self.assertEqual([r.group_size for r in results], [2, 2])
        self.assertEqual([s.call_id for s in ctx.ledger.stages], ['count', 'list'])
        self.assertEqual([s['stage_indices'] for s in ctx.ledger.condition_steps], [[0], [1]])
        self.assertEqual([r['operation'] for r in ctx.ledger.query_results()], ['count', 'list'])
        self.assertEqual(ctx.ledger.query_results()[1]['ordered_ids'], [44, 43, 42])
        self.assertEqual(ctx.ledger.query_results()[0]['total_count'], 4)
        retained[0].ledger.stages[0].stage['meta']['found_count'] = 999
        self.assertEqual(ctx.ledger.stages[0].stage['meta']['found_count'], 4)

    def test_missing_unresolved_filter_still_fails_and_keeps_other_result(self):
        ctx = context_for(); work = calls(ctx)
        work[0].arguments['filters'] = []
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()):
            results = execute_calls(agent_registry(query_tools=True), work, ctx, concurrency=2)
        self.assertEqual(results[0].result['error']['code'], 'condition_mismatch')
        self.assertTrue(results[1].result['ok'])
        self.assertEqual([s.call_id for s in ctx.ledger.stages], ['list'])
        self.assertEqual(ctx.ledger.condition_steps[0]['stage_indices'], [0])

    def test_cancelled_wave_never_publishes(self):
        ctx = context_for(); budget = ExecutionBudget(time.monotonic() + 10); gate = Barrier(2)
        def source(*a, **k):
            gate.wait(2)
            budget.cancel()
            return scanned()
        with execution_scope(budget), patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=source):
            with self.assertRaises(RequestStopped):
                execute_calls(agent_registry(query_tools=True), calls(ctx), ctx, concurrency=2)
        self.assertFalse(ctx.ledger.stages)
        self.assertFalse(ctx.signatures)

    def test_parallel_charges_respect_shared_tool_cap(self):
        ctx = context_for(); budget = AgentBudget(16, 1, 2, 10); ctx.charge_tool = budget.charge_tool
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()):
            results = execute_calls(agent_registry(query_tools=True), calls(ctx), ctx, concurrency=2)
        self.assertEqual(sum(r.result['ok'] for r in results), 1)
        self.assertEqual(budget.tool_calls, 1)
        self.assertEqual(len(ctx.ledger.stages), 1)

    def test_new_references_duplicates_population_and_planning_are_sequential(self):
        ctx = context_for(); tools = agent_registry(query_tools=True); a, b = calls(ctx)
        self.assertTrue(independent(tools, a, b, ctx))
        self.assertFalse(independent(tools, a, Invocation('same', a.name, a.arguments), ctx))
        self.assertFalse(independent(tools, a, Invocation('ref', b.name, {'condition_ref': ctx.query_contracts['mantis'].ref}), ctx))
        self.assertFalse(independent(tools, b, Invocation('detail', 'get_mantis_issue_detail', {'issue_id': 43}), ctx))
        self.assertFalse(independent(tools, a, Invocation('plan', 'prepare_query', {}), ctx))
        self.assertFalse(independent(tools, a, Invocation('report', 'build_issue_report', {}), ctx))
        ctx.query_contracts['mantis'].population_limit = 100
        self.assertFalse(independent(tools, a, b, ctx))

    def test_sequential_setting_reuses_query_cache_and_preserves_duplicate_guard(self):
        ctx = context_for(); work = calls(ctx)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()) as source:
            results = execute_calls(agent_registry(query_tools=True), [*work, work[0]], ctx, concurrency=1)
        self.assertEqual(source.call_count, 1)
        self.assertEqual([r.group_size for r in results], [1, 1, 1])
        self.assertEqual(results[2].result['error']['code'], 'duplicate_call')

    def test_at_most_two_workers_then_known_details_and_context_propagation(self):
        ctx = context_for(); tools = agent_registry(query_tools=True); gate = Barrier(2)
        policy = ContextVar('parallel_test_policy', default='denied'); policy.set('allowed')
        retained = []
        def detail(args, local):
            self.assertEqual(policy.get(), 'allowed')
            self.assertEqual(len(ctx.ledger.stages), 2)
            retained.append(args.issue_id)
            gate.wait(2)
            local.ledger.add('mantis', {'source': 'mantis', 'issues_list': [{'id': args.issue_id}], 'meta': {}},
                             grade='scoped', unapplied=(), call_id=local.call_id)
            return {'ok': True}
        tools.specs['get_mantis_issue_detail'] = replace(tools.specs['get_mantis_issue_detail'], run=detail)
        work = [*calls(ctx), Invocation('d1', 'get_mantis_issue_detail', {'issue_id': 43}),
                Invocation('d2', 'get_mantis_issue_detail', {'issue_id': 42})]
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()):
            results = execute_calls(tools, work, ctx, concurrency=99)
        self.assertEqual([r.group for r in results], [1, 1, 2, 2])
        self.assertEqual([r.group_size for r in results], [2] * 4)
        self.assertEqual(set(retained), {43, 42})
        self.assertEqual([s.call_id for s in ctx.ledger.stages], ['count', 'list', 'd1', 'd2'])
        self.assertEqual([s['stage_indices'] for s in ctx.ledger.condition_steps], [[0], [1], [2], [3]])

    def test_one_timeout_discards_only_its_provisional_evidence(self):
        ctx = context_for(); tools = agent_registry(query_tools=True)
        def fail(args, local):
            local.notion_read_cache['bad'] = {'content': 'unverified'}
            raise StepTimeout()
        tools.specs['count_mantis_issues'] = replace(tools.specs['count_mantis_issues'], run=fail)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()):
            results = execute_calls(tools, calls(ctx), ctx, concurrency=2)
        self.assertEqual(results[0].result['error']['code'], 'tool_timeout')
        self.assertTrue(results[1].result['ok'])
        self.assertNotIn('bad', ctx.notion_read_cache)
        self.assertEqual(ctx.ledger.condition_steps[1]['stage_indices'], [0])

    def test_group_collision_rejects_second_delta_without_losing_first(self):
        ctx = context_for(); a, b = [begin_call(ctx.for_call(key)) for key in ('a', 'b')]
        a.context.notion_read_cache['shared'] = {'content': 'first'}
        b.context.notion_read_cache['shared'] = {'content': 'second'}
        b.context.notion_read_cache['not_published'] = {}
        self.assertEqual(publish_group([a, b]), [True, False])
        self.assertEqual(ctx.notion_read_cache, {'shared': {'content': 'first'}})

    def test_group_rejects_external_owner_change_and_duplicate_notion_refs(self):
        ctx = context_for(); a, b = [begin_call(ctx.for_call(key)) for key in ('a', 'b')]
        a.context.ledger.notion_refs['page-a'] = 'N1'
        b.context.ledger.notion_refs['page-b'] = 'N1'
        self.assertEqual(publish_group([a, b]), [True, False])
        self.assertEqual(ctx.ledger.notion_refs, {'page-a': 'N1'})
        self.assertEqual(publish_group([a, b]), [False, False])

    def test_notion_invalidation_merges_with_count_without_resurrecting_body(self):
        ctx = context_for()
        ctx.ledger.add('notion', {'notion_pages': [{'id': 'page', 'content': 'old', '_verified_body': True}]},
                       grade='scoped', unapplied=(), call_id='earlier')
        tools = agent_registry(query_tools=True)
        def fail(args, local):
            raise StepTimeout()
        tools.specs['get_notion_page_content'] = replace(tools.specs['get_notion_page_content'], run=fail)
        work = [calls(ctx)[0], Invocation('body', 'get_notion_page_content', {'page_ref': 'N1'})]
        self.assertTrue(independent(tools, *work, ctx))
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()):
            results = execute_calls(tools, work, ctx, concurrency=2)
        self.assertTrue(results[0].result['ok'])
        self.assertEqual(results[1].result['error']['code'], 'tool_timeout')
        self.assertEqual(ctx.ledger.notion_page('page')['content'], '')
        self.assertNotIn('_verified_body', ctx.ledger.notion_page('page'))
        ctx.related_ordinal = 2
        self.assertFalse(independent(tools, *work, ctx))
        ctx.related_ordinal = None
        self.assertFalse(independent(tools, work[1], Invocation('body2', work[1].name, {'page_ref': 'N1'}), ctx))

    def test_loop_preserves_call_ids_and_limits_and_records_actual_execution(self):
        import json
        ctx = context_for()
        settings = AgentSettings(max_calls_per_turn=2, max_concurrent_tools=2)
        run = SimpleNamespace(model={'supportsParallelToolCalls': True}, settings=settings, tools=ctx,
            budget=AgentBudget(16, 16, 2, 1), registry=agent_registry(query_tools=True), trace=[], plan={})
        work = [*calls(ctx), Invocation('overflow', 'count_mantis_issues', arguments(ctx))]
        turn = AssistantTurn('tool_calls', tool_calls=tuple(ToolCall(c.call_id, c.name, json.dumps(c.arguments)) for c in work))
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()):
            messages = _run_tools(run, turn)
        self.assertEqual([m['tool_call_id'] for m in messages], ['count', 'list', 'overflow'])
        self.assertEqual(json.loads(messages[2]['content'])['error']['code'], 'not_executed_call_limit')
        self.assertEqual([t.get('concurrentTools') for t in run.trace], [2, 2, None])
        self.assertEqual([s.call_id for s in ctx.ledger.stages], ['count', 'list'])

    def test_request_store_cold_reads_pin_one_generation(self):
        from backend.sources import mantis_store
        started = Event(); release = Event()
        def fetch(**kwargs):
            started.set()
            self.assertTrue(release.wait(2))
            return [object()]
        with mantis_store.store_request_scope(), patch.object(mantis_store, 'store_path', return_value='fixture'), \
             patch.object(mantis_store, 'connection_key', return_value='connection'), \
             patch.object(mantis_store, '_read_complete_store', side_effect=fetch) as source, ThreadPoolExecutor(2) as pool:
            first = submit_with_context(pool, mantis_store.read_complete_store)
            self.assertTrue(started.wait(2))
            second = submit_with_context(pool, mantis_store.read_complete_store)
            release.set()
            self.assertIs(first.result(2), second.result(2))
        self.assertEqual(source.call_count, 1)

    def test_concurrency_setting_is_opt_in_and_bounded(self):
        import os
        for value, expected in [(None, 1), ('0', 1), ('2', 2), ('99', 2)]:
            env = {} if value is None else {'AGENT_MAX_CONCURRENT_TOOLS': value}
            with patch.dict(os.environ, env, clear=True):
                self.assertEqual(AgentSettings.from_env().max_concurrent_tools, expected)

    def test_observer_counts_parallel_attempts_and_only_published_body_evidence(self):
        from scripts.accuracy.benchmark_observation import observe_requests
        from backend.services.agent import orchestrator
        ctx = context_for(); tools = agent_registry(query_tools=True)
        ctx.ledger.add('notion', {'notion_pages': [{'id': 'page', 'content': ''}]},
                       grade='scoped', unapplied=(), call_id='earlier')
        def body(args, local):
            return {'ok': True, 'tool': 'get_notion_page_content', 'results': [{'ref': 'N1', 'body_checked': True, 'content_complete': True, 'content': '현재 본문'}]}
        tools.specs['get_notion_page_content'] = replace(tools.specs['get_notion_page_content'], run=body)
        work = [calls(ctx)[0], Invocation('body', 'get_notion_page_content', {'page_ref': 'N1'})]
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()), observe_requests({}) as observation:
            executed = execute_calls(tools, work, ctx, concurrency=2)
            self.assertEqual(observation.report()['evidence_reads'], [])
            messages = [{'role': 'tool', 'content': orchestrator.fit_result(item.result, 6000)} for item in executed]
            observation.record_model_delivery({'messages': messages})
        self.assertEqual(observation.report()['logical_tool_attempts'], 2)
        self.assertEqual(len(observation.report()['evidence_reads']), 1)
        # 게시 충돌은 성공한 본문 읽기로 기록하지 않는다.
        ctx = context_for(); a, b = [begin_call(ctx.for_call(key)) for key in ('a', 'b')]
        ctx._call_owner.version += 1
        with observe_requests({}) as observation:
            tools.publish_pending([('get_notion_page_content', a, body(None, None)), ('count_mantis_issues', b, {'ok': True})])
        self.assertFalse(observation.report()['evidence_reads'])

    def test_existing_evidence_dependency_and_overlapping_details_stay_sequential(self):
        ctx = context_for(); tools = agent_registry(query_tools=True)
        ctx.confirmed_issue_ids = frozenset({42, 43})
        a = Invocation('read', 'get_mantis_issue_detail', {'issue_id': 42})
        b = Invocation('batch', 'get_mantis_issue_details', {'issue_ids': [42, 43]})
        self.assertFalse(independent(tools, a, b, ctx))
        dependent = calls(ctx)[0]
        dependent.arguments['filters'][0]['evidence_ref'] = 'M#42'
        dependent.arguments['purpose'] = 'investigate'
        self.assertFalse(independent(tools, a, dependent, ctx))

    def test_parallel_stage_timing_keeps_both_durations(self):
        from backend import timing
        from threading import local
        gate = Barrier(2); counters = local(); recorded = {}
        def clock():
            counters.calls = getattr(counters, 'calls', 0) + 1
            if counters.calls == 2:
                gate.wait(2)
                return 1.0
            return 0.0
        action = timing.measured('parallel_read')(lambda: None)
        token = timing.TIMINGS.set(recorded)
        try:
            with patch.object(timing, 'perf_counter', side_effect=clock), ThreadPoolExecutor(2) as pool:
                futures = [submit_with_context(pool, action) for _ in range(2)]
                for future in futures:
                    future.result(2)
        finally:
            timing.TIMINGS.reset(token)
        self.assertEqual(recorded['parallel_read'], {'calls': 2, 'durationMs': 2000.0})

    def test_different_customer_scopes_keep_own_conditions_and_final_numeric_check(self):
        from test_agent_multi_query import comparison_context, evaluated, scan, scope_args
        plan, ctx = comparison_context(); gate = Barrier(2)
        work = [Invocation(scope, 'count_mantis_issues', scope_args(ctx, scope)) for scope in ('S1', 'S2')]
        def source(limit, constraints, **kwargs):
            gate.wait(2)
            return scan([1] if constraints['customer_key'] == '한투' else [2, 3])
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', side_effect=source):
            results = execute_calls(agent_registry(query_tools=True), work, ctx, concurrency=2)
        self.assertTrue(all(r.result['ok'] for r in results), results)
        self.assertEqual(ctx.ledger.query_results(), [])
        self.assertEqual(ctx.ledger.query_results('S1')[0]['total_count'], 1)
        self.assertEqual(ctx.ledger.query_results('S2')[0]['total_count'], 2)
        self.assertEqual([(s['query_scope_id'], s['stage_indices']) for s in ctx.ledger.condition_steps], [('S1', [0]), ('S2', [1])])
        self.assertEqual(evaluated(plan, ctx)[2]['status'], 'pass')
