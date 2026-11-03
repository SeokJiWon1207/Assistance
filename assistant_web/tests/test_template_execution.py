"""검증된 보고서 종류가 일반 목록/설명으로 바뀌지 않는 실행 계약."""
from unittest import TestCase
from unittest.mock import patch
from backend.schemas import ChatRequest
from backend.services.contracts import ChatPipelineContext, DeterministicReportResult
from backend.services.unified_planning import execute_plan


class TemplateExecutionTests(TestCase):
    def test_empty_similarity_still_cites_the_verified_seed_without_inventing_candidates(self):
        from backend.reports import similar
        raw={'id':9000,'summary':'표시 오류','project':{'name':'기준증권'},'notes':[]}
        with patch.object(similar,'fetch_mantis_issue_by_id',return_value=raw), \
             patch.object(similar,'execute_chroma_search',return_value={'status':'ok','documents':[]}):
            r=similar.build_similar_resolution_report('#9000과 비슷한 과거 이슈 원인과 조치 비교해줘')
        self.assertEqual([s['issue_id'] for s in r['sources']],[9000])
        self.assertEqual(r['retrieval']['stages'][0]['meta']['displayed_count'],0)
        self.assertIn('찾지 못했',r['content'])

    def test_validated_similar_plan_survives_a_short_followup_without_legacy_keywords(self):
        from backend.intent.confirmed_context import confirmed_constraints_scope
        from backend.sources.mantis import normalize_issue
        q='그 범위로 다시 정리해줘'
        constraints={'issue_ids':[9000],'unified_plan_validated':True,'people':{'names':[]}}
        ctx=ChatPipelineContext(q,'test','key',[],q,{'mantis':True,'vectorDb':True},[],constraints,5,
            intent_plan={'planner':'unified_llm','source':'mantis','operation':'report','report_kind':'similar'})
        raw={'id':9000,'summary':'실시간 값 표시 오류','project':{'name':'테스트증권'},'notes':[]}
        example=normalize_issue({**raw,'id':8000})
        with confirmed_constraints_scope(q,constraints), \
             patch('backend.reports.similar.fetch_mantis_issue_by_id',return_value=raw), \
             patch('backend.reports.similar.execute_chroma_search',return_value={
                 'status':'ok','documents':[{'issue_id':8000,'latest_issue':example}]}):
            r=execute_plan(ChatRequest(modelId='test',messages=[{'role':'user','content':q}]),ctx,{})
        self.assertEqual(r['raw']['mode'],'deterministic_similar_resolution')
        self.assertIn('원인·해결 비교',r['content'])
        self.assertEqual({s['issue_id'] for s in r['sources']},{9000,8000})

    def test_similarity_seed_is_a_reference_but_candidates_keep_all_user_filters(self):
        from backend.evaluation.report_scopes import report_evidence_constraints
        constraints={'customer_key':'선택증권','platform_key':'hts','created_at':{'from':'2026-01-01'}}
        retrieval={'stages':[{'source':'mantis_similar_resolution','meta':{'base_issue_ids':[9000]}}]}
        seed=report_evidence_constraints('deterministic_similar_resolution',{'issue_id':9000},constraints,retrieval)
        candidate=report_evidence_constraints('deterministic_similar_resolution',{'issue_id':8000},constraints,retrieval)
        self.assertEqual(seed,{'issue_ids':[9000]})
        self.assertEqual(candidate,constraints)

    def test_specialized_builders_are_selected_from_the_validated_plan(self):
        for kind,builder in [('profile','build_customer_profile_report'),('monthly','build_monthly_customer_report'),
                             ('weekly','build_weekly_report'),('similar','build_similar_resolution_report'),
                             ('triage','build_incident_triage_report')]:
            with self.subTest(kind=kind):
                ctx=ChatPipelineContext('NH 보고서','test','key',[],'NH 보고서',
                    {'mantis':True,'vectorDb':True},[],{},5,
                    intent_plan={'planner':'unified_llm','source':'mantis','operation':'report','report_kind':kind})
                answer={'content':'전용 보고서','raw':{'mode':kind}}
                with patch('backend.services.unified_planning.'+builder,return_value=answer), \
                     patch('backend.services.unified_planning.build_deterministic_report',return_value=DeterministicReportResult()):
                    actual=execute_plan(ChatRequest(modelId='test',messages=[{'role':'user','content':'NH 보고서'}]),ctx,{})
                self.assertEqual(actual['raw']['mode'],kind)

    def test_effort_and_duration_both_survive_composition(self):
        from backend.reports.effort_sla import build_effort_sla_report
        effort={'content':'총 공수: **2.50**','sources':[{'issue_id':1}],
                'retrieval':{'constraints':{},'stages':[{'source':'mantis_effort_filtered','status':'ok','meta':{}}]},
                'raw':{'mode':'deterministic_filtered_effort_report'}}
        sla={'content':'평균 처리시간: 10.0일','sources':[{'issue_id':1},{'issue_id':2}],
             'retrieval':{'stages':[]},'raw':{'mode':'deterministic_sla_report'}}
        with patch('backend.reports.effort_sla.build_effort_report',return_value=effort), \
             patch('backend.reports.effort_sla.build_sla_report',return_value=sla):
            result=build_effort_sla_report('NH 공수와 평균 처리시간')
        self.assertIn('2.50',result['content'])
        self.assertIn('10.0일',result['content'])
        self.assertEqual([s['issue_id'] for s in result['sources']],[1,2])

    def test_effort_uses_confirmed_date_range_for_display_as_well_as_matching(self):
        from backend.reports.effort import extract_effort_query
        from backend.intent.confirmed_context import confirmed_constraints_scope
        q='지난달 NH 공수'
        c={'customer_key':'NH','customer_aliases':['NH'],'people':{'names':[]},
           'unified_plan_validated':True,'delivered_at':{'from':'2026-09-01','to':'2026-09-30'}}
        with confirmed_constraints_scope(q,c):
            query=extract_effort_query(q)
        self.assertEqual(query['date_range'],{'from':'2026-09-01','to':'2026-09-30'})

    def test_validated_customer_reports_do_not_require_legacy_request_phrases(self):
        from backend.reports import customer, monthly_customer
        from backend.sources.mantis_collection import IssueBatch
        raw=[{'id':101,'project':{'name':'132SHGM(신한굿모닝증권)'},'summary':'차트 오류','status':{'name':'assigned'},
              'handler':{'name':'석지원'},'created_at':'2026-10-02T09:00:00+09:00',
              'updated_at':'2026-10-03T09:00:00+09:00','custom_fields':[],'notes':[]}]
        c={'customer_key':'신한','customer_aliases':['신한투자증권'],'people':{'names':[]},'unified_plan_validated':True,
           'created_at':{'from':'2026-10-01','to':'2026-10-31','precision':'month'}}
        for kind,q,mode in [('profile','신한으로 바꿔서 다시 만들어줘','deterministic_customer_profile'),
                            ('monthly','그럼 신한은 어때?','deterministic_monthly_customer_report')]:
            with self.subTest(kind=kind):
                ctx=ChatPipelineContext(q,'test','key',[],q,{'mantis':True},[],c,5,
                    intent_plan={'planner':'unified_llm','source':'mantis','operation':'report','report_kind':kind})
                matched: list = []
                matches=lambda issue,constraints,seen=matched: seen.append(constraints.get('customer_key')) or True
                with patch.object(customer,'stream_complete_store') as opened, \
                     patch.object(customer,'extract_query_constraints',side_effect=AssertionError('질문 재해석')), \
                     patch.object(monthly_customer,'extract_query_constraints',side_effect=AssertionError('질문 재해석')), \
                     patch.object(customer,'issue_matches_constraints',side_effect=matches), \
                     patch.object(monthly_customer,'issue_matches_constraints',side_effect=matches), \
                     patch.object(monthly_customer,'load_report_issues',return_value=IssueBatch(raw,{'search_complete':True})):
                    opened.return_value.__enter__.return_value=IssueBatch(raw,{'search_complete':True})
                    r=execute_plan(ChatRequest(modelId='test',messages=[{'role':'user','content':q}]),ctx,{})
                self.assertEqual(r['raw']['mode'],mode)
                self.assertEqual(set(matched),{'신한'})
                self.assertEqual([s['issue_id'] for s in r['sources']],[101])

    def test_validated_triage_uses_plan_scope_and_terms_without_template_phrases(self):
        from backend.reports import incident_triage
        q='신한 HTS 차트가 안 떠. 어떻게 대응해야 해?'
        c={'customer_key':'신한','platform_key':'hts','platform_aliases':['HTS'],'people':{'names':[]},
           'unified_plan_validated':True}
        ctx=ChatPipelineContext(q,'test','key',[],q,{'mantis':True,'vectorDb':True},[],c,5,
            intent_plan={'planner':'unified_llm','source':'mantis','operation':'report','report_kind':'triage','terms':['차트']})
        with patch.object(incident_triage,'extract_query_constraints',side_effect=AssertionError('질문 재해석')), \
             patch.object(incident_triage,'execute_chroma_search',return_value={'status':'ok','documents':[]}) as search:
            r=execute_plan(ChatRequest(modelId='test',messages=[{'role':'user','content':q}]),ctx,{})
        self.assertEqual(r['raw']['mode'],'deterministic_incident_triage')
        self.assertIn('장애 초동 대응안 — 신한 · HTS',r['content'])
        self.assertIn('증상: **차트**',r['content'])
        self.assertEqual(search.call_args.args[0],'차트')
        self.assertEqual(search.call_args.args[2]['platform_key'],'hts')

    def test_legacy_report_builders_still_require_their_request_phrases(self):
        from backend.reports.customer import build_customer_profile_report
        from backend.reports.incident_triage import build_incident_triage_report
        from backend.reports.monthly_customer import build_monthly_customer_report
        self.assertIsNone(build_customer_profile_report('신한 이슈 5개 보여줘'))
        self.assertIsNone(build_monthly_customer_report('신한 이슈 5개 보여줘'))
        self.assertIsNone(build_incident_triage_report('신한 HTS 차트가 안 떠', search_similar=False))
