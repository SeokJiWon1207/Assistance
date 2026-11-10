"""복합 응답의 각 결과와 검증을 보존하는 회귀."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from agent_harness_cases import FakeModel, FakeSources, agent_environment, chat_request, explain_context, final, tool_call, turn
from backend.evaluation.delivery import decide_delivery
from backend.evaluation.verification import build_verification_report
from backend.services.agent.planning import PlanningSession
from backend.services.agent.policy import request_transfer_scope
from backend.services.agent.route import run_agent_if_eligible
from backend.sources.mantis_collection import IssueBatch
from test_agent_report_tools import report_plan, rows
from test_tool_condition_arguments import PROPOSAL, arguments, context_for


class CommonResponseTests(unittest.TestCase):
    def test_model_presentation_is_preserved_without_changing_conditions(self):
        q = '전사 한투 미해결 이슈 최근 3건을 보여주고 설명해줘.'
        session = PlanningSession(q, {}, {})
        result = session.submit(proposal={**deepcopy(PROPOSAL), 'answer_sections': ['explanation', 'results']})
        self.assertTrue(result['ok'], result)
        self.assertEqual(session.plan.get('answer_sections'), ['explanation', 'results'])
        self.assertEqual(session.plan['resolved_constraints']['statuses'], ['open'])

    def test_model_can_select_explanation_source_without_changing_search_scope(self):
        q = '전사 한투 미해결 이슈 최근 3건과 두 번째 이슈의 Notion 가이드도 설명해줘.'
        session = PlanningSession(q, {}, {})
        result = session.submit(proposal={**deepcopy(PROPOSAL), 'source': 'both', 'operation': 'explain',
            'answer_sections': ['results', 'explanation'], 'explanation_sources': ['notion']})
        self.assertTrue(result['ok'], result)
        self.assertEqual(session.plan.get('explanation_sources'), ['notion'])
        self.assertEqual(session.plan['source'], 'both')
        self.assertEqual(session.plan['resolved_constraints']['statuses'], ['open'])

    def test_presentation_cannot_drop_results_or_inject_unverified_text(self):
        q = '전사 한투 미해결 이슈 최근 3건을 보여줘.'
        for sections in (['explanation'], ['results', '999건입니다'], ['results', 'results'], 'results'):
            session = PlanningSession(q, {}, {})
            self.assertFalse(session.submit(proposal={**deepcopy(PROPOSAL), 'answer_sections': sections})['ok'])
            self.assertIsNone(session.plan)
            fixed = session.submit(changes={'answer_sections': ['results']})
            self.assertTrue(fixed['ok'], fixed)
            self.assertEqual(session.plan['resolved_constraints']['statuses'], ['open'])

    def test_read_capability_supports_single_get_and_explanation_query_tools(self):
        from backend.services.agent.eligibility import agent_route_reason, uses_query_tools
        with agent_environment(), request_transfer_scope('company-test'):
            context = explain_context('이슈 #12345 내용을 알려줘.')
            context.intent_plan['operation'] = 'get'
            self.assertEqual(agent_route_reason(context), '')
            context.intent_plan['operation'] = 'explain'
            self.assertTrue(uses_query_tools(context))
            context.enabled_tools['mantis'] = False
            self.assertEqual(agent_route_reason(context), 'source_disabled')

    def test_tool_schema_only_offers_comparison_scopes_present_in_the_request(self):
        from backend.tools.catalog import agent_registry
        from backend.intent.unified_validation import validate_plan
        ctx = context_for(validate_plan('전사 한투 미해결 이슈 최근 3건을 보여줘.', deepcopy(PROPOSAL), {}, {}))
        registry = agent_registry(query_tools=True)
        def parameters():
            return next(t['function']['parameters'] for t in registry.definitions(ctx)
                        if t['function']['name'] == 'get_mantis_issue_detail')
        self.assertNotIn('query_scope_id', parameters()['properties'])
        # 정의에서 숨긴 인자를 임의로 보내도 기존 조건 검증은 계속 차단한다.
        result = registry.execute('get_mantis_issue_detail', {'issue_id': 29186, 'query_scope_id': 'S1'}, '', ctx)
        self.assertEqual(result['error']['code'], 'condition_mismatch')
        ctx.query_scopes = {'S2': ctx.query_contracts['mantis']}
        schema = parameters()['properties']['query_scope_id']
        self.assertEqual(schema['anyOf'][0]['enum'], ['S2'])
        ctx.query_scopes.clear()
        self.assertNotIn('query_scope_id', parameters()['properties'])

    def test_report_does_not_swallow_additional_list_or_its_numeric_validation(self):
        q, plan = report_plan('weekly')
        ctx = context_for(plan, q)
        args = arguments(ctx)
        raw = rows()[0]
        from backend.sources.mantis_fields import normalize_issue
        row = normalize_issue(raw)
        stage = {'source': 'mantis_snapshot', 'status': 'ok', 'issues_list': [row],
                 'meta': {'search_complete': True, 'found_count': 1, 'matched_issue_ids': [1], 'ordered_matched_ids': [1]}}
        model = FakeModel([turn(tool_call('build_issue_report', {**args, 'kind': 'weekly'})),
                           turn(tool_call('list_mantis_issues', {**args, 'limit': 5})),
                           final('총 999건입니다.')])
        with agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'), \
             patch('backend.reports.weekly_customer.load_report_issues', return_value=IssueBatch(rows(), {'search_complete': True})), \
             patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage):
            chat = explain_context(q, constraints=plan['resolved_constraints'])
            chat.intent_plan = plan
            chat.structured_tool_conditions = True
            response = run_agent_if_eligible(chat_request(q), chat)
        self.assertEqual(response['raw']['mode'], 'agent_composed', response['agentReview'])
        self.assertIn('1. #1 ', response['content'])
        self.assertNotIn('999', response['content'])
        report = build_verification_report(q, response['content'], response['sourcesForFollowUps'], response['retrieval'], 'agent_composed')
        self.assertTrue(decide_delivery(report).allowed, report['checks'])
        damaged = build_verification_report(q, response['content'].replace('1. #1 ', '1. #999 '),
                                            response['sourcesForFollowUps'], response['retrieval'], 'agent_composed')
        self.assertFalse(decide_delivery(damaged).allowed)
        self.assertIn('agent_query_result', {c['id'] for c in report['checks']})
        altered = deepcopy(response['retrieval'])
        part = next(p for p in altered['answer_parts'] if p['kind'] == 'query')
        part['content'] = part['content'].replace('1건입니다', '99건입니다')
        text = '\n\n'.join(p['content'].rstrip() for p in altered['answer_parts'])
        altered['answer_footer'] = ''
        tampered = build_verification_report(q, text, response['sourcesForFollowUps'], altered, 'agent_composed')
        self.assertIn('agent_query_result', decide_delivery(tampered).blocked_checks)

    def test_revoked_evidence_does_not_survive_in_an_earlier_result_part(self):
        from types import SimpleNamespace
        from backend.services.agent import orchestrator as agent
        revoked = {'content': '현재 원문 접근을 확인하지 못했습니다.', 'sources': [], 'sourcesForFollowUps': [],
                   'retrieval': None, 'raw': {'mode': 'explanation_evidence_incomplete'}}
        run = SimpleNamespace(plan={'answer_sections': ['results', 'explanation']}, collecting_parts=False)
        with patch.object(agent, '_result_parts', return_value=[('report', {'content': '접근 철회된 원문'})]), \
             patch.object(agent, '_finish_explanation', return_value=revoked):
            self.assertEqual(agent._finish(run, None), revoked)

    def test_single_lookup_with_a_detail_read_needs_an_explanation_beyond_the_id_title_list(self):
        from types import SimpleNamespace
        from backend.services.agent import orchestrator as agent
        from backend.tools.ledger import EvidenceLedger
        read, listed = EvidenceLedger(), EvidenceLedger()
        for ledger, meta in ((read, {'detail_read_id': 31699}), (listed, {})):
            ledger.add('mantis', {'source': 'mantis_snapshot', 'status': 'ok', 'issues_list': [], 'meta': meta},
                       grade='scoped', unapplied=(), call_id='call')
        run = lambda operation, ledger: SimpleNamespace(plan={'operation': operation, 'answer_sections': ['results']}, ledger=ledger)
        # '이 이슈는 어느 고객사 건이야?'처럼 상세를 읽은 단건 조회만 설명을 덧붙인다.
        self.assertTrue(agent._requires_explanation(run('get', read)))
        self.assertFalse(agent._requires_explanation(run('get', listed)))
        self.assertFalse(agent._requires_explanation(run('search', read)))

    def test_composition_verifier_rejects_missing_parts_and_recursive_modes(self):
        for retrieval in ({}, {'answer_parts': [{'mode': 'agent_composed', 'content': '임의 답변'}]}):
            report = build_verification_report('몇 건이야?', '0건입니다.', [], retrieval, 'agent_composed')
            self.assertFalse(decide_delivery(report).allowed)

    def test_cited_explanation_cannot_hide_an_invented_number_by_omitting_units(self):
        from backend.services.agent.citations import validate_citations, validate_quantities
        for text in ('처리 대상은 총 999입니다. [N1]', '대기 시간은 999초입니다. [N1]'):
            self.assertTrue(validate_quantities(text, {'N1': '대기 시간은 10초입니다.'}))
        self.assertFalse(validate_quantities('대기 시간은 10초입니다. [N1]', {'N1': '대기 시간은 10초입니다.'}))
        # ID 보정과 수치 검증이 서로 가로채지 않되, 함께 있는 수량은 여전히 검사한다.
        self.assertFalse(validate_quantities('#99999 이슈에서 확정됐습니다.', {}))
        self.assertEqual(validate_citations('#99999 이슈에서 확정됐습니다.', {}, set()).issues[0].reason, 'unknown_issue')
        self.assertTrue(validate_quantities('#99999 이슈의 대상은 999건입니다. [N1]', {'N1': '대상은 1건입니다.'}))

    def test_numbered_list_controls_followup_order_even_after_report_and_explanation(self):
        from backend.services.agent.response_parts import compose_parts
        def response(ids):
            return {'content': '검증된 결과', 'raw': {'mode': 'agent_query_results'},
                    'sourcesForFollowUps': [{'type': 'mantis', 'issue_id': i} for i in ids]}
        result = compose_parts([('explanation', response([52])), ('report', response([41, 66, 52, 99])),
                                ('query', response([66, 52, 41]))], {})
        self.assertEqual([s['issue_id'] for s in result['sourcesForFollowUps']], [66, 52, 41])
        self.assertEqual({s['issue_id'] for s in result['sources']}, {41, 66, 52, 99})

    def test_report_and_grounded_explanation_are_both_kept(self):
        q, plan = report_plan('weekly')
        plan.update(answer_sections=['results', 'explanation'], explanation_sources=['mantis'])
        args = arguments(context_for(plan, q))
        data = rows()
        data[0]['description'] = '차트 설정은 재접속 후 다시 확인해야 합니다.'
        class QuoteModel(FakeModel):
            def grounded(self, payload):
                system = payload['messages'][0]['content']
                if '[검색 파이프라인 결과]\n' in system:
                    shown = json.loads(system.rsplit('[검색 파이프라인 결과]\n', 1)[1])
                    issue = shown['issues'][0]
                    excerpt = next(e for e in issue['answer_evidence']['excerpts'] if '재접속 후' in e['text'])
                    return self.respond(final(json.dumps({'selected_issue_ids': [issue['id']],
                        'sections': [['technical', [excerpt['passage_id']], False]],
                        'answer_items': [['주간 보고서', [excerpt['passage_id']], False, excerpt['text']]], 'investigate': []}, ensure_ascii=False)))
                return super().grounded(payload)
        model = QuoteModel([turn(tool_call('build_issue_report', {**args, 'kind': 'weekly'})),
                           turn(tool_call('get_mantis_issue_detail', {'issue_id': 1})), final('임의 원인은 확정했습니다.')])
        with agent_environment(model=model, sources=FakeSources(lookups={1: data[0]})), request_transfer_scope('company-test'), \
             patch('backend.reports.weekly_customer.load_report_issues', return_value=IssueBatch(data, {'search_complete': True})):
            chat = explain_context(q, constraints=plan['resolved_constraints'])
            chat.intent_plan = plan
            chat.structured_tool_conditions = True
            result = run_agent_if_eligible(chat_request(q), chat)
        self.assertEqual([p['kind'] for p in result['agentReview']['responseParts']], ['report', 'explanation'])
        self.assertNotIn('임의 원인은 확정', result['content'])
        self.assertIn('재접속 후 다시 확인', result['content'])
        verified = build_verification_report(q, result['content'], result['sourcesForFollowUps'], result['retrieval'], result['raw']['mode'])
        self.assertTrue(decide_delivery(verified).allowed, verified['checks'])

    def test_notion_properties_and_body_both_survive_and_invented_numbers_do_not(self):
        from test_agent_remaining_tools import notion_context
        ctx = notion_context('get')
        ctx.question = '노션에서 제목이 "조건검색 서버"인 문서의 상태와 담당자 속성, 본문을 설명해줘.'
        plan = deepcopy(ctx.query_contracts['notion'])
        page = {'id': 'page-1', 'database_id': 'db', 'title': '조건검색 서버', 'status': '진행 중',
                'owner': '', '_candidate_only': True, 'content': '', 'content_incomplete': False}
        current = {**page, '_verified_body': True, 'content': '접속 설정에서 서버 주소를 확인합니다.'}
        stage = {'source': 'notion', 'status': 'ok', 'notion_pages': [page],
                 'meta': {'candidate_count': 1, 'search_complete': True}}
        for read_body, bad_number in ((True, False), (True, True), (False, False)):
            draft = ('접속 설정에서 서버 주소를 확인합니다. [N1]' if not bad_number else
                     '처리 대상은 999건입니다. [N1]\n접속 설정에서 서버 주소를 확인합니다. [N1]')
            model = FakeModel([turn(tool_call('list_notion_pages', {'filters': plan.filters, 'limit': 3})),
                turn(tool_call('get_notion_page_properties', {'page_ref': 'N1', 'fields': ['status']})),
                *([turn(tool_call('get_notion_page_content', {'page_ref': 'N1'}))] if read_body else []), final(draft)])
            with agent_environment(model=model, sources=FakeSources()), request_transfer_scope('company-test'), \
                 patch('backend.tools.notion_queries.search.execute_notion_search', return_value=deepcopy(stage)), \
                 patch('backend.tools.notion_queries.notion_scope', return_value={'db': 'db'}), \
                 patch('backend.tools.notion_queries.read_candidate_page', return_value=deepcopy(page)), \
                 patch('backend.tools.notion_tools.notion_scope', return_value={'db': 'db'}), \
                 patch('backend.tools.notion_tools.read_current_page', return_value=deepcopy(current)):
                chat = explain_context(ctx.question, source='notion', constraints=ctx.conditions['notion'].constraints)
                from backend.intent.unified_validation import validate_plan
                proposal = {**deepcopy(PROPOSAL), 'source': 'notion', 'operation': 'get', 'terms': [],
                    'status': {'mode': 'unspecified', 'values': [], 'evidence': ''}, 'limit': 3,
                    'filters': [{'field': 'title', 'operator': 'equals', 'value': '조건검색 서버', 'evidence': '조건검색 서버'}]}
                chat.intent_plan = {**validate_plan(ctx.question, proposal, {}, {}), 'answer_sections': ['explanation', 'results']}
                chat.structured_tool_conditions = True
                response = run_agent_if_eligible(chat_request(ctx.question), chat)
            self.assertIn('상태: 진행 중', response['content'])
            if read_body:
                self.assertIn('접속 설정에서 서버 주소를 확인합니다', response['content'])
                self.assertLess(response['content'].index('접속 설정'), response['content'].index('상태:'))
            else:
                self.assertNotIn('접속 설정에서 서버 주소를 확인합니다', response['content'])
                self.assertIn('본문 근거를 확보하지 못했습니다', response['content'])
            self.assertNotIn('999', response['content'])
            verified = build_verification_report(ctx.question, response['content'], response['sourcesForFollowUps'],
                                                 response['retrieval'], response['raw']['mode'])
            self.assertTrue(decide_delivery(verified).allowed, verified['checks'])


if __name__ == '__main__':
    unittest.main()
