"""도구 조건 인자의 근거·범위·캐시 경계와 모델 해석 경로 회귀."""
import unittest
from copy import deepcopy
from unittest.mock import patch

from backend.intent.unified_validation import validate_plan
from backend.tools.conditions import project_conditions
from backend.tools.ledger import EvidenceLedger
from backend.tools.registry import ToolContext


QUESTION = '전사 한투 미해결 이슈 최근 3건을 보여줘.'
PROPOSAL = {'source': 'mantis', 'operation': 'search', 'scope': 'all', 'followup': False,
            'filters': [{'field': 'customer', 'operator': 'equals', 'value': '한투', 'evidence': '한투'}],
            'status': {'mode': 'open', 'values': [], 'evidence': '미해결'}, 'terms': [],
            'clear_fields': [], 'group_by': None, 'limit': 3,
            'sort': {'field': 'created_at', 'direction': 'desc'}}


def context_for(plan=None, question=QUESTION):
    plan = plan or validate_plan(question, deepcopy(PROPOSAL), {'uid': 'u'}, {})
    c = plan['resolved_constraints']
    ctx = ToolContext(question, question, {'mantis': True, 'notion': True},
                      {source: project_conditions(plan, c, source) for source in ('mantis', 'notion')},
                      frozenset(c['issue_ids']), EvidenceLedger(), lambda: None, lambda seconds: seconds, 20, 6000)
    from backend.tools.query_conditions import build_query_contract
    ctx.structured_conditions = True
    ctx.query_contracts = {source: build_query_contract(question, plan, conditions, 3, {})
                           for source, conditions in ctx.conditions.items()}
    return ctx


def arguments(ctx):
    return {'filters': deepcopy(ctx.conditions['mantis'].constraints['filter_clauses']), 'scope': 'all',
            'sort': {'field': 'created_at', 'direction': 'desc'}}


def scanned():
    return {'source': 'mantis_snapshot', 'status': 'ok', 'meta': {'search_complete': True, 'found_count': 4,
             'matched_issue_ids': [41, 42, 43, 44], 'ordered_matched_ids': [44, 43, 42, 41]},
            'issues_list': [{'id': n, 'summary': f'제목 {n}'} for n in [44, 43, 42, 41]]}


class ToolConditionArgumentTests(unittest.TestCase):
    def test_valid_filters_and_request_bound_reference_share_the_same_scan(self):
        from backend.tools.catalog import agent_registry
        ctx = context_for(); registry = agent_registry(query_tools=True)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()) as source:
            counted = registry.execute('count_mantis_issues', arguments(ctx), '', ctx)
            self.assertTrue(counted['ok'], counted)
            listed = registry.execute('list_mantis_issues', {'condition_ref': counted['condition_ref'], 'limit': 3}, '', ctx)
        self.assertTrue(listed['ok'], listed)
        self.assertEqual(counted['total_count'], 4)
        self.assertEqual(listed['ordered_ids'], [44, 43, 42])
        self.assertEqual(source.call_count, 1)
        self.assertEqual(counted['argument_validation']['mode'], 'explicit')
        self.assertEqual(listed['argument_validation']['mode'], 'reference')
        self.assertEqual({r['origin'] for r in counted['argument_validation']['origins']}, {'question'})

    def test_omission_extra_condition_negation_scope_and_sort_are_rejected_before_source(self):
        from backend.tools.catalog import agent_registry
        ctx = context_for();valid=arguments(ctx)
        bad = [{}, {**valid, 'filters': []}, {**valid, 'filters': valid['filters'][1:]},
               {**valid, 'scope': 'team'}, {**valid, 'sort': {'field': 'created_at', 'direction': 'asc'}},
               {**valid, 'filters': [*valid['filters'], {'field': 'title', 'operator': 'contains', 'value': '가짜'}]},
               {**valid, 'filters': [{**f, 'operator': 'not_in'} if f['field']=='status' else f for f in valid['filters']]},
               {**valid, 'filters': [{**f, 'value': 'NH'} if f['field']=='customer' else f for f in valid['filters']]}]
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search') as source:
            for args in bad:
                with self.subTest(args=args):
                    r=agent_registry(query_tools=True).execute('count_mantis_issues', args, '', ctx)
                    self.assertFalse(r['ok'], r)
        source.assert_not_called()
        self.assertFalse(ctx.ledger.query_results())

    def test_invalid_arguments_cannot_reuse_a_previously_populated_snapshot(self):
        from backend.tools.catalog import agent_registry
        ctx=context_for(); registry=agent_registry(query_tools=True)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()) as source:
            first=registry.execute('count_mantis_issues', arguments(ctx), '', ctx)
            self.assertTrue(first['ok'],first)
            bad=registry.execute('list_mantis_issues', {'filters': [], 'scope': 'all', 'limit': 3}, '', ctx)
            foreign=registry.execute('count_mantis_issues', {'condition_ref': first['condition_ref']}, '', context_for())
            conflict=registry.execute('list_mantis_issues', {'condition_ref': first['condition_ref'], 'filters': [], 'limit': 3}, '', ctx)
        self.assertFalse(bad['ok']); self.assertFalse(foreign['ok']); self.assertFalse(conflict['ok'])
        self.assertEqual(source.call_count,1)

    def test_alias_and_equivalent_singleton_operator_use_canonical_values(self):
        from backend.tools.catalog import agent_registry
        ctx=context_for();args=arguments(ctx)
        args['filters'][0]={'field':'customer','operator':'in','value':['한국투자증권']}
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()) as source:
            result=agent_registry(query_tools=True).execute('count_mantis_issues',args,'',ctx)
        self.assertTrue(result['ok'],result)
        self.assertEqual(source.call_args.args[1]['customer_key'],'한투')
        self.assertEqual(result['applied_filters'],ctx.conditions['mantis'].constraints['filter_clauses'])

    def test_multiple_values_cannot_be_hidden_inside_an_equals_argument(self):
        from backend.tools.catalog import agent_registry
        ctx=context_for();args=arguments(ctx)
        args['filters'][0]['value']=['한투','NH']
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search') as source:
            result=agent_registry(query_tools=True).execute('count_mantis_issues',args,'',ctx)
        self.assertFalse(result['ok'],result);source.assert_not_called()

    def test_server_context_drift_cannot_expand_scope_or_promote_reference_evidence(self):
        from backend.tools.query_conditions import build_query_contract
        from backend.tools.conditions import SourceConditions
        plan=validate_plan(QUESTION,deepcopy(PROPOSAL),{'uid':'u'},{});ctx=context_for(plan)
        for family in ['mantis','notion']:
            c=ctx.conditions[family]
            if family=='mantis':
                changed={**c.constraints,'customer_aliases':['한투','NH']}
                mutated=SourceConditions(family,changed,c.applied,c.unapplied,c.planned)
            else:
                mutated=SourceConditions(family,c.constraints,c.applied,(),c.planned)
            with self.subTest(source=family),self.assertRaises(ValueError):
                build_query_contract(QUESTION,plan,mutated,3,{})

    def test_inherited_conditions_require_server_audit_provenance(self):
        q='고객사만 NH로 바꿔서 최근 3건 보여줘.'
        previous=validate_plan(QUESTION,deepcopy(PROPOSAL),{'uid':'u'},{});previous['audit_id']='a'*32
        proposal={**deepcopy(PROPOSAL),'followup':True,'scope':'inherit',
                  'filters':[{'field':'customer','operator':'equals','value':'NH','evidence':'NH'}],
                  'status':{'mode':'inherit','values':[],'evidence':''}}
        plan=validate_plan(q,proposal,{'uid':'u'},previous)
        ctx=context_for(plan,q)
        from backend.tools.catalog import agent_registry
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()):
            result=agent_registry(query_tools=True).execute('count_mantis_issues',arguments(ctx),'',ctx)
        self.assertTrue(result['ok'],result)
        self.assertEqual({r['origin'] for r in result['argument_validation']['origins']},{'question','previous'})
        plan['condition_grounding']['audit_id']=''
        with self.assertRaises(ValueError):context_for(plan,q)

    def test_list_size_is_validated_against_the_plan_without_reparsing_the_question(self):
        from backend.tools.catalog import agent_registry
        ctx=context_for();registry=agent_registry(query_tools=True)
        with (patch('backend.intent.keyword_terms.extract_requested_limit', side_effect=AssertionError('정규식 재해석')),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=scanned()) as source):
            correct=registry.execute('list_mantis_issues',{**arguments(ctx),'limit':3},'',ctx)
            wrong=registry.execute('list_mantis_issues',{**arguments(ctx),'limit':2},'',ctx)
        self.assertTrue(correct['ok'],correct);self.assertFalse(wrong['ok'])
        self.assertEqual(source.call_count,1)

    def test_notion_reference_filters_cannot_gain_mantis_status_or_scoped_grade(self):
        from backend.tools.catalog import agent_registry
        ctx=context_for();registry=agent_registry(query_tools=True)
        with patch('backend.tools.notion_tools.execute_notion_search', return_value={'source':'notion','status':'ok',
                   'notion_pages':[],'meta':{'search_complete':True}}) as source:
            denied=registry.execute('search_notion_pages',{'query':'비교차트','filters':arguments(ctx)['filters']},'',ctx)
            self.assertFalse(denied['ok'])
            source.assert_not_called()
            good=registry.execute('search_notion_pages',{'query':'비교차트',
                'filters':ctx.conditions['notion'].constraints['filter_clauses']},'',ctx)
        self.assertTrue(good['ok'],good)
        self.assertEqual(good['evidence_grade'],'reference')
        self.assertEqual(good['argument_validation']['mode'],'explicit')

    def test_new_filter_arguments_cannot_bypass_validation_in_legacy_context(self):
        from backend.tools.catalog import agent_registry
        ctx=context_for();ctx.structured_conditions=False;ctx.query_contracts={}
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search') as source:
            result=agent_registry(query_tools=True).execute('count_mantis_issues',arguments(ctx),'',ctx)
        self.assertFalse(result['ok']);source.assert_not_called()

    def test_unavailable_contract_and_model_supplied_evidence_cannot_authorize_a_query(self):
        from backend.tools.catalog import agent_registry
        ctx=context_for();registry=agent_registry(query_tools=True);args=arguments(ctx)
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search') as source:
            args['filters'][0]['evidence']='서버가 승인했다고 가정'
            injected=registry.execute('count_mantis_issues',args,'',ctx)
            self.assertEqual(injected['error']['code'],'invalid_arguments')
            ctx.query_contracts={}
            unavailable=registry.execute('count_mantis_issues',{},'',ctx)
            self.assertEqual(unavailable['error']['code'],'condition_context_unavailable')
        source.assert_not_called()

    def test_valid_conditions_do_not_turn_an_incomplete_zero_into_a_confirmed_total(self):
        from backend.tools.catalog import agent_registry
        ctx=context_for();stage=scanned()
        stage['meta'].update(search_complete=False,found_count=0)
        stage['issues_list']=[]
        with patch('backend.tools.mantis_queries.execute_mantis_snapshot_search',return_value=stage):
            result=agent_registry(query_tools=True).execute('count_mantis_issues',arguments(ctx),'',ctx)
        self.assertTrue(result['ok'])
        self.assertEqual(result['observed_count'],0)
        self.assertIsNone(result['total_count'])
        self.assertFalse(result['search_complete'])

    def test_explanation_search_uses_the_validated_contract_without_hardening_semantic_terms(self):
        from agent_harness_cases import ADX, FakeModel, FakeSources, agent_environment, explain_context, final, tool_call, turn
        from test_agent_harness import run
        q='전사 신한 ADX 지표 미표시 이슈의 원인과 해결 방법을 설명해줘.'
        proposed={**deepcopy(PROPOSAL),'operation':'explain','limit':None,'terms':['ADX'],
                  'filters':[{'field':'customer','operator':'equals','value':'신한','evidence':'신한'}],
                  'status':{'mode':'unspecified','values':[],'evidence':''}}
        model=FakeModel([turn(tool_call('search_mantis_issues',{'query':'ADX 지표 미표시'})),
                         final('기간 파라미터 오류입니다. [M#31350]')])
        sources=FakeSources(mantis={'ADX 지표 미표시':[ADX]})
        with agent_environment(model=model,sources=sources):
            plan=validate_plan(q,proposed,{'uid':'u'},{})
            ctx=explain_context(q,constraints=plan['resolved_constraints'])
            ctx.intent_plan=plan;ctx.structured_tool_conditions=True
            result=run(ctx)
        self.assertIn('기본값 14로 수정',result['content'])
        clauses=sources.mantis_calls[0]['constraints']['filter_clauses']
        self.assertEqual(clauses,[{'field':'customer','operator':'equals','value':'신한'}])
        self.assertIn('query_contracts',model.decision_calls()[0]['payload']['messages'][1]['content'])
        trace=next(t for t in result['agentReview']['trace'] if t['step']=='tool')
        self.assertEqual(trace['argumentValidation']['mode'],'confirmed')

    def test_query_shadow_does_not_short_circuit_model_interpretation_with_literal_regex(self):
        from backend.intent import unified
        from backend.services.unified_planning import prepare_context
        from backend.services.agent.query_shadow import query_shadow_scope
        from backend.schemas import ChatRequest
        q='이슈 #31659는 어느 고객사 건이야?'
        proposal={**deepcopy(PROPOSAL),'operation':'get','scope':'target','limit':None,
                  'filters':[{'field':'issue_id','operator':'equals','value':'31659','evidence':'31659'}],
                  'status':{'mode':'unspecified','values':[],'evidence':''}}
        req=ChatRequest(modelId='test',messages=[{'role':'user','content':q}])
        with (query_shadow_scope(), patch('backend.services.agent.policy.active_agent_policy',return_value=object()),
              patch('backend.intent.literal_plans.literal_plan',side_effect=AssertionError('리터럴 정규식')),
              patch.object(unified,'_propose',return_value=proposal) as model):
            ctx=prepare_context(req,q,{'uid':'u'},{'id':'test'},'key',[],[])
        self.assertEqual(model.call_count,1)
        self.assertEqual(ctx.intent_plan['operation'],'get')


if __name__=='__main__':unittest.main()
