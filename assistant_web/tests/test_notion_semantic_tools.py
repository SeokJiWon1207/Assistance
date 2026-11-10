"""에이전트가 의미 검색에서 문서 탐색·실제 본문 읽기로 이어지는지 검사한다."""
import unittest
from unittest.mock import patch

from test_tool_condition_arguments import context_for


class NotionSemanticToolTests(unittest.TestCase):
    def test_long_body_reaches_the_model_with_the_current_matching_passage(self):
        from backend.tools.catalog import agent_registry
        from backend.tools.registry import fit_result
        import json
        ctx=context_for();ctx.max_result_chars=1000
        passage='종목 추가 버튼을 선택합니다.'
        body='도입부\n'*2000+passage+'\\\"'*4000
        ctx.ledger.add('notion',{'source':'notion','status':'ok','notion_pages':[
            {'id':'long','title':'한투 사용 방법','content':body,'content_incomplete':False,
             '_verified_body':True,'semantic_excerpt':passage}], 'meta':{'search_complete':False}},
            grade='reference',unapplied=('상태',),call_id='search')
        result=agent_registry().execute('get_notion_page_content',{'page_ref':'N1'},'',ctx)
        sent=json.loads(fit_result(result,ctx.max_result_chars))
        self.assertTrue(sent['results'])
        self.assertIn(passage,sent['results'][0]['content'])
        self.assertTrue(sent['truncated'])

    def test_explanation_topic_is_not_silently_promoted_to_an_exact_document_title(self):
        from backend.intent import unified
        from copy import deepcopy
        q='한투 조건검색 서버 문서에서 서버 접속 방법을 설명해줘.'
        plan={'source':'notion','operation':'explain','scope':'all','followup':False,
              'filters':[{'field':'customer','operator':'equals','value':'한투','evidence':'한투'}],
              'status':{'mode':'unspecified','values':[],'evidence':''},'clear_fields':[],
              'terms':['조건검색','서버','접속'],'group_by':None,'limit':None}
        wrong=deepcopy(plan)
        wrong['filters'].append({'field':'title','operator':'equals','value':'조건검색 서버 문서','evidence':'조건검색 서버 문서'})
        with patch.object(unified,'_propose',side_effect=[wrong,plan]) as propose:
            result=unified.plan_question(q,{'id':'test'},'test',{}, {})
        self.assertEqual(propose.call_count,2)
        self.assertEqual(propose.call_args.args[2]['messages'][-2]['role'], 'assistant')
        self.assertFalse(any(c['field']=='title' for c in result['filter_clauses']))
        contains = deepcopy(wrong); contains['filters'][-1]['operator'] = 'contains'
        with self.assertRaisesRegex(ValueError, 'terms'):
            unified.validate_plan(q, contains, {}, {})
        quoted=deepcopy(wrong);quoted['terms']=[]
        exact=unified.validate_plan("한투 '조건검색 서버 문서'를 설명해줘.",quoted,{}, {})
        self.assertTrue(any(c['field']=='title' for c in exact['filter_clauses']))

    def test_search_navigation_and_read_preserve_reference_grade_and_issue_binding(self):
        from backend.tools.catalog import agent_registry
        from backend.tools import notion_tools
        ctx=context_for();ctx.related_ordinal=1
        ctx.ledger.add('mantis',{'source':'mantis_snapshot','status':'ok','issues_list':[{'id':42,'summary':'비교차트 업데이트'}],
            'meta':{'query_result':{'operation':'list','ordered_ids':[42]}}},grade='scoped',unapplied=(),call_id='list')
        root={'id':'root','database_id':'db','title':'한투 비교차트 자료','content':'','content_incomplete':False,
              '_verified_body':True,'_verified_page_links':[{'page_id':'guide','kind':'child'}]}
        child={'id':'guide','database_id':'db','title':'한투 비교차트 사용 방법','content':'','content_incomplete':True}
        current={**child,'_verified_body':True,'content':'비교차트에서 종목 추가 버튼을 선택합니다.',
                 'content_incomplete':False,'_verified_page_links':[]}
        registry=agent_registry(query_tools=True)
        self.assertIn('search_notion_body',registry.specs)
        self.assertIn('list_notion_page_links',registry.specs)
        with (patch.object(notion_tools,'execute_notion_body_search',return_value={
                  'source':'notion','status':'ok','notion_pages':[root],'meta':{'coverage':'top_k','search_complete':False}}),
              patch.object(notion_tools,'notion_scope',return_value={'db':'db'}),
              patch.object(notion_tools,'follow_page_links',return_value=([child],{'search_complete':False})),
              patch.object(notion_tools,'read_current_page',return_value=current),
              patch.object(notion_tools.related_guides,'find_alternate_guide',side_effect=AssertionError('모델 흐름 대신 자동 조회'))):
            found=registry.execute('search_notion_body',{'query':'비교차트 사용 방법'},'',ctx)
            self.assertTrue(found['ok'],found);self.assertEqual(found['evidence_grade'],'reference')
            self.assertFalse(found['search_complete'])
            links=registry.execute('list_notion_page_links',{'page_ref':'N1'},'',ctx)
            self.assertEqual(links['results'][0]['ref'],'N2')
            result=registry.execute('get_notion_page_content',{'page_ref':'N2'},'',ctx)
        self.assertTrue(result['ok'],result)
        self.assertIn('종목 추가 버튼',result['results'][0]['content'])
        self.assertTrue(ctx.has_related_guide())
        self.assertEqual(ctx.ledger.related_pages['guide']['issue_id'],42)
        self.assertEqual(ctx.ledger.page_grade('guide'),'reference')

    def test_page_access_revocation_removes_old_evidence_and_unknown_ref_never_reads(self):
        from backend.tools.catalog import agent_registry
        from backend.tools import notion_tools
        ctx=context_for();registry=agent_registry(query_tools=True)
        ctx.ledger.add('notion',{'source':'notion','status':'ok','notion_pages':[
            {'id':'old','database_id':'db','title':'예전 문서','content':'이미 폐기된 원문'}],
            'meta':{'search_complete':True}},grade='scoped',unapplied=(),call_id='search')
        with (patch.object(notion_tools,'notion_scope',return_value={'db':'db'}),
              patch.object(notion_tools,'read_current_page',return_value=None) as read):
            unknown=registry.execute('list_notion_page_links',{'page_ref':'N99'},'',ctx)
            self.assertFalse(unknown['ok']);read.assert_not_called()
            result=registry.execute('get_notion_page_content',{'page_ref':'N1'},'',ctx)
        self.assertFalse(result['ok']);self.assertEqual(ctx.ledger.notion_pages(),[])
        self.assertNotIn('N1',ctx.ledger.refs())

    def test_disabled_body_reading_keeps_title_and_property_evidence(self):
        from backend.tools.catalog import agent_registry
        from backend.tools import notion_tools
        ctx=context_for();registry=agent_registry(query_tools=True)
        ctx.ledger.add('notion',{'source':'notion','status':'ok','notion_pages':[
            {'id':'page','database_id':'db','title':'찾은 문서','properties_text':'상태: 시작전'}],
            'meta':{'search_complete':True}},grade='scoped',unapplied=(),call_id='search')
        with (patch.dict('os.environ',{'NOTION_FETCH_PAGE_BLOCKS':'false'}),
              patch.object(notion_tools,'notion_scope',return_value={'db':'db'})):
            for tool in ('get_notion_page_content','list_notion_page_links'):
                result=registry.execute(tool,{'page_ref':'N1'},'',ctx)
                self.assertEqual(result['error']['code'],'body_disabled',result)
        self.assertEqual([p['title'] for p in ctx.ledger.notion_pages()],['찾은 문서'])
        self.assertTrue(ctx.ledger.stages[0].stage['meta']['search_complete'])


if __name__=='__main__':unittest.main()
