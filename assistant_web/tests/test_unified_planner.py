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
              patch.object(service, 'get_private_model', return_value={'id': 'test', 'status': 'active'}),
              patch.object(service, 'get_model_api_key', return_value='key'),
              patch('backend.intent.unified.post_model_chat', return_value=response) as model,
              patch.object(service, 'resolve_status_intent', side_effect=AssertionError('legacy status')),
              patch.object(service, 'plan_search_intent_with_model', side_effect=AssertionError('legacy planner')),
              patch.object(service, 'build_contextual_retrieval_text', side_effect=AssertionError('legacy rewrite')),
              patch.object(service, 'build_mantis_count_report', side_effect=count),
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
                p = validate_plan(case['q'], case['unified'], {'mantisUsername': '석지원'}, previous)
                self.assertEqual(p['resolved_constraints']['statuses'], case['unified_statuses'])
                if 'unified_excluded_statuses' in case:
                    self.assertEqual(p['resolved_constraints']['excluded_statuses'], case['unified_excluded_statuses'])
                    self.assertIsNone(p['resolved_constraints']['created_at'])
                self.assertEqual(p['resolved_constraints']['people']['role'], case['unified_role'])


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
