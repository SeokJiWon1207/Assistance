"""명시 고객사 누락과 Notion 부분 일치가 다른 회사 문서를 반환하지 않아야 한다."""
from backend.notion import read_api
from backend.notion import transport
from backend.notion import schema as notion_schema
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch
import requests

from backend.execution import ExecutionBudget, RequestStopped, execution_scope

from backend.intent.constraints import extract_query_constraints
from backend.intent import unified
from backend.intent.unified_validation import validate_plan, validate_execution_contract
from backend.notion import search


def proposal(**updates):
    return {'source': 'notion', 'operation': 'search', 'scope': 'default', 'followup': False,
            'filters': [], 'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
            'clear_fields': [], 'terms': [], 'group_by': None, 'limit': None, **updates}


def customer(value='한투', operator='equals', evidence='한투'):
    return {'field': 'customer', 'operator': operator, 'value': value, 'evidence': evidence}


class CustomerScopeContractTests(unittest.TestCase):
    def fallback_models(self):
        primary = {'id': 'luna', 'fallbackModelIds': ['gemini', 'other']}
        fallback = {'id': 'gemini', 'provider': 'gemini'}
        self.enterContext(patch.object(unified.llm, 'get_model_registry', return_value=[]))
        self.enterContext(patch.object(unified.llm, 'get_fallback_models', return_value=[fallback, {'id':'other'}]))
        self.enterContext(patch.object(unified.llm, 'get_model_api_key', return_value='test-key'))
        return primary

    def test_gemini_transport_failure_does_not_expand_reinterpretation_budget(self):
        primary = self.fallback_models()
        calls = []
        def propose(model, *_args):
            calls.append(model['id'])
            if model['id'] != 'luna':
                raise requests.Timeout()
            return proposal()
        with patch.object(unified, '_propose', side_effect=propose):
            plan = unified.plan_question('한투 문서 찾아줘', primary, 'key', {}, {})
        self.assertEqual(plan['operation'], 'clarify')
        self.assertEqual(calls, ['luna', 'luna', 'gemini'])
        self.assertNotIn('resolved_constraints', plan)

    def test_deadline_stops_before_customer_reinterpretation(self):
        primary = self.fallback_models()
        budget = ExecutionBudget.create()
        calls = []
        def propose(model, *_args):
            calls.append(model['id'])
            if len(calls) == 2:
                budget.deadline = 0
            return proposal()
        with execution_scope(budget), patch.object(unified, '_propose', side_effect=propose), self.assertRaises(RequestStopped):
            unified.plan_question('한투 문서 찾아줘', primary, 'key', {}, {})
        self.assertEqual(calls, ['luna', 'luna'])

    def test_unsupported_operation_does_not_trigger_customer_reinterpretation(self):
        primary = self.fallback_models()
        with patch.object(unified, '_propose', return_value=proposal(source='mantis', operation='write')) as propose:
            plan = unified.plan_question('한투 문서 찾아줘', primary, 'key', {}, {})
        self.assertEqual(plan['operation'], 'clarify')
        self.assertEqual(propose.call_count, 2)

    def test_current_owner_lookup_uses_issue_properties_after_an_explanation(self):
        previous = validate_plan('맨티스 #31708 이슈의 문제 현상을 설명해줘',
            proposal(source='mantis', operation='explain', scope='target', filters=[
                {'field':'issue_id','operator':'equals','value':'31708','evidence':'#31708'}]), {}, {})
        inherited = proposal(source='mantis', operation='explain', followup=True, scope='inherit',
                             status={'mode':'inherit','values':[],'evidence':''})
        for question in ['그 이슈 담당자는 누구야?', '0031708 그 이슈 담당자는 누구야?']:
            with self.subTest(question=question):
                plan = validate_plan(question, inherited, {}, previous)
                self.assertEqual(plan['operation'], 'get')
                self.assertEqual(plan['resolved_constraints']['issue_ids'], [31708])
                self.assertTrue(validate_execution_contract(plan['grounding_text'], plan)['valid'])
        for question in ['그 이슈 담당자 변경 이력을 설명해줘', '그 이슈 담당자는 누구였어?']:
            with self.subTest(question=question):
                self.assertEqual(validate_plan(question, inherited, {}, previous)['operation'], 'explain')

    def test_document_quantity_is_not_hana_customer(self):
        for question in ['노션 문서 하나 찾아줘', '노션 최근 문서 중 하나 보여줘', '노션 문서 하나만 찾아줘']:
            with self.subTest(question=question):
                plan = validate_plan(question, proposal(), {}, {})
                self.assertIsNone(plan['resolved_constraints']['customer_key'])
        previous = validate_plan('노션 한투 문서 찾아줘', proposal(filters=[customer()]), {}, {})
        plan = validate_plan('그중 하나 보여줘', proposal(followup=True, scope='inherit',
            status={'mode':'inherit','values':[],'evidence':''}), {}, previous)
        self.assertEqual(plan['resolved_constraints']['customer_key'], '한투')
        hana = validate_plan('노션에서 하나 관련 문서 찾아줘', proposal(filters=[customer('하나', evidence='하나')]), {}, {})
        self.assertEqual(hana['resolved_constraints']['customer_key'], '하나')

    def test_missing_customer_or_customer_only_in_terms_is_rejected(self):
        for q, terms in [('노션에서 한투 관련 최근 문서 찾아줘', ['한투']),
                         ('한투에서 조건검색 서버 관련 문서 찾아줘', ['조건검색', '서버'])]:
            with self.subTest(q=q), self.assertRaisesRegex(ValueError, '고객사'):
                validate_plan(q, proposal(terms=terms), {}, {})

    def test_wrong_customer_cannot_be_grounded_in_substring_of_other_company(self):
        with self.assertRaisesRegex(ValueError, '고객사'):
            validate_plan('신한투자증권 문서 찾아줘', proposal(filters=[customer(evidence='신한투자증권')]), {}, {})

    def test_company_terms_are_consumed_but_topics_remain(self):
        plan = validate_plan('한투에서 조건검색 서버 문서 찾아줘',
                             proposal(filters=[customer()], terms=['한투', '조건검색', '서버']), {}, {})
        self.assertEqual(plan['terms'], ['조건검색', '서버'])
        self.assertTrue(validate_execution_contract(plan['grounding_text'], plan)['valid'])

    def test_plain_related_document_topics_do_not_become_body_only_filters(self):
        keywords = [{'field': 'keyword', 'operator': 'contains', 'value': value, 'evidence': value}
                    for value in ['조건검색', '서버']]
        plan = validate_plan('한투에서 조건검색 서버 관련 문서 찾아줘',
                             proposal(filters=[customer(), *keywords]), {}, {})
        self.assertEqual(plan['terms'], ['조건검색', '서버'])
        self.assertEqual(plan['filter_clauses'], [{'field':'customer', 'operator':'equals', 'value':'한투'}])
        self.assertTrue(validate_execution_contract(plan['grounding_text'], plan)['valid'])
        for qualifier in ['본문에', '내용에', '제목에', '문자열', '키워드']:
            with self.subTest(qualifier=qualifier):
                question = f'한투에서 {qualifier} 조건검색 서버 관련 문서 찾아줘'
                explicit = validate_plan(question, proposal(filters=[customer(), *keywords]), {}, {})
                self.assertEqual(len(explicit['filter_clauses']), 3)
                self.assertEqual(explicit['terms'], [])

    def test_literals_and_general_topics_are_not_company_filters(self):
        cases = [("본문에 '한투'가 포함된 문서", [{'field': 'keyword', 'operator': 'contains', 'value': '한투', 'evidence': "'한투'"}], []),
                 ("노션에서 '[한투] 서버 점검' 문서 찾아줘", [{'field': 'title', 'operator': 'equals', 'value': '[한투] 서버 점검', 'evidence': "'[한투] 서버 점검'"}], []),
                 ('노션에서 인증 관련 문서 찾아줘', [], ['인증'])]
        for q, filters, terms in cases:
            with self.subTest(q=q):
                plan = validate_plan(q, proposal(filters=filters, terms=terms), {}, {})
                self.assertIsNone(plan['resolved_constraints']['customer_key'])
                self.assertEqual(plan['terms'], terms)

    def test_multiple_inclusions_and_exclusions_are_checked_as_sets(self):
        for q, values, operator in [('한투랑 신한 문서 찾아줘', ['한투', '신한'], 'in'),
                                    ('한투와 신한 빼고 문서 찾아줘', ['한투', '신한'], 'not_in')]:
            with self.subTest(q=q):
                with self.assertRaisesRegex(ValueError, '고객사'):
                    validate_plan(q, proposal(filters=[customer('신한', 'equals', '신한')]), {}, {})
                plan = validate_plan(q, proposal(filters=[customer(values, operator, q)]), {}, {})
                self.assertEqual(plan['filter_clauses'][0]['value'], values)

    def test_rule_extractor_preserves_both_companies(self):
        for q in ['한투랑 신한 문서 찾아줘', '한투와 신한 문서 찾아줘']:
            with self.subTest(q=q):
                clauses = [c for c in extract_query_constraints(q)['filter_clauses'] if c['field'] == 'customer']
                self.assertEqual(clauses, [{'field': 'customer', 'operator': 'in', 'value': ['한투', '신한']}])

    def test_followup_replacement_removal_and_independent_question(self):
        previous = validate_plan('한투 문서 찾아줘', proposal(filters=[customer()]), {}, {})
        changed = validate_plan('그중 신한으로 바꿔줘', proposal(followup=True, scope='inherit',
            status={'mode': 'inherit', 'values': [], 'evidence': ''}, filters=[customer('신한', evidence='신한')]), {}, previous)
        self.assertEqual(changed['resolved_constraints']['customer_key'], '신한')
        cleared = validate_plan('고객사 조건을 해제해줘', proposal(followup=True, scope='inherit',
            status={'mode': 'inherit', 'values': [], 'evidence': ''}, clear_fields=['customer']), {}, changed)
        self.assertIsNone(cleared['resolved_constraints']['customer_key'])
        fresh = validate_plan('노션에서 인증 문서 찾아줘', proposal(terms=['인증']), {}, previous)
        self.assertIsNone(fresh['resolved_constraints']['customer_key'])

    def test_missing_customer_uses_one_gemini_reinterpretation_with_same_checks(self):
        primary = {'id': 'luna', 'fallbackModelIds': ['gemini']}
        fallback = {'id': 'gemini', 'provider': 'gemini'}
        for valid in [True, False]:
            with self.subTest(valid=valid), \
                 patch.object(unified.llm, 'get_model_registry', return_value=[primary, fallback]), \
                 patch.object(unified.llm, 'get_fallback_models', return_value=[fallback]), \
                 patch.object(unified.llm, 'get_model_api_key', return_value='test-key'):
                calls = []
                def propose(model, _key, payload, calls=calls, valid=valid):
                    calls.append((model['id'], deepcopy(payload)))
                    return proposal(filters=[customer()] if valid and model['id'] == 'gemini' else [])
                with patch.object(unified, '_propose', side_effect=propose):
                    plan = unified.plan_question('한투 관련 문서 찾아줘', primary, 'test-key', {}, {})
                self.assertEqual([c[0] for c in calls], ['luna', 'luna', 'gemini'])
                if valid:
                    self.assertEqual(plan['resolved_constraints']['customer_key'], '한투')
                    self.assertEqual(plan['planner_model_id'], 'gemini')
                else:
                    self.assertEqual(plan['operation'], 'clarify')
                    self.assertNotIn('resolved_constraints', plan)


class NotionCustomerIdentityTests(unittest.TestCase):
    def test_registered_customer_aliases_keep_their_own_company(self):
        from backend.config import CUSTOMER_ALIASES
        from backend.customer_identity import canonical_customer, customer_mentions
        for key, aliases in CUSTOMER_ALIASES.items():
            for alias in [key, *aliases]:
                with self.subTest(customer=key, alias=alias):
                    self.assertEqual(canonical_customer(alias), key)
                    self.assertEqual({item[2] for item in customer_mentions(alias)}, {key})


    def test_customer_property_is_canonical_and_overrides_other_company_in_title(self):
        clause = [customer()]
        for value, text, expected in [('한국투자증권', '조건검색 서버', True),
                                      ('신한투자증권', '한투 관련 참고', False),
                                      ('한국투자증권, 신한투자증권', '공동 작업', True)]:
            with self.subTest(value=value):
                self.assertEqual(search.notion_page_matches_common_filters(
                    {'customer': value, 'search_text': text}, clause), expected)

    def test_text_fallback_uses_boundaries_and_keeps_non_title_properties(self):
        for text, expected in [('[신한투자증권] 서버 점검', False), ('신한투자증권 정기점검', False),
                               ('[한투] 서버 점검', True), ('한투에서 온 문의', True),
                               ('조건검색 서버 점검\n메모: 한국투자증권', True), ('한투자산운용 문서', False)]:
            with self.subTest(text=text):
                self.assertEqual(search.notion_page_matches_common_filters(
                    {'customer': '', 'title': '조건검색 서버', 'search_text': text}, [customer()]), expected)

    def test_exclusion_does_not_exclude_shinhan_and_multi_company_is_or(self):
        for value, expected in [('한국투자증권', False), ('신한투자증권', True)]:
            self.assertEqual(search.notion_page_matches_common_filters({'customer': value},
                [customer(operator='not_equals')]), expected)
        for value, expected in [('한국투자증권', True), ('신한투자증권', True), ('NH투자증권', False)]:
            self.assertEqual(search.notion_page_matches_common_filters({'customer': value},
                [customer(['한투', '신한'], 'in')]), expected)

    def test_api_options_use_same_company_identity(self):
        schema = {'고객사': {'type': 'select', 'select': {'options': [
            {'name': '한국투자증권'}, {'name': '신한투자증권'}, {'name': 'NH투자증권'}]}}}
        with patch.object(notion_schema, 'notion_property_schema', return_value=schema):
            self.assertEqual(search.notion_database_customer_filter('db', [customer()], []),
                             {'or': [{'property': '고객사', 'select': {'equals': '한국투자증권'}},
                                     {'property': '고객사', 'select': {'is_empty': True}}]})

    def test_rich_text_customer_cannot_be_hidden_by_other_select_customer_filter(self):
        schema = {'고객사': {'type': 'select', 'select': {'options': [{'name': '신한투자증권'}]}},
                  '고객': {'type': 'rich_text'}}
        with patch.object(notion_schema, 'notion_property_schema', return_value=schema):
            self.assertIsNone(search.notion_database_customer_filter('db', [customer('신한')], []))

    def test_missing_property_does_not_install_lossy_title_filter(self):
        with patch.object(notion_schema, 'notion_property_schema', return_value={'제목': {'type': 'title'}}):
            self.assertIsNone(search.notion_database_customer_filter('db', [customer()], []))

    def test_candidate_pagination_keeps_empty_customer_rows_and_stops_at_verified_limit(self):
        for has_customer_schema in (True, False):
            with self.subTest(customer_schema=has_customer_schema):
                schema = {'제목': {'type': 'title'}, '고객사': {'type': 'select',
                          'select': {'options': [{'name': '한국투자증권'}]}}}
                if not has_customer_schema:
                    schema = {'제목': {'type': 'title'}, '메모': {'type': 'rich_text'}}
                def page(number, title, has_customer_schema=has_customer_schema):
                    result = {'id': f'page-{number}', 'parent': {'database_id': 'db'},
                            'created_time': '2026-10-30T00:00:00Z', 'last_edited_time': '2026-10-30T00:00:00Z',
                            'properties': {'제목': {'type': 'title', 'title': [{'plain_text': title}]},
                                           '고객사': {'type': 'select', 'select': None}}}
                    if not has_customer_schema:
                        result['properties'].pop('고객사')
                        result['properties']['제목']['title'] = [{'plain_text': f'문서 {number}'}]
                        result['properties']['메모'] = {'type': 'rich_text', 'rich_text': [{'plain_text': title}]}
                    return result
                calls = []
                def query(_url, payload, calls=calls, page=page, **_kwargs):
                    calls.append(payload)
                    response = Mock(status_code=200)
                    response.json.return_value = {'results': [page(0, '[신한투자증권] 서버 점검')] if len(calls)==1
                                                 else [page(i, '[한투] 서버 점검') for i in range(1,6)],
                                                 'has_more': True, 'next_cursor': f'cursor-{len(calls)}'}
                    return response
                with patch.object(search, 'resolve_notion_settings', return_value={'token':'test','database_ids':['db']}), \
                     patch.object(search, 'resolve_notion_read_scope', return_value=({'db':'db'}, True)), \
                     patch.object(read_api, 'fetch_notion_database_title', return_value='이슈리스트'), \
                     patch.object(notion_schema, 'notion_property_schema', return_value=schema), \
                     patch.object(transport, 'notion_post_json', side_effect=query):
                    result = search.execute_notion_search('노션에서 한투 고객사 항목 보여줘',
                        constraints={'customer_aliases':['한국투자증권'], 'filter_clauses':[customer()]}, include_blocks=False)
                self.assertEqual([p['id'] for p in result['notion_pages']], [f'page-{i}' for i in range(1,6)])
                self.assertEqual(len(calls), 2)
                self.assertTrue(result['meta']['result_window_complete'])

                if not has_customer_schema:
                    self.assertTrue(all('filter' not in payload for payload in calls))
