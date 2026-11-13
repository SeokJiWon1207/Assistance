import backend.llm as chat_llm
import backend.services.chat.planning as chat_planning
import backend.services.chat.prepare as chat_prepare
import backend.services.unified_planning as chat_unified
import copy
import importlib
import importlib.util
import unittest
from unittest.mock import Mock, patch

import requests

from backend.sources.mantis import issue_matches_constraints


def proposal(**updates):
    return {
        'source': 'mantis', 'operation': 'count', 'scope': 'self', 'followup': False,
        'filters': [], 'status': {'mode': 'open', 'values': [], 'evidence': '남아 있는'},
        'clear_fields': [], 'terms': [], 'group_by': None, 'limit': None,
        **updates,
    }


class UnifiedPlannerTests(unittest.TestCase):
    def test_mirae_shorthand_validates_and_selects_only_domestic_mirae_project(self):
        for name in ('미래', '미래에셋', '미래에셋증권'):
            with self.subTest(name=name):
                plan = self.validate(f'9월 {name} mts 이슈 알려줘', proposal(
                    operation='search', scope='team',
                    status={'mode': 'unspecified', 'values': [], 'evidence': ''}, filters=[
                        {'field': 'customer', 'operator': 'equals', 'value': '미래에셋', 'evidence': name},
                        {'field': 'platform', 'operator': 'equals', 'value': 'mts', 'evidence': 'mts'},
                        {'field': 'created_at', 'operator': 'between',
                         'value': ['2026-09-01', '2026-09-30'], 'evidence': '9월'},
                    ]))
                constraints = plan['resolved_constraints']
                self.assertEqual(constraints['customer_key'], '미래에셋')
                self.assertEqual(constraints['platform_key'], 'mts')
                self.assertEqual(constraints['created_at'], {'from': '2026-09-01', 'to': '2026-09-30'})
                from backend.query_filters import matches_source_filters
                clause = next(c for c in constraints['filter_clauses'] if c['field'] == 'customer')
                for project, expected in (
                    ('147MiraeAsset', True), ('125Daewoo', False),
                    ('136INNI_ETRADE', False), ('112KBStar', False),
                ):
                    self.assertEqual(matches_source_filters('mantis', {'customer': project}, [clause]), expected)

    def test_narrowing_similarity_cannot_clear_a_customer_named_as_the_target(self):
        previous=self.validate('#9000과 비슷한 과거 사례의 원인을 비교해줘',proposal(
            operation='report',report_kind='similar',scope='target',
            status={'mode':'unspecified','values':[],'evidence':''},filters=[
                {'field':'issue_id','operator':'equals','value':9000,'evidence':'#9000'}]))
        q='NH MTS 사례로 좁혀서 원인과 조치를 비교해줘'
        wrong=proposal(operation='report',report_kind='similar',scope='inherit',followup=True,
            status={'mode':'inherit','values':[],'evidence':''},clear_fields=['customer'],filters=[
                {'field':'platform','operator':'equals','value':'mts','evidence':'MTS'}])
        with self.assertRaisesRegex(ValueError,'고객사'):
            self.validate(q,wrong,previous)
        correct=copy.deepcopy(wrong);correct['clear_fields']=[]
        correct['filters'].append({'field':'customer','operator':'equals','value':'NH','evidence':'NH'})
        result=self.validate(q,correct,previous)
        self.assertEqual(result['resolved_constraints']['customer_key'],'NH')
        cleared=self.validate('NH 고객사 조건은 풀고 MTS로 좁혀서 비교해줘',wrong,result)
        self.assertIsNone(cleared['resolved_constraints']['customer_key'])

    def planner(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.intent.unified'))
        return importlib.import_module('backend.intent.unified')

    def validate(self, question, plan, previous=None):
        return self.planner().validate_plan(question, plan, {'mantisUsername': '석지원'}, previous or {})

    def test_monthly_count_and_effort_is_not_processing_duration(self):
        q = '2026년 월별 이슈 유형별 등록건수와 전달 공수 추이를 보여줘'
        p = proposal(operation='report', report_kind='effort_sla', scope='all',
                     effort={'type': 'trend'},
                     status={'mode': 'unspecified', 'values': [], 'evidence': ''},
                     filters=[{'field': 'delivered_at', 'operator': 'between',
                               'value': ['2026-01-01', '2026-12-31'], 'evidence': '2026년'}])
        with self.assertRaisesRegex(ValueError, '처리시간'):
            self.validate(q, p)
        p['report_kind'] = 'effort'
        self.assertEqual(self.validate(q, p)['effort']['type'], 'trend')
        p['report_kind'] = 'effort_sla'
        p['effort']['type'] = 'total'
        self.assertEqual(self.validate('2026년 공수와 평균 처리시간을 알려줘', p)['report_kind'], 'effort_sla')

    def test_remaining_all_count_keeps_open_scope(self):
        q = '내 담당에 남아 있는 이슈를 모두 세어줘'
        result = self.validate(q, proposal())
        self.assertEqual(result['resolved_constraints']['statuses'], ['open'])
        self.assertEqual(result['resolved_constraints']['people']['names'], ['석지원'])

    def test_all_quantity_cannot_ground_all_status(self):
        q = '내 담당에 남아 있는 이슈를 모두 세어줘'
        with self.assertRaises(ValueError):
            self.validate(q, proposal(status={'mode': 'all', 'values': [], 'evidence': '모두'}))

    def test_omitted_state_cannot_silently_count_everything(self):
        with self.assertRaises(ValueError):
            self.validate('내 이슈 몇 개 남았어?', proposal(status={'mode': 'unspecified', 'values': [], 'evidence': ''}))

    def test_explicit_all_status_is_allowed(self):
        result = self.validate('내 이슈 전체 상태 몇 건?', proposal(status={'mode': 'all', 'values': [], 'evidence': '전체 상태'}))
        self.assertEqual(result['resolved_constraints']['statuses'], [])

    def test_title_quoted_pending_word_is_not_a_status_filter(self):
        p = proposal(status={'mode': 'unspecified', 'values': [], 'evidence': ''}, filters=[
            {'field': 'title', 'operator': 'contains', 'value': '남아 있는', 'evidence': '"남아 있는"'},
        ])
        c = self.validate('내 이슈 제목에 "남아 있는"이 포함된 것 몇 개?', p)['resolved_constraints']
        self.assertEqual(c['statuses'], [])
        self.assertEqual(c['filter_clauses'][0]['field'], 'title')

    def test_followup_preserves_other_dimensions(self):
        previous = self.validate('내 NH MTS 이슈 몇 개?', proposal(
            status={'mode': 'unspecified', 'values': [], 'evidence': ''}, filters=[
                {'field': 'customer', 'operator': 'equals', 'value': 'NH', 'evidence': 'NH'},
                {'field': 'platform', 'operator': 'equals', 'value': 'mts', 'evidence': 'MTS'},
            ]))
        result = self.validate('그중 개발완료만 몇 개?', proposal(scope='inherit', followup=True,
            status={'mode': 'include', 'values': ['developed'], 'evidence': '개발완료'}), previous)
        c = result['resolved_constraints']
        self.assertEqual(c['statuses'], ['developed'])
        self.assertEqual(c['people']['names'], ['석지원'])
        self.assertTrue(any(f['field'] == 'customer' for f in c['filter_clauses']))
        self.assertTrue(any(f['field'] == 'platform' for f in c['filter_clauses']))
        cleared = self.validate('매체 제한을 해제해줘', proposal(scope='inherit', followup=True,
            status={'mode': 'inherit', 'values': [], 'evidence': ''}, clear_fields=['platform']), result)
        self.assertFalse(any(f['field'] == 'platform' for f in cleared['filter_clauses']))
        self.assertEqual(cleared['resolved_constraints']['statuses'], ['developed'])

    def test_followup_requires_authenticated_previous_plan(self):
        with self.assertRaises(ValueError):
            self.validate('그중 몇 건?', proposal(followup=True, scope='inherit'))

    def test_registered_reporter_excludes_body_sender(self):
        p = proposal(scope='all', status={'mode': 'unspecified', 'values': [], 'evidence': ''}, filters=[
            {'field': 'registered_reporter', 'operator': 'equals', 'value': '석지원', 'evidence': '석지원이 등록자'},
        ])
        c = self.validate('석지원이 등록자인 이슈 몇 개?', p)['resolved_constraints']
        row = {'id': 1, 'summary': '문의', 'reporter': {'real_name': '다른사람'},
               'handler': {'real_name': '다른사람'}, 'status': {'name': 'new'},
               'description': 'From. 석지원', 'custom_fields': []}
        self.assertFalse(issue_matches_constraints(row, c))
        row['reporter']['real_name'] = '석지원'
        self.assertTrue(issue_matches_constraints(row, c))

    def test_legacy_customer_sender_lookup_is_preserved(self):
        row = {'id': 1, 'summary': '문의', 'reporter': {'real_name': '다른사람'},
               'handler': {'real_name': '다른사람'}, 'status': {'name': 'new'},
               'description': 'From. 석지원', 'custom_fields': []}
        self.assertTrue(issue_matches_constraints(row, {'people': {'role': 'reporter', 'names': ['석지원']}}))

    def test_vwap_requester_plan_matches_customer_email_in_both_issues(self):
        question = '이윤동 과장이 VWAP 지표 추가 요청한 이슈 찾아줘'
        request = {'field': 'reporter', 'operator': 'equals', 'value': '이윤동',
                   'evidence': '이윤동 과장이 VWAP 지표 추가 요청한'}
        plan = proposal(operation='search', scope='target', terms=['VWAP'], filters=[request],
                        status={'mode': 'unspecified', 'values': [], 'evidence': ''})
        constraints = self.validate(question, plan)['resolved_constraints']
        self.assertEqual(constraints['people']['role'], 'reporter')
        issues = [
            {'id': issue_id, 'summary': 'VWAP 지표 추가 문의', 'description': description,
             'reporter': {'real_name': '김진성'}, 'handler': {'real_name': handler}, 'custom_fields': []}
            for issue_id, handler, description in (
                (30878, '우동헌', '메일 수신 From. 이윤동\nVWAP 추가 문의'),
                (30879, '이영희', 'From. 이윤동\nVWAP 추가 문의'),
                (30880, '이윤동', 'From. 다른사람\nVWAP 추가 문의'),
            )
        ]
        self.assertEqual([row['id'] for row in issues if issue_matches_constraints(row, constraints)],
                         [30878, 30879])
        request['field'] = 'registered_reporter'
        with self.assertRaisesRegex(ValueError, '인물 역할 불일치.*reporter'):
            self.validate(question, plan)

    def test_registration_and_assignment_ground_the_role_of_the_named_person(self):
        for question, name, correct, wrong in (
            ('김진성이 Mantis에 등록한 이슈 찾아줘', '김진성', 'registered_reporter', 'reporter'),
            ('이윤동이 담당하는 VWAP 지표 추가 요청 이슈 찾아줘', '이윤동', 'owner', 'reporter'),
            ('요청자 이윤동의 VWAP 이슈 찾아줘', '이윤동', 'reporter', 'owner'),
        ):
            with self.subTest(question=question):
                planned = {'field': correct, 'operator': 'equals', 'value': name, 'evidence': question}
                plan = proposal(operation='search', scope='target', filters=[planned],
                                status={'mode': 'unspecified', 'values': [], 'evidence': ''})
                self.assertIn(correct, [clause['field'] for clause in self.validate(question, plan)['filter_clauses']])
                planned['field'] = wrong
                with self.assertRaisesRegex(ValueError, f'인물 역할 불일치.*{correct}'):
                    self.validate(question, plan)

    def test_requester_and_assignee_are_validated_against_their_own_phrases(self):
        question = '이윤동 과장이 요청했고 담당자는 우동헌인 이슈 찾아줘'
        requester = {'field': 'reporter', 'operator': 'equals', 'value': '이윤동', 'evidence': '이윤동 과장이 요청했고'}
        assignee = {'field': 'owner', 'operator': 'equals', 'value': '우동헌', 'evidence': '담당자는 우동헌인'}
        plan = proposal(operation='search', scope='target', filters=[requester, assignee],
                        status={'mode': 'unspecified', 'values': [], 'evidence': ''})
        with patch('backend.intent.entities.load_known_people', return_value=(('우동헌',), ('이윤동',))):
            validated = self.validate(question, plan)
        self.assertEqual({(clause['field'], clause['value']) for clause in validated['filter_clauses']},
                         {('reporter', '이윤동'), ('owner', '우동헌')})
        plan['filters'] = [{**assignee, 'value': ['이윤동', '우동헌'], 'operator': 'in',
                            'evidence': question}]
        with self.assertRaisesRegex(ValueError, '인물 역할 불일치.*reporter'):
            self.validate(question, plan)

    def test_recipient_wording_does_not_imply_requester(self):
        question = '이윤동 과장한테 전달한 이슈 찾아줘'
        plan = proposal(operation='search', scope='target',
                        status={'mode': 'unspecified', 'values': [], 'evidence': ''},
                        filters=[{'field': 'owner', 'operator': 'equals', 'value': '이윤동',
                                  'evidence': '이윤동 과장한테'}])
        self.assertEqual(self.validate(question, plan)['resolved_constraints']['people']['role'], 'handler')

    def test_same_person_can_be_both_requester_and_assignee(self):
        question = '이윤동 과장이 요청하고 직접 담당한 이슈 찾아줘'
        plan = proposal(operation='search', scope='target',
                        status={'mode': 'unspecified', 'values': [], 'evidence': ''},
                        filters=[{'field': field, 'operator': 'equals', 'value': '이윤동', 'evidence': question}
                                 for field in ('reporter', 'owner')])
        self.assertEqual({clause['field'] for clause in self.validate(question, plan)['filter_clauses']},
                         {'reporter', 'owner'})

    def test_model_reinterprets_vwap_request_after_role_rejection(self):
        import json
        module = self.planner()
        question = '이윤동 과장이 VWAP 지표 추가 요청한 이슈 찾아줘'
        plans = [
            proposal(operation='search', scope='target', terms=['VWAP'],
                     status={'mode': 'unspecified', 'values': [], 'evidence': ''},
                     filters=[{'field': field, 'operator': 'equals', 'value': '이윤동',
                               'evidence': '이윤동 과장이'}])
            for field in ('owner', 'reporter')
        ]
        with patch.object(module, '_propose', side_effect=plans) as propose:
            result = module.plan_question(question, {'id': 'test'}, 'key', {}, {}, model_first=True)
        self.assertEqual(result['resolved_constraints']['people']['role'], 'reporter')
        self.assertEqual(propose.call_count, 2)
        messages = propose.call_args.args[2]['messages']
        payload = json.loads(messages[1]['content'])
        self.assertIn('이윤동', payload['mentioned_people'])
        self.assertIn({'name': '이윤동', 'field': 'reporter', 'evidence': '이윤동 과장이 VWAP 지표 추가 요청한'},
                      payload['person_role_hints'])
        self.assertIn('인물 역할 불일치', messages[-1]['content'])

    def test_agent_role_hints_require_a_named_person(self):
        import json
        from backend.services.agent.planning import PlanningSession, planning_messages
        questions = (
            ('이윤동 과장님이 VWAP 지표 추가 요청한 이슈 찾아줘', ['reporter']),
            ('요청자 이름에 김진성이 들어가는 이슈 찾아줘', []),
        )
        for question, fields in questions:
            with self.subTest(question=question):
                messages = planning_messages(PlanningSession(question, {}, {}))
                payload = json.loads(messages[1]['content'])
                self.assertEqual([hint['field'] for hint in payload['person_role_hints']], fields)

    def test_contradictory_filters_and_ungrounded_names_rejected(self):
        p = proposal(filters=[
            {'field': 'title', 'operator': 'contains', 'value': '점검', 'evidence': '점검'},
            {'field': 'title', 'operator': 'not_contains', 'value': '점검', 'evidence': '점검'},
        ])
        with self.assertRaises(ValueError):
            self.validate('내 남아 있는 점검 이슈 몇 개?', p)
        p = proposal(scope='target', filters=[{'field': 'owner', 'operator': 'equals', 'value': '다른사람', 'evidence': '내'}])
        with self.assertRaises(ValueError):
            self.validate('내 남아 있는 이슈 몇 개?', p)

    def test_model_timeout_never_falls_back_to_unfiltered_count(self):
        module = self.planner()
        with patch.object(module, 'post_model_chat', side_effect=requests.Timeout):
            result = module.plan_question('내 이슈 몇 개?', {'id': 'test'}, 'key', {}, {})
        self.assertEqual(result['operation'], 'clarify')
        self.assertNotIn('resolved_constraints', result)

    def test_one_model_interprets_whole_question(self):
        module = self.planner()
        import json
        response = Mock(json=lambda: {'choices': [{'message': {'content': json.dumps(proposal())}}]})
        with patch.object(module, 'post_model_chat', return_value=response) as call:
            result = module.plan_question('내 남아 있는 이슈 모두 몇 개?', {'id': 'test'}, 'key', {'mantisUsername': '석지원'}, {})
        self.assertEqual(result['operation'], 'count')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result['resolved_constraints']['statuses'], ['open'])
        self.assertEqual(copy.deepcopy(result), result)


class UnifiedIntegrationTests(unittest.TestCase):
    def test_customer_spike_question_returns_baseline_comparison(self):
        import json
        from pathlib import Path

        from backend.schemas import ChatRequest
        from backend.services import chat_service as service

        fixture = Path(__file__).parent / 'fixtures' / 'routing_cases.jsonl'
        cases = [case for case in map(json.loads, fixture.read_text().splitlines())
                 if case.get('spike_planner') == 'report']
        self.assertTrue(cases)
        spike_plan = proposal(operation='report', report_kind='spike', scope='default', group_by='customer',
                              status={'mode': 'unspecified', 'values': [], 'evidence': ''})
        snapshot = {
            'available': True, 'scanned_count': 37, 'search_complete': True,
            'group_counts': {'유진증권\x1f2026-10': 3, '유진증권\x1f2026-11': 12,
                             'KB증권\x1f2026-10': 11, 'KB증권\x1f2026-11': 11},
        }
        for case in cases:
            question = case['q']
            request = ChatRequest(modelId='test', messages=[{'role': 'user', 'content': question}])
            with (self.subTest(question=question),
                  patch.dict('os.environ', {'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
                  patch.object(chat_prepare, 'get_private_model', return_value={'id': 'test', 'status': 'active'}),
                  patch.object(chat_llm, 'get_model_api_key', return_value='key'),
                  patch('backend.intent.unified._propose', return_value=spike_plan) as model,
                  patch('backend.reports.aggregate.scan_mantis_documents', return_value=snapshot),
                  patch.object(chat_unified, 'build_mantis_count_report', return_value={
                      'content': '총 11,885건입니다.', 'sources': [], 'raw': {'mode': 'deterministic_mantis_count'},
                  }),
                  patch.object(service, 'persist_chat_response', side_effect=lambda request, text, result, **kw: result)):
                result = service.chat_service.build_response(request, user={'uid': 'u'})
                self.assertEqual(result['raw']['mode'], 'deterministic_mantis_spike')
                plan = result['retrieval']['query_plan']
                self.assertEqual((plan['operation'], plan['report_kind'], plan['group_by']), ('report', 'spike', 'customer'))
                self.assertEqual(plan['resolved_constraints']['mantis_team_scope'], 'team')
                self.assertIn('유진투자증권', result['content'])
                self.assertIn('직전 평균 3.0건, 4.0배', result['content'])
                self.assertNotIn('KB증권', result['content'])
                self.assertEqual(result['sources'], [])
                model.assert_called_once()
                self.assertEqual(plan['planner_model_id'], 'test')

    def test_spike_text_in_issue_title_uses_search_filter(self):
        import json
        from pathlib import Path

        from backend.domain.team import TEAM_MEMBERS
        from backend.intent.unified import plan_question

        fixture = Path(__file__).parent / 'fixtures' / 'routing_cases.jsonl'
        case = next(case for case in map(json.loads, fixture.read_text().splitlines())
                    if case.get('spike_planner') == 'title_search')
        search_plan = proposal(operation='search', scope='default',
                               status={'mode': 'unspecified', 'values': [], 'evidence': ''},
                               filters=[{'field': 'title', 'operator': 'contains', 'value': '급증', 'evidence': '급증'}])
        with patch('backend.intent.unified._propose', return_value=search_plan) as model:
            plan = plan_question(case['q'], {'id': 'test'}, 'key', {}, {})
        model.assert_called_once()
        self.assertEqual(plan['operation'], 'search')
        self.assertIsNone(plan['report_kind'])
        self.assertEqual(plan['filter_clauses'], [{'field': 'title', 'operator': 'contains', 'value': '급증'}])
        issue = {'id': 12345, 'handler': {'real_name': TEAM_MEMBERS[0]}, 'description': '요청이 급증함'}
        self.assertTrue(issue_matches_constraints({**issue, 'summary': '접속 오류 급증'}, plan['resolved_constraints']))
        self.assertFalse(issue_matches_constraints({**issue, 'summary': '접속 오류'}, plan['resolved_constraints']))

    def test_short_handler_lookup_builds_exact_filter_without_planner_model(self):
        from backend.schemas import ChatRequest

        question = '담당자가 cs이슈인건?'
        request = ChatRequest(modelId='test', messages=[{'role': 'user', 'content': question}])
        for enabled, policy in [('false', None), ('true', None), ('true', object())]:
            with (self.subTest(enabled=enabled, agent=bool(policy)),
                  patch.dict('os.environ', {'UNIFIED_QUERY_PLANNER_ENABLED': enabled}),
                  patch.object(chat_prepare, 'get_private_model', return_value={'id': 'test', 'status': 'active'}),
                  patch.object(chat_llm, 'get_model_api_key', return_value=None),
                  patch('backend.services.agent.policy.active_agent_policy', return_value=policy)):
                context = chat_prepare.prepare_chat_pipeline(request, question, {'uid': 'u'})
                self.assertEqual(context.intent_plan['source'], 'mantis')
                self.assertEqual(context.intent_plan['operation'], 'search')
                self.assertEqual(context.intent_plan['terms'], [])
                self.assertEqual(context.intent_plan['sort'], {'field': 'created_at', 'direction': 'desc'})
                self.assertFalse(context.agent_planning_context)
                for handler, expected in [('CS이슈', True), ('csissue', False), ('다른담당자', False)]:
                    issue = {'id': 12345, 'handler': {'real_name': handler}, 'summary': 'cs이슈 관련 문의'}
                    self.assertEqual(issue_matches_constraints(issue, context.query_constraints), expected)

    def test_default_is_unified_and_explicit_rollback_exists(self):
        from backend.intent.unified import enabled
        with patch.dict('os.environ', {}, clear=True):
            self.assertTrue(enabled())
        with patch.dict('os.environ', {'UNIFIED_QUERY_PLANNER_ENABLED': 'false'}):
            self.assertFalse(enabled())

    def test_full_count_path_does_not_reinterpret_with_legacy_planners(self):
        import json
        import backend.services.chat_service as service
        from backend.schemas import ChatRequest, ChatMessage
        response = Mock(json=lambda: {'choices': [{'message': {'content': json.dumps(proposal())}}]})
        request = ChatRequest(modelId='test', messages=[ChatMessage(role='user', content='내 남아 있는 이슈 모두 몇 개?')])
        seen = []
        def count(text, plan):
            from backend.intent.constraints import extract_query_constraints, merge_query_plan_constraints
            c = merge_query_plan_constraints(extract_query_constraints(text), plan)
            seen.append(c)
            return {'content': '총 2건', 'sources': [], 'retrieval': {'constraints': c, 'stages': []}}
        with (patch.dict('os.environ', {'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
              patch.object(chat_prepare, 'get_private_model', return_value={'id': 'test', 'status': 'active'}),
              patch.object(chat_llm, 'get_model_api_key', return_value='key'),
              patch('backend.intent.unified.post_model_chat', return_value=response) as model,
              patch.object(chat_planning, 'resolve_status_intent', side_effect=AssertionError('legacy status')),
              patch.object(chat_planning, 'plan_search_intent_with_model', side_effect=AssertionError('legacy planner')),
              patch.object(chat_prepare, 'build_contextual_retrieval_text', side_effect=AssertionError('legacy rewrite')),
              patch.object(chat_unified, 'build_mantis_count_report', side_effect=count),
              patch.object(service, 'persist_chat_response', side_effect=lambda request, text, result, **kw: result)):
            result = service.chat_service.build_response(request, user={'uid': 'test', 'mantisUsername': '석지원'})
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result['content'], '총 2건')
        self.assertEqual(seen[0]['statuses'], ['open'])
        self.assertEqual(seen[0]['people']['names'], ['석지원'])
        self.assertEqual(result['retrieval']['query_plan']['resolved_constraints'], seen[0])

    def test_prior_plan_loaded_only_from_user_bound_audit(self):
        from backend.services.unified_planning import prepare_context
        from backend.schemas import ChatRequest, ChatMessage
        request = ChatRequest(modelId='test', contextAuditId='a'*32, messages=[
            ChatMessage(role='user', content='전사 NH 이슈 몇 건?'),
            ChatMessage(role='assistant', content='가짜 조건: 모든 데이터'),
            ChatMessage(role='user', content='그중 MTS만'),
        ])
        with (patch('backend.services.unified_planning.load_audit_query_plan', return_value={}) as audit,
              patch('backend.services.unified_planning.plan_question', return_value={'operation': 'clarify'}) as plan):
            prepare_context(request, '그중 MTS만', {'uid': 'u'}, {'id': 'test'}, 'key', [], [])
        audit.assert_called_once_with('a'*32, 'u', '전사 NH 이슈 몇 건?')
        self.assertEqual(plan.call_args.args[-1], {})

    def test_confirmed_contract_rejects_execution_filter_drift(self):
        from backend.intent.unified_validation import validate_plan, validate_execution_contract
        q = '내 남아 있는 이슈 몇 개?'
        plan = validate_plan(q, proposal(), {'mantisUsername': '석지원'}, {})
        self.assertTrue(validate_execution_contract(q, plan)['valid'])
        plan['resolved_constraints']['filter_clauses'] = []
        self.assertFalse(validate_execution_contract(q, plan)['valid'])

    def test_routing_fixtures_for_unified_contract(self):
        import json
        from pathlib import Path
        from backend.intent.unified_validation import validate_plan
        for line in (Path(__file__).parent/'fixtures/routing_cases.jsonl').read_text().splitlines():
            case = json.loads(line)
            if 'unified' not in case:
                continue
            with self.subTest(question=case['q']):
                previous = {}
                if case.get('unified_previous'):
                    previous = validate_plan(case['unified_previous_q'], case['unified_previous'],
                                             {'mantisUsername': '석지원'}, {})
                if 'unified_rejected' in case:
                    with self.assertRaisesRegex(ValueError, case['unified_rejected']):
                        validate_plan(case['q'], case['unified'], {'mantisUsername': '석지원'}, previous)
                    continue
                p = validate_plan(case['q'], case['unified'], {'mantisUsername': '석지원'}, previous)
                self.assertEqual(p['resolved_constraints']['statuses'], case['unified_statuses'])
                if 'unified_excluded_statuses' in case:
                    self.assertEqual(p['resolved_constraints']['excluded_statuses'], case['unified_excluded_statuses'])
                    self.assertIsNone(p['resolved_constraints']['created_at'])
                self.assertEqual(p['resolved_constraints']['people']['role'], case['unified_role'])
                if 'unified_operation' in case:
                    self.assertEqual(p['operation'], case['unified_operation'])
                if 'unified_scope' in case:
                    self.assertEqual(p['resolved_constraints']['mantis_team_scope'], case['unified_scope'])
                if 'unified_customer' in case:
                    self.assertEqual(p['resolved_constraints']['customer_key'], case['unified_customer'])
                if 'unified_terms' in case:
                    self.assertEqual(p['terms'], case['unified_terms'])


class UnifiedSchemaNormalizationTests(unittest.TestCase):
    planner = UnifiedPlannerTests.planner
    validate = UnifiedPlannerTests.validate
    def test_explicit_status_clause_is_normalized_without_another_model_call(self):
        p = proposal(scope='target', status={'mode': 'unspecified', 'values': [], 'evidence': ''}, filters=[
            {'field': 'owner', 'operator': 'equals', 'value': '석지원', 'evidence': '석지원 담당'},
            {'field': 'status', 'operator': 'equals', 'value': 'confirmed', 'evidence': '확인된'},
        ])
        self.assertEqual(self.validate('석지원 담당 확인된 이슈 몇 건?', p)['resolved_constraints']['statuses'], ['confirmed'])

    def test_discontinuous_evidence_requires_original_order(self):
        p = proposal(status={'mode': 'unspecified', 'values': [], 'evidence': ''}, filters=[
            {'field': 'created_at', 'operator': 'between', 'value': ['2026-06-01', '2026-06-30'], 'evidence': '2026년 6월 등록'},
        ])
        self.assertEqual(self.validate('내 2026년 6월 유안타 등록 이슈 몇 건?', p)['resolved_constraints']['created_at']['from'], '2026-06-01')
        p['filters'][0]['evidence'] = '2026년 7월 등록'
        with self.assertRaises(ValueError):
            self.validate('내 2026년 6월 유안타 등록 이슈 몇 건?', p)

    def test_pending_explanation_is_not_an_open_only_search(self):
        p = proposal(operation='explain', scope='target', status={'mode': 'unspecified', 'values': [], 'evidence': ''},
                     filters=[{'field': 'issue_id', 'operator': 'equals', 'value': '31088', 'evidence': '#31088'}])
        result = self.validate('#31088 이슈에서 남아 있는 작업을 설명해줘', p)
        self.assertEqual(result['resolved_constraints']['statuses'], [])

    def test_repeated_authenticated_filter_is_not_an_ungrounded_new_filter(self):
        previous = self.validate('내 남아 있는 이슈 몇 건?', proposal())
        result = self.validate('상태 제한을 풀어줘', proposal(scope='inherit', followup=True,
            status={'mode': 'all', 'values': [], 'evidence': '상태 제한을 풀어줘'},
            filters=[{'field': 'owner', 'operator': 'equals', 'value': '석지원', 'evidence': '이전 조건 유지'}]), previous)
        self.assertEqual(result['resolved_constraints']['people']['names'], ['석지원'])
        self.assertEqual(result['resolved_constraints']['statuses'], [])
        bad = proposal(scope='inherit', followup=True, status={'mode': 'inherit', 'values': [], 'evidence': ''},
            filters=[{'field': 'owner', 'operator': 'equals', 'value': '다른사람', 'evidence': '이전 조건 유지'}])
        with self.assertRaises(ValueError):
            self.validate('같은 조건으로 다시 세어줘', bad, previous)

    def test_count_search_terms_are_executed_not_just_saved_in_plan(self):
        p = proposal(status={'mode': 'unspecified', 'values': [], 'evidence': ''}, terms=['차트'])
        result = self.validate('내 차트 이슈 몇 개?', p)
        self.assertIn({'field': 'keyword', 'operator': 'contains', 'value': '차트'}, result['filter_clauses'])

    def test_notion_self_uses_display_name_without_mantis_team_scope(self):
        from backend.intent.unified_validation import validate_plan
        p = proposal(source='notion', operation='search', status={'mode': 'unspecified', 'values': [], 'evidence': ''})
        c = validate_plan('내 노션 문서 찾아줘', p, {'displayName': '노션이름', 'mantisUsername': '다른이름'}, {})['resolved_constraints']
        self.assertEqual(c['people']['names'], ['노션이름'])
        self.assertNotIn('mantis_team_members', c)

    def test_malformed_status_shape_is_a_clarification_not_server_error(self):
        import json
        from backend.intent.unified import plan_question
        p = proposal(status='open', filters=[{'field': 'status', 'operator': 'equals', 'value': 'new', 'evidence': '신규'}])
        response = Mock(json=lambda: {'choices': [{'message': {'content': json.dumps(p)}}]})
        with patch('backend.intent.unified.post_model_chat', return_value=response):
            result = plan_question('내 신규 이슈 몇 개?', {'id': 'test'}, 'key', {}, {})
        self.assertEqual(result['operation'], 'clarify')

    def test_unified_count_does_not_reparse_quantity_as_display_limit(self):
        from backend.reports.aggregate import build_mantis_count_report
        p = self.validate('내 전체 상태 이슈 수는?', proposal(status={'mode':'all','values':[],'evidence':'전체 상태'}))
        with (patch('backend.reports.aggregate.count_issue_display_limit', side_effect=AssertionError('quantity reparsed')),
              patch('backend.reports.aggregate.scan_mantis_documents', return_value={'available':False,'reason':'fixture'})):
            result = build_mantis_count_report('내 전체 상태 이슈 수는?',p)
        self.assertIsNotNone(result)

    def test_ellipsis_in_status_evidence_only_allows_original_ordered_fragments(self):
        p = proposal(status={'mode':'exclude','values':['closed'],'evidence':'종료만 제외...해결 포함'})
        self.assertEqual(self.validate('내 이슈 종료만 제외하고 해결 포함해서 몇 개?',p)['resolved_constraints']['excluded_statuses'],['closed'])
        p['status']['evidence']='신규만 제외...해결 포함'
        with self.assertRaises(ValueError):
            self.validate('내 이슈 종료만 제외하고 해결 포함해서 몇 개?',p)

    def test_explicit_status_classification_is_not_quantity_all(self):
        p=proposal(status={'mode':'all','values':[],'evidence':'상태 구분 없이'})
        self.assertEqual(self.validate('내 이슈 상태 구분 없이 몇 건?',p)['resolved_constraints']['statuses'],[])
        p['status']['evidence']='모두'
        with self.assertRaises(ValueError):
            self.validate('내 남아 있는 이슈 모두 몇 건?',p)

    def test_equivalent_singleton_in_filter_is_not_a_new_followup_condition(self):
        previous=self.validate('우동헌 담당 전체 상태 이슈 몇 건?',proposal(scope='target',
            status={'mode':'all','values':[],'evidence':'전체 상태'},filters=[
                {'field':'owner','operator':'in','value':['우동헌'],'evidence':'우동헌 담당'}]))
        previous['audit_id']='c'*32
        result=self.validate('같은 담당자로 다시 세어줘',proposal(scope='inherit',followup=True,
            status={'mode':'inherit','values':[],'evidence':''},filters=[
                {'field':'owner','operator':'equals','value':'우동헌','evidence':'같은 담당자'}]),previous)
        self.assertEqual(result['filter_clauses'],previous['filter_clauses'])
        with self.assertRaises(ValueError):
            self.validate('같은 담당자로 다시 세어줘',proposal(scope='inherit',followup=True,
                status={'mode':'inherit','values':[],'evidence':''},filters=[
                    {'field':'owner','operator':'equals','value':'김진성','evidence':'같은 담당자'}]),previous)


class RelativeCreatedAtValidationTests(unittest.TestCase):
    """상대 등록일은 모델이 계산한 범위가 아니라 서버 달력 계산과 대조한다."""

    TODAY = __import__('datetime').date(2026, 10, 21)

    def setUp(self):
        from datetime import date

        today = self.TODAY

        class FixedDate(date):
            @classmethod
            def today(cls):
                return today

        patcher = patch('backend.intent.validation_rules.date', FixedDate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self, value, evidence, operator='between'):
        return proposal(scope='team', status={'mode': 'unspecified', 'values': [], 'evidence': ''}, filters=[
            {'field': 'created_at', 'operator': operator, 'value': value, 'evidence': evidence}])

    def validate(self, question, plan):
        from backend.intent.unified_validation import validate_plan
        return validate_plan(question, plan, {'mantisUsername': '석지원'}, {})

    def test_model_computed_relative_range_must_match_server_calendar(self):
        for question, evidence, wrong, expected in (
            ('최근 3개월 등록된 이슈 몇 건이야?', '최근 3개월', ['2025-01-01', '2025-03-31'], '2026-08-01~2026-10-21'),
            ('지난주 등록된 이슈 몇 건이야?', '지난주', ['2026-10-13', '2026-10-19'], '2026-10-12~2026-10-18'),
            ('최근 7일 등록된 이슈 몇 건이야?', '최근 7일', ['2026-10-14', '2026-10-21'], '2026-10-15~2026-10-21'),
            ('어제 올라온 이슈 몇 건이야?', '어제 올라온', ['2026-10-21', '2026-10-21'], '2026-10-20~2026-10-20'),
            ('지난달 등록된 이슈 몇 건이야?', '지난달', ['2026-08-01', '2026-08-31'], '2026-09-01~2026-09-30'),
        ):
            with self.subTest(question=question):
                # 재시도에서 모델이 바로잡을 수 있도록 거절 사유에 서버가 계산한 범위를 담는다.
                with self.assertRaisesRegex(ValueError, expected):
                    self.validate(question, self.plan(wrong, evidence))

    def test_equivalent_forms_of_the_server_range_are_accepted(self):
        for question, evidence, operator, value in (
            ('최근 3개월 등록된 이슈 몇 건이야?', '최근 3개월', 'between', ['2026-08-01', '2026-10-21']),
            ('최근 3개월 등록된 이슈 몇 건이야?', '최근 3개월 등록된', 'gte', '2026-08-01'),
            # 등록일에는 미래가 없으므로 말일까지와 오늘까지는 같은 조회다.
            ('이번 달 등록된 이슈 몇 건이야?', '이번 달', 'between', ['2026-10-01', '2026-10-31']),
            ('이번 달 등록된 이슈 몇 건이야?', '이번 달', 'between', ['2026-10-01', '2026-10-21']),
            ('이번 주에 등록된 이슈 몇 건이야?', '이번 주에 등록된', 'between', ['2026-10-19', '2026-10-25']),
            ('오늘 등록된 이슈 몇 건이야?', '오늘', 'equals', '2026-10-21'),
        ):
            with self.subTest(evidence=evidence, value=value):
                plan = self.validate(question, self.plan(value, evidence, operator))
                self.assertTrue(plan['resolved_constraints']['unified_plan_validated'])

    def test_qualified_or_absolute_dates_are_not_forced_to_the_whole_period(self):
        # 세부 조건이 붙은 근거를 전체 기간으로 고치면 맞는 계획을 틀린 범위로 유도한다.
        for question, evidence, value in (
            ('지난달 15일 이후 등록된 이슈 몇 건이야?', '지난달 15일 이후', ['2026-09-15', '2026-09-30']),
            ('지난주 월요일에 등록된 이슈 몇 건이야?', '지난주 월요일', ['2026-10-12', '2026-10-12']),
            ('2026년 9월 등록된 이슈 몇 건이야?', '2026년 9월', ['2026-09-01', '2026-09-30']),
        ):
            with self.subTest(evidence=evidence):
                plan = self.validate(question, self.plan(value, evidence))
                self.assertEqual(plan['resolved_constraints']['created_at'], {'from': value[0], 'to': value[1]})

    def test_planner_retry_executes_the_corrected_relative_range(self):
        import json
        module = importlib.import_module('backend.intent.unified')
        wrong = self.plan(['2025-01-01', '2025-03-31'], '최근 3개월')
        correct = self.plan(['2026-08-01', '2026-10-21'], '최근 3개월')
        responses = [Mock(json=lambda body=body: {'choices': [{'message': {'content': json.dumps(body)}}]})
                     for body in (wrong, correct)]
        with patch.object(module, 'post_model_chat', side_effect=responses) as call:
            result = module.plan_question('최근 3개월 등록된 이슈 몇 건이야?', {'id': 'test'}, 'key',
                                          {'mantisUsername': '석지원'}, {})
        self.assertEqual(call.call_count, 2)
        self.assertIn('2026-08-01~2026-10-21', call.call_args_list[1].args[2]['messages'][-1]['content'])
        self.assertEqual(result['resolved_constraints']['created_at'], {'from': '2026-08-01', 'to': '2026-10-21'})
