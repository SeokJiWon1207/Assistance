import unittest
from copy import deepcopy
from unittest.mock import patch

from agent_harness_cases import FakeModel, FakeSources, agent_environment, explain_context, final, tool_call, turn
from test_agent_harness import run


class AgentQueryOutcomeTests(unittest.TestCase):
    def test_verified_query_results_can_use_unused_final_calls_but_explanations_keep_the_reserve(self):
        from types import SimpleNamespace
        from backend.services.agent import orchestrator as agent
        from backend.services.agent.policy import AgentSettings
        from backend.tools.ledger import EvidenceLedger
        from backend.services.agent.protocol import parse_assistant_turn
        for has_query in (False,True):
            ledger=EvidenceLedger()
            if has_query:
                ledger.add('mantis',{'source':'mantis_snapshot','status':'ok','issues_list':[],
                    'meta':{'query_result':{'operation':'count','total_count':6,'search_complete':True}}},
                    grade='scoped',unapplied=(),call_id='count')
            state=SimpleNamespace(settings=AgentSettings(),model={'id':'test'},
                budget=agent.AgentBudget(8,8,2,45,model_requests=6),ledger=ledger,
                registry=SimpleNamespace(definitions=lambda _:[]),tools=SimpleNamespace(),
                messages=[],stop_reason='',nudged=False,decision_models=[])
            with (patch.object(agent,'_system_prompt',return_value=''),patch.object(agent,'_user_prompt',return_value=''),
                  patch.object(agent,'_query_outcome',return_value='partial_evidence'),
                  patch.object(agent,'_request_turn',return_value=parse_assistant_turn({'choices':[final('확인 결과')]})) as request):
                result=agent._investigate(state)
            self.assertEqual(request.call_count,1 if has_query else 0)
            self.assertEqual(state.budget.reserve_calls,0 if has_query else 2)
            self.assertEqual(state.budget.max_model_requests,8)
            self.assertEqual(state.budget.max_tool_calls,8)
            self.assertEqual(state.budget.reserve_seconds,5 if has_query else 45)
            self.assertEqual(result,'확인 결과' if has_query else None)

    def test_related_page_read_uses_the_remaining_investigation_budget(self):
        import time
        from backend.execution import CURRENT_BUDGET, ExecutionBudget
        from backend.tools.registry import ToolContext
        from backend.tools.catalog import agent_registry
        from backend.tools.ledger import EvidenceLedger
        context = ToolContext("관련 가이드", "관련 가이드", {'notion': True}, {}, frozenset(), EvidenceLedger(),
                              lambda: None, lambda seconds: min(seconds, 90), 20, 6000)
        token = CURRENT_BUDGET.set(ExecutionBudget(time.monotonic() + 140))
        self.addCleanup(CURRENT_BUDGET.reset, token)
        with patch('backend.tools.registry.run_with_deadline', return_value={'ok': True}) as invoke:
            agent_registry().execute('get_notion_page_content', {'page_ref': 'N1'}, '', context)
            self.assertEqual(invoke.call_args.args[0], 20)
            context.signatures.clear()
            context.related_ordinal = 2
            agent_registry().execute('get_notion_page_content', {'page_ref': 'N1'}, '', context)
            self.assertEqual(invoke.call_args.args[0], 90)

    def test_compound_repair_receives_the_rejected_plan_and_keeps_sort_out_of_filters(self):
        from backend.intent import unified
        import json
        q = "전사 한투 미해결 이슈가 몇 건이야? 최근 3건과 두 번째 이슈의 Notion 가이드도 설명해줘."
        correct = {"source": "both", "operation": "explain", "scope": "all", "followup": False,
                   "filters": [{"field": "customer", "operator": "equals", "value": "한투", "evidence": "한투"}],
                   "status": {"mode": "open", "values": [], "evidence": "미해결"}, "clear_fields": [],
                   "terms": [], "group_by": None, "limit": 3, "sort": {"field": "created_at", "direction": "desc"}}
        rejected = deepcopy(correct)
        rejected["filters"].append({"field": "sort", "operator": "equals", "value": correct["sort"], "evidence": "최근 3건"})
        attempts = []

        def propose(_model, _key, payload):
            attempts.append(deepcopy(payload))
            return rejected if len(attempts) == 1 else correct

        with patch.object(unified, '_propose', side_effect=propose):
            plan = unified.plan_question(q, {"id": "test"}, "test", {}, {}, ordinal_guide_tools=True)
        self.assertEqual(plan["resolved_constraints"]["statuses"], ["open"])
        self.assertIsNone(plan["resolved_constraints"]["created_at"])
        self.assertEqual(attempts[1]["messages"][-2]["role"], "assistant")
        self.assertEqual(json.loads(attempts[1]["messages"][-2]["content"]), rejected)
        self.assertIn("최상위 sort", attempts[1]["messages"][0]["content"])

    def test_compound_guide_retries_and_rejects_a_plan_that_drops_the_issue_status_or_month(self):
        from backend.intent import unified
        proposal = {"source": "both", "operation": "explain", "scope": "all", "followup": False,
                    "filters": [{"field": "customer", "operator": "equals", "value": "한투", "evidence": "한투"}],
                    "status": {"mode": "unspecified", "values": [], "evidence": ""},
                    "clear_fields": [], "terms": [], "group_by": None, "limit": 3}
        q = "전사 한투 미해결 이슈가 몇 건이야? 최근 3건과 두 번째 이슈의 Notion 가이드도 설명해줘."
        fixed = {**proposal, "status": {"mode": "open", "values": [], "evidence": "미해결"}}
        with patch.object(unified, '_propose', side_effect=[proposal, fixed]) as propose:
            result = unified.plan_question(q, {"id": "test"}, "test", {}, {}, ordinal_guide_tools=True)
            self.assertEqual(propose.call_count, 2)
            self.assertEqual(result["resolved_constraints"]["statuses"], ["open"])
        with patch.object(unified, '_propose', return_value=fixed):
            result = unified.plan_question("이번 달 " + q, {"id": "test"}, "test", {}, {}, ordinal_guide_tools=True)
            self.assertEqual(result["operation"], "clarify")
        with patch.object(unified, '_propose', return_value=proposal):
            q = "전사 한투 이슈가 몇 건이야? 최근 3건과 두 번째 이슈의 Notion에서 지난달 작성한 '미해결 이슈' 가이드도 설명해줘."
            plan = unified.plan_question(q, {"id": "test"}, "test", {}, {}, ordinal_guide_tools=True)
            self.assertEqual(plan["resolved_constraints"]["statuses"], [])
            self.assertIsNone(plan["resolved_constraints"]["created_at"])

    def test_planner_can_represent_the_list_and_dependent_guide_without_guessing_the_issue(self):
        from backend.intent import unified
        q = "NH투자증권 Mantis 이슈의 최근 3건과 두 번째 이슈에 관련된 Notion 가이드를 설명해줘."
        proposal = {"source": "both", "operation": "explain", "scope": "all", "followup": False,
                    "filters": [{"field": "customer", "operator": "equals", "value": "NH", "evidence": "NH투자증권"}],
                    "status": {"mode": "unspecified", "values": [], "evidence": ""},
                    "clear_fields": [], "terms": [], "group_by": None, "limit": 3}
        with patch.object(unified, '_propose', return_value=proposal) as propose:
            plan = unified.plan_question(q, {"id": "test"}, "test", {}, {}, ordinal_guide_tools=True)
            self.assertIn("list_mantis_issues", propose.call_args.args[2]["messages"][0]["content"])
            self.assertEqual(plan["resolved_constraints"]["issue_ids"], [])
            self.assertEqual(plan["operation"], "explain")
            unified.plan_question(q, {"id": "test"}, "test", {}, {})
            self.assertNotIn("list_mantis_issues", propose.call_args.args[2]["messages"][0]["content"])

    def test_verified_previous_ordinal_can_read_just_the_bound_title(self):
        from backend.services.agent.query_shadow import query_shadow_scope
        from backend.services.agent.route import run_query_shadow_if_requested
        from backend.services.agent.policy import request_transfer_scope
        from agent_harness_cases import chat_request

        q = "그중 두 번째 이슈 번호와 제목만 다시 알려줘."
        model = FakeModel([turn(tool_call('get_mantis_issue_detail', {'issue_id': 52, 'projection': 'title'})),
                           final('잘못된 제목입니다.')])
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              query_shadow_scope(), patch('backend.tools.mantis_tools.execute_mantis_id_lookup', return_value={
                  'source': 'mantis_id_lookup', 'status': 'ok', 'meta': {'search_complete': True},
                  'issues_list': [{'id': 52, 'summary': '서버가 확인한 제목', 'status': 'assigned'}]}) as lookup):
            request = chat_request(q, contextAuditId='a' * 32)
            ctx = explain_context(q, source='mantis', constraints={'issue_ids': [52]})
            ctx.intent_plan['operation'] = 'get'
            self.assertEqual(run_query_shadow_if_requested(request, ctx)['raw']['mode'], 'agent_query_shadow_skipped')
            ctx.ordinal_source_id = 52
            result = run_query_shadow_if_requested(request, ctx)
        self.assertEqual(result['content'], '#52 서버가 확인한 제목')
        self.assertEqual([s['issue_id'] for s in result['sources']], [52])
        self.assertEqual(lookup.call_count, 1)

    def test_empty_linked_page_opens_an_alternate_guide_and_preserves_list_order(self):
        from backend.services.agent.query_shadow import query_shadow_scope
        from backend.services.agent.route import run_query_shadow_if_requested
        from backend.services.agent.policy import request_transfer_scope
        from agent_harness_cases import chat_request, notion_page, notion_stage

        q = "한투 미해결 이슈 최근 3건과 두 번째 이슈의 Notion 사용 방법을 알려줘"
        model = FakeModel([turn(tool_call('list_mantis_issues', {'limit': 3})),
                           turn(tool_call('search_notion_pages', {'query': '비교차트'})),
                           turn(tool_call('get_notion_page_content', {'page_ref': 'N1'})), final('모델 추측')])
        rows = [{'id': i, 'summary': title} for i, title in [(60, '기타'), (52, '비교차트 업데이트'), (41, '서버 오류')]]
        scanned = {'source': 'mantis_snapshot', 'status': 'ok', 'meta': {'search_complete': True, 'found_count': 3},
                   'issues_list': rows}
        ticket = notion_page('ticket', '비교차트 업데이트 확인 요청', '')
        guide = notion_page('guide', '비교차트 등록방법', '')
        ticket['match_method'] = 'notion_catalog_snapshot'
        excerpt = "비교차트 화면에서 종목 추가를 선택합니다."
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              query_shadow_scope(), patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned),
              patch('backend.tools.notion_tools.execute_notion_search', return_value=notion_stage(ticket)),
              patch('backend.notion.read_api.fetch_notion_page_content', return_value={'text': '', 'segments': []}),
              patch('backend.notion.related_guides.find_alternate_guide',
                    return_value=(guide, excerpt, {'outcome': 'found', 'checked_count': 2})) as retry):
            context = explain_context(q, source='both',
                constraints={'statuses': ['open'], 'filter_clauses': [{'field': 'status', 'operator': 'in', 'value': ['open']}]})
            context.intent_plan.update(answer_sections=['results', 'explanation'], explanation_sources=['notion'])
            with patch('backend.services.agent.orchestrator._grounded', side_effect=AssertionError('문서 설명을 이슈 설명으로 재실행')):
                result = run_query_shadow_if_requested(chat_request(q), context)
        self.assertTrue(retry.called)
        self.assertIn(excerpt, result['content'])
        self.assertEqual(result['agentReview']['retrievalOutcome'], 'sufficient_evidence')
        reads = result['agentReview']['relatedReads']
        self.assertEqual([(r['pageId'], r['issue_id']) for r in reads], [('ticket', 52), ('guide', 52)])
        self.assertEqual([s['issue_id'] for s in result['sourcesForFollowUps']], [60, 52, 41])

    def test_second_list_item_can_lead_to_an_opened_guide_in_separate_reference_scope(self):
        from backend.services.agent.query_shadow import query_shadow_scope
        from backend.services.agent.route import run_query_shadow_if_requested
        from backend.services.agent.policy import request_transfer_scope
        from agent_harness_cases import chat_request, notion_page, notion_stage

        q = "미해결 이슈가 몇 건이야? 최근 3건과 두 번째 이슈의 Notion 가이드도 설명해줘"
        model = FakeModel([turn(tool_call('count_mantis_issues', {})), turn(tool_call('list_mantis_issues', {'limit': 3})),
                           turn(tool_call('search_notion_pages', {'query': '비교차트'})),
                           turn(tool_call('get_notion_page_content', {'page_ref': 'N1'})), final('외부 추측입니다.')])
        rows = [{'id': i, 'summary': title, 'status': 'assigned'}
                for i, title in [(66, '다른 기능'), (52, '비교차트 업데이트'), (41, '설정 오류')]]
        scanned = {'source': 'mantis_snapshot', 'status': 'ok', 'meta': {'search_complete': True, 'found_count': 3},
                   'issues_list': rows}
        page = notion_page('manual', '비교차트 사용 가이드', '')
        page['match_method'] = 'notion_catalog_snapshot'
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              query_shadow_scope(), patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned),
              patch('backend.tools.notion_tools.execute_notion_search', return_value=notion_stage(page)),
              patch('backend.notion.read_api.fetch_notion_page_content', return_value={
                  'text': '비교차트에서 종목 추가를 선택합니다.',
                  'segments': [{'page_id': 'manual', 'text': '비교차트에서 종목 추가를 선택합니다.'}]})):
            result = run_query_shadow_if_requested(chat_request(q), explain_context(q, source='both',
                constraints={'statuses': ['open'], 'filter_clauses': [{'field': 'status', 'operator': 'in', 'value': ['open']}]}))
        self.assertIn('#52', result['content'])
        self.assertIn('종목 추가를 선택합니다', result['content'])
        self.assertNotIn('외부 추측', result['content'])
        self.assertEqual(result['agentReview']['relatedReads'][0]['issue_id'], 52)
        self.assertEqual([s['issue_id'] for s in result['sourcesForFollowUps']], [66, 52, 41])

    def test_verified_list_size_is_limited_by_the_true_total_and_its_order(self):
        from backend.evaluation.agent_queries import verify_query_results

        stage = {'source': 'mantis_snapshot', 'status': 'ok', 'meta': {'found_count': 2, 'search_complete': True,
                 'matched_issue_ids': [42, 50], 'ordered_matched_ids': [50, 42],
                 'query_result': {'operation': 'list', 'ordered_ids': [50, 42], 'total_count': 2}},
                 'issues_list': [{'id': 50}, {'id': 42}]}
        text = "적용 조건에 맞는 Mantis 이슈는 2건입니다.\n1. #50 예제\n2. #42 예제"
        self.assertEqual(verify_query_results(text, [stage], 3)['status'], 'pass')
        for wrong in (text.replace('2건', '3건'), text.replace('#50', '#42'), text.replace('2. #42 예제', '')):
            self.assertEqual(verify_query_results(wrong, [stage], 3)['status'], 'fail')

    def test_shadow_composes_server_count_and_list_and_rejects_a_nonexistent_second_item(self):
        from backend.services.agent.query_shadow import query_shadow_scope
        from backend.services.agent.route import run_query_shadow_if_requested
        from backend.services.agent.policy import request_transfer_scope
        from agent_harness_cases import chat_request

        question = "한투 미해결 이슈가 몇 건이야? 최근 3건과 두 번째 이슈의 Notion 가이드를 알려줘"
        model = FakeModel([turn(tool_call('count_mantis_issues', {})),
                           turn(tool_call('list_mantis_issues', {'limit': 3})),
                           turn(tool_call('search_notion_pages', {'query': '오류 가이드'})),
                           final('99건이고 두 번째 이슈가 있습니다.')])
        stage = {'source': 'mantis_snapshot', 'status': 'ok', 'meta': {'search_complete': True, 'found_count': 1},
                 'issues_list': [{'id': 42, 'summary': '차트 동작 오류', 'status': 'assigned'}]}
        with (agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage),
              patch('backend.tools.notion_tools.execute_notion_search') as notion):
            context = explain_context(question, source='both')
            request = chat_request(question)
            self.assertIsNone(run_query_shadow_if_requested(request, context))
            with query_shadow_scope():
                result = run_query_shadow_if_requested(request, context)
        notion.assert_not_called()
        self.assertIn('1건', result['content'])
        self.assertIn('#42', result['content'])
        self.assertIn('두 번째', result['content'])
        self.assertNotIn('99건', result['content'])
        self.assertEqual(result['agentReview']['queryResults'][-1]['ordered_ids'], [42])

    def test_query_tools_share_exact_total_and_keep_order_without_exposing_unlisted_ids(self):
        from backend.tools.catalog import agent_registry
        from backend.tools.conditions import project_conditions
        from backend.tools.ledger import EvidenceLedger
        from backend.tools.registry import ToolContext

        filters = [{'field': 'customer', 'operator': 'equals', 'value': '한투'}]
        context = ToolContext("한투 이슈 최근 2건", "한투 이슈 최근 2건", {'mantis': True}, {
            'mantis': project_conditions({'source': 'mantis'}, {'filter_clauses': filters}, 'mantis')},
            frozenset(), EvidenceLedger(), lambda: None, lambda seconds: seconds, 20, 6000)
        rows = [{'id': i, 'summary': f'이슈 {i}', 'status': 'assigned'} for i in [7, 9, 3]]
        stage = {'source': 'mantis_snapshot', 'status': 'ok', 'meta': {
            'search_complete': True, 'found_count': 3, 'fetchedAt': '2026-11-05T00:00:00Z'}, 'issues_list': rows}
        for complete in (True, False):
            context.signatures.clear()
            context.ledger = EvidenceLedger()
            stage['meta']['search_complete'] = complete
            registry = agent_registry(query_tools=True)
            with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=deepcopy(stage)) as scan:
                count = registry.execute('count_mantis_issues', {}, '', context)
                listed = registry.execute('list_mantis_issues', {'limit': 2}, '', context)
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(count['total_count'], 3 if complete else None)
            self.assertEqual(listed['ordered_ids'], [7, 9])
            self.assertEqual(set(context.ledger.refs()), {'M#7', 'M#9'})
            self.assertEqual(count['applied_filters'], filters)
            self.assertEqual(listed['search_complete'], complete)

    def test_zero_is_confirmed_only_for_a_complete_scoped_query(self):
        for complete, expected in ((True, 'complete_empty'), (False, 'search_incomplete')):
            with self.subTest(complete=complete):
                question = "사내 배포 가이드에 해당 절차가 있는지 설명해줘"
                model = FakeModel([turn(tool_call('search_notion_pages', {'query': '배포 가이드'})), final('확인했습니다.')])
                stage = {'source': 'notion', 'status': 'ok', 'notion_pages': [], 'meta': {
                    'search_complete': complete, 'candidate_count_complete': complete, 'candidate_count': 0,
                    'incomplete_reasons': [] if complete else ['live_search_time_limit']}}
                with (agent_environment(model=model, sources=FakeSources()),
                      patch('backend.tools.notion_tools.execute_notion_search', return_value=stage)):
                    result = run(explain_context(question, source='notion'))
                self.assertEqual(result['agentReview']['stopReason'], expected)
                self.assertEqual(result['agentReview']['finalization'], expected)
                self.assertIn('0건' if complete else '전체 조회', result['content'])
