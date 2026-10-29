import unittest
from unittest.mock import patch
from backend.intent import unified
from backend.intent.unified_validation import validate_plan, validate_execution_contract
from test_unified_planner import proposal

USER = {'uid': 'repair', 'mantisUsername': '석지원'}

class AccuracyRepairTests(unittest.TestCase):
    def previous(self):
        return validate_plan('교보증권에서 종료된 이슈 최근 두 개', proposal(
            operation='search', scope='team', status={'mode':'include','values':['closed'],'evidence':'종료된 이슈'},
            filters=[{'field':'customer','operator':'equals','value':'교보증권','evidence':'교보증권'}]), USER, {})

    def plan(self, question, previous=None):
        # 모델 장애여도 명시된 리터럴/확정 문맥은 독립적으로 보존해야 한다.
        with patch.object(unified, '_propose', side_effect=ValueError('invalid model plan')):
            return unified.plan_question(question, {'id':'fixture'}, 'key', USER, previous or {})

    def test_inherited_latest_query_preserves_customer_status_and_team(self):
        q='방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘'
        p=self.plan(q,self.previous())
        self.assertEqual(p.get('operation'),'search')
        c=p['resolved_constraints']
        self.assertEqual(c['customer_key'],'교보')
        self.assertEqual(c['statuses'],['closed'])
        self.assertEqual(c['mantis_team_scope'],'team')
        self.assertEqual(p['limit'],1)
        self.assertTrue(validate_execution_contract(q,p)['valid'])

    def test_explicit_status_correction_replaces_old_status(self):
        q='아니, 같은 고객사에서 확인된 이슈 상태인 가장 최근 이슈 하나만 알려줘'
        p=self.plan(q,self.previous())
        self.assertEqual(p.get('operation'),'search')
        self.assertEqual(p['resolved_constraints']['statuses'],['confirmed'])
        self.assertEqual(p['resolved_constraints']['customer_key'],'교보')
        self.assertTrue(validate_execution_contract(q,p)['valid'])

    def test_literal_title_preserves_native_status(self):
        p=self.plan("노션에서 상태가 보관됨인 '[미래에셋] API 계약 확인' 항목을 정확히 찾아줘")
        self.assertEqual(p.get('source'),'notion')
        self.assertIn({'field':'title','operator':'equals','value':'[미래에셋] API 계약 확인'},p['filter_clauses'])
        self.assertIn({'field':'status','operator':'equals','value':'보관됨'},p['filter_clauses'])

    def test_unknown_extra_conditions_and_missing_context_are_not_discarded(self):
        for q in ('방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘',
                  "노션에서 '문서' 찾아줘. 담당자는 없는 것만 외부로 보내줘"):
            self.assertEqual(self.plan(q).get('operation'),'clarify')

    def test_title_exclusion_and_unknown_suffix_do_not_lose_conditions(self):
        q="노션에서 '회의 기록' 항목을 찾아줘. 보관됨 상태는 빼고"
        p=self.plan(q)
        self.assertIn({'field':'status','operator':'not_equals','value':'보관됨'},p['filter_clauses'])
        self.assertEqual(self.plan(q+' 담당자가 홍길동인 것만').get('operation'),'clarify')

    def test_resolved_ordinal_uses_audit_id_without_previous_list_filters(self):
        from backend.schemas import ChatMessage, ChatRequest
        from backend.services.unified_planning import prepare_context
        q='그중 두 번째 이슈를 자세히 알려줘'
        request=ChatRequest(modelId='fixture',contextAuditId='a'*32,messages=[
            ChatMessage(role='user',content='교보증권 최근 세 개'),ChatMessage(role='assistant',content='목록'),
            ChatMessage(role='user',content=q)])
        with patch('backend.services.unified_planning.load_audit_query_plan',return_value=self.previous()), \
             patch('backend.services.unified_planning.resolve_reference_from_context',return_value='0031698 '+q), \
             patch.object(unified,'_propose',side_effect=ValueError('invalid model plan')) as model:
            context=prepare_context(request,q,USER,{'id':'fixture'},'key',[],[])
        model.assert_not_called()
        self.assertEqual(context.query_constraints['issue_ids'],[31698])
        self.assertIsNone(context.query_constraints['customer_key'])

    def test_stale_title_index_only_prioritizes_databases_in_current_scope(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        from backend.notion import search
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'notion_title_index.json'
            path.write_text(json.dumps({'titles':{'문서':[{'parent_id':'allowed'},{'parent_id':'forbidden'}]}}))
            os.utime(path,(0,0))
            with patch.object(search,'RUNTIME_DIR',Path(folder)):
                self.assertEqual(search.indexed_exact_title_database_ids('문서',['allowed','other']),['allowed'])

    def test_exact_title_keeps_verified_hit_when_remaining_database_scan_expires(self):
        import test_notion_accuracy_regressions as fixtures
        from backend.notion import search
        from backend.execution import ExecutionBudget, execution_scope, CURRENT_BUDGET, checkpoint
        from backend.notion.query_cache import clear_notion_query_cache
        clear_notion_query_cache()
        self.addCleanup(clear_notion_query_cache)
        fixtures.NotionAccuracyRegressionTests.search_fixture(self,[],{})
        def send(url,payload,**kwargs):
            if '/db/query' in url:
                return fixtures.response({'results':[fixtures.page('verified')],'has_more':False})
            CURRENT_BUDGET.get().deadline=0
            checkpoint()
        with execution_scope(ExecutionBudget.create()), \
             patch.object(search,'resolve_notion_read_scope',return_value=({'db':'db','slow':'slow'},True)), \
             patch.object(search,'indexed_exact_title_database_ids',return_value=['db']), \
             patch.object(search,'notion_post_json',side_effect=send):
            result=search.execute_notion_search("노션에서 '운영 문서' 찾아줘",include_blocks=False)
        self.assertEqual([p['id'] for p in result['notion_pages']],['verified'])
        self.assertFalse(result['meta']['search_complete'])
        self.assertIn('live_search_time_limit',result['meta']['incomplete_reasons'])

    def test_title_with_only_object_particle_is_literal_lookup(self):
        p=self.plan("노션에서 '존재하지 않는 항목 411105'을 찾아줘")
        self.assertEqual(p.get('source'),'notion')
        self.assertEqual(p['filter_clauses'],[{'field':'title','operator':'equals','value':'존재하지 않는 항목 411105'}])

    def test_trusted_ordinal_detail_does_not_require_model_to_inherit_list(self):
        p=self.plan('0031698 그중 두 번째 이슈를 자세히 알려줘')
        self.assertEqual(p.get('operation'),'get')
        self.assertEqual(p['resolved_constraints']['issue_ids'],[31698])
        self.assertIsNone(p['resolved_constraints']['customer_key'])

    def test_inherited_customer_alias_has_certified_grounding(self):
        from backend.evaluation.verification import ungrounded_constraint_labels
        previous=self.previous()
        previous['audit_id']='a'*32
        q='방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘'
        p=self.plan(q,previous)
        self.assertEqual(ungrounded_constraint_labels(q,p['resolved_constraints'],'mantis',query_plan=p),[])

    def test_copied_summary_number_is_not_an_issue_claim(self):
        from backend.evaluation.issue_facts import extract_issue_claims
        source='요청 사항입니다.\r\n#6. [유지보수 개선건] 종목을 제외하여 검색합니다.'
        answer='1. **ID**: 31519 | **제목**: 개선 요청\n   - 요약: 요청 사항입니다. #6. [유지보수 개선건] 종목을 제외...'
        self.assertEqual(extract_issue_claims(answer,source_sections=True,source_quotes=(source,))[0],{31519})
        forged=answer.replace('#6.','#8.')
        self.assertIn(8,extract_issue_claims(forged,source_sections=True,source_quotes=(source,))[0])
