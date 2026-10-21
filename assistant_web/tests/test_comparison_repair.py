"""실제 비교 오류의 이력·증상 귀속·검색 조건을 재현한다."""
from unittest import TestCase

from backend.intent.unified_validation import validate_plan
from backend.reports import similar
from backend.reports.comparison_narrative import comparison_facts


def plan(**kw):
    return {'source':'mantis','operation':'report','report_kind':'similar','scope':'target','followup':False,
            'filters':[], 'status':{'mode':'unspecified','values':[],'evidence':''},
            'clear_fields':[], 'terms':[], 'group_by':None, 'limit':None, **kw}


class ComparisonRepairTests(TestCase):
    def test_similarity_topic_is_not_a_literal_filter_and_survives_followup(self):
        p=validate_plan('#90 이전 메모리 사용량 증가 사례를 비교해줘',plan(filters=[
            {'field':'issue_id','operator':'equals','value':'90','evidence':'#90'},
            {'field':'keyword','operator':'contains','value':'메모리 사용량 증가','evidence':'메모리 사용량 증가'}]),{}, {})
        self.assertNotIn('keyword',[c['field'] for c in p['filter_clauses']])
        self.assertIn('메모리 사용량 증가',p['terms'])
        q=validate_plan('같은 기준으로 KB증권 MTS만 비교해줘',plan(followup=True,scope='inherit',filters=[
            {'field':'customer','operator':'equals','value':'kb','evidence':'KB증권'},
            {'field':'platform','operator':'equals','value':'mts','evidence':'MTS'}]),{},p)
        self.assertEqual(q['resolved_constraints']['customer_key'],'kb')
        self.assertEqual(q['resolved_constraints']['platform_key'],'mts')
        self.assertIn('메모리 사용량 증가',q['terms'])
        self.assertNotIn('keyword',[c['field'] for c in q['filter_clauses']])

    def test_explicit_literal_exclusion_and_counts_keep_their_filters(self):
        for question,operation,field,op in [
            ('본문에 "메모리 사용량 증가"가 정확히 포함된 사례만 비교해줘','report','keyword','contains'),
            ('제목에 테스트가 없는 유사 사례를 비교해줘','report','title','not_contains'),
            ('메모리 사용량 증가가 포함된 이슈 몇 건?','count','keyword','contains')]:
            value='테스트' if field=='title' else '메모리 사용량 증가'
            result=validate_plan(question,plan(operation=operation,filters=[
                {'field':field,'operator':op,'value':value,'evidence':value}]),{}, {})
            self.assertIn({'field':field,'operator':op,'value':value},result['filter_clauses'])

    def test_applied_trial_and_rollback_decision_are_not_erased_by_later_qc(self):
        issue={'id':91,'summary':'CPU 부하 개선','notes':[
            {'id':10,'text':'개선 소스를 개발 서버에 적용 후 CPU 사용률을 확인해보았으나, 큰 차이를 느끼지 못하여 기존의 소스로 롤백하기로 결정하였습니다.'},
            {'id':11,'text':'재확인할 부분이 생겨 이슈를 부활시키겠습니다.'},
            {'id':12,'text':'[QC 완료] CPU 과부하 현상 없음을 확인'}]}
        text=similar._recorded_fix(issue)[0]+'\n'+'\n'.join(similar._history_lines(issue))
        for expected in ['적용 후','롤백하기로 결정','부활','노트 10','노트 11','QC']:
            self.assertIn(expected,text)
        self.assertNotIn('롤백 완료',text)
        facts=comparison_facts([issue])
        self.assertFalse(any(f['kind']=='unknown' and '실제 조치' in f['text'] for f in facts))

    def test_repository_delivery_is_preserved_without_promoting_to_production(self):
        issue={'id':92,'notes':[{'id':20,'text':'고객사 git repository를 통해 전달 완료'}]}
        text=similar._recorded_fix(issue)[0]
        self.assertIn('전달',text)
        self.assertIn('노트 20',text)
        self.assertNotIn('운영',text)
        pending={'notes':[{'id':21,'text':'고객사 git repository를 통해 전달할 예정'}]}
        self.assertEqual(similar._recorded_fix(pending),('',''))

    def test_throughput_parenthesis_is_not_the_cpu_cause(self):
        issue={'summary':'실시간 시세 CPU 부하 개선','notes':[{'id':30,'text':'단, 회선 지연으로 실제로는 초당 800~900건 처리)'}]}
        self.assertEqual(similar._recorded_cause(issue),('',''))

    def test_later_indicator_value_bug_does_not_replace_uncertain_cpu_cause(self):
        issue={'id':93,'summary':'HTS 버벅임 CPU 부하','notes':[
            {'id':40,'text':'원인\n계산 횟수가 많아서 CPU 사용이 올라간 것으로 보입니다.'},
            {'id':41,'text':'ADX 지표가 AS-IS랑 값이 다른 현상. Type 캐스팅 차이로 발생. CPU 부하 개선이 더 중요하기 때문에 롤백은 하지 않고 추가 작업 진행하기로 함.'}]}
        self.assertEqual(similar._recorded_cause(issue),('',''))
        self.assertIn('계산 횟수',similar._unconfirmed_cause(issue))
        # 모델이 같은 잘못된 분류를 내더라도 서버의 증상 귀속 검사를 거쳐야 한다.
        issue['comparison_facts']={'verified':True,'cause':[{'field':'note:41','text':'Type 캐스팅 차이로 발생'}]}
        self.assertEqual(similar._recorded_cause(issue),('',''))

    def test_legacy_soft_filter_is_removed_but_explicit_filter_survives_two_followups(self):
        old=validate_plan('#90 메모리 사례 비교해줘',plan(filters=[
            {'field':'issue_id','operator':'equals','value':'90','evidence':'#90'}]),{}, {})
        old['resolved_constraints']['filter_clauses'].append({'field':'keyword','operator':'contains','value':'메모리 사용량 증가'})
        q=validate_plan('같은 기준으로 MTS만 비교해줘',plan(followup=True,scope='inherit',filters=[
            {'field':'platform','operator':'equals','value':'mts','evidence':'MTS'}]),{},old)
        self.assertNotIn('keyword',[f['field'] for f in q['filter_clauses']])
        first=validate_plan('본문에 "메모리" 문자열이 포함된 유사 사례 비교',plan(filters=[
            {'field':'keyword','operator':'contains','value':'메모리','evidence':'메모리'}]),{}, {})
        second=validate_plan('같은 기준으로 MTS만 비교해줘',plan(followup=True,scope='inherit',filters=[
            {'field':'platform','operator':'equals','value':'mts','evidence':'MTS'}]),{},first)
        third=validate_plan('같은 조건으로 계속 비교해줘',plan(followup=True,scope='inherit'),{},second)
        self.assertIn({'field':'keyword','operator':'contains','value':'메모리'},third['filter_clauses'])

    def test_explicit_history_cannot_disappear_from_a_positive_summary_review(self):
        import json
        from unittest.mock import Mock,patch
        from backend.reports import comparison_narrative as mod
        issues=[{'id':80,'summary':'CPU 부하','notes':[
            {'id':1,'text':'소스를 개발 서버에 적용 후 부하를 확인하였으나 차이가 없어 롤백하기로 결정함.'}]},
            {'id':70,'notes':[{'id':2,'text':'캐시 해제 로직 수정 완료'}]}]
        facts=mod.comparison_facts(issues)
        action=next(f['id'] for f in facts if f['issue_id']==80 and f['kind']=='action')
        other=next(f['id'] for f in facts if f['issue_id']==70 and f['kind']=='action')
        draft={'claims':[{'text':'#80은 개발 서버에 소스를 적용한 기록이 있습니다.','fact_ids':[action]},
                         {'text':'#70은 캐시 해제 로직을 수정했습니다.','fact_ids':[other]}]}
        def response(value):return Mock(raise_for_status=lambda:None,json=lambda:{'choices':[{'message':{'content':json.dumps(value)}}]})
        with patch.object(mod,'post_model_chat',side_effect=[response(draft),response({'supported':[True,True]})]):
            text,meta=mod.summarize_comparison('원인과 조치를 비교해줘',issues,{'id':'test'},'key')
        self.assertEqual(text,'')
        self.assertEqual(meta['status'],'rejected')

    def test_empty_title_only_reports_do_not_spend_a_fact_review_call(self):
        from unittest.mock import patch
        from backend.reports import comparison_facts as mod
        issues=[{'id':1,'summary':'안드로이드 메모리 릭 개선','description':'안드로이드 메모리 릭 개선',
                 'notes':[{'id':1,'text':'고객사 git repository를 통해 전달 완료'}]},
                {'id':2,'summary':'차트 설정 메모리릭 수정','description':'차트 설정 메모리릭 수정'}]
        with patch.object(mod,'post_model_chat',side_effect=AssertionError('추가 분류할 기술 근거 없음')):
            result=mod.review_comparison_facts(issues,{'id':'test'},'key')
        self.assertEqual(result['status'],'not_needed')

    def test_rollback_decision_does_not_support_rollback_completion(self):
        from backend.reports.comparison_narrative import validate_claim
        row={'id':'h','issue_id':80,'kind':'history','text':'기존 소스로 롤백하기로 결정하였습니다.'}
        for text in ['#80은 롤백 후 재검토 중입니다.','#80은 롤백을 완료했습니다.']:
            self.assertFalse(validate_claim({'text':text,'fact_ids':['h']},{'h':row}))
        self.assertTrue(validate_claim({'text':'#80은 기존 소스로 롤백하기로 결정했습니다.','fact_ids':['h']},{'h':row}))

    def test_uncertainty_cannot_be_borrowed_from_a_different_issue(self):
        from backend.reports.comparison_narrative import validate_claim
        rows={'a':{'id':'a','issue_id':80,'kind':'uncertain_cause','text':'ADX 재계산으로 CPU가 올라간 것으로 보입니다.'},
              'b':{'id':'b','issue_id':70,'kind':'unknown','text':'실제 조치 기록이 확인되지 않습니다.'}}
        self.assertFalse(validate_claim({'text':'#80은 ADX 재계산으로 CPU가 올랐고 #70은 실제 조치가 확인되지 않습니다.','fact_ids':['a','b']},rows))

    def test_conjecture_word_is_not_a_confirmed_cause(self):
        from backend.reports.comparison_facts import review_comparison_facts
        from unittest.mock import patch
        issue={'id':1,'summary':'CPU 부하','notes':[{'id':3,'text':'전종목 반복문에서 CPU 부하가 발생할 것으로 추측됩니다.'}]}
        self.assertEqual(similar._recorded_cause(issue),('',''))
        self.assertIn('추측',similar._unconfirmed_cause(issue))
        # 이미 불확실성이 명시되고 실제 조치는 없는 원문을 확정 원인 찾기로 재해석하지 않는다.
        with patch('backend.reports.comparison_facts.post_model_chat',side_effect=AssertionError('추측만 있는 기록')):
            review_comparison_facts([issue],{'id':'test'},'key')

    def test_recorded_action_with_explicit_uncertainty_does_not_retry_a_cause_guess(self):
        from unittest.mock import patch
        from backend.reports import comparison_facts as mod
        issue={'id':4,'summary':'CPU 부하','notes':[
            {'id':1,'text':'반복 계산이 CPU 부하 원인으로 보입니다.'},
            {'id':2,'text':'개선 소스를 개발 서버에 적용 후 CPU 사용률을 확인했으나 차이가 없어 롤백하기로 결정함.'},
            {'id':3,'text':'CPU 100% 조건에서 부하가 발생하는 자료입니다.'}]}
        with patch.object(mod,'post_model_chat',side_effect=AssertionError('기록된 추정을 재추론하지 않음')):
            result=mod.review_comparison_facts([issue],{'id':'test'},'key')
        self.assertEqual(result['status'],'not_needed')

    def test_intended_rollback_is_not_reported_as_a_completed_history(self):
        from backend.reports.comparison_narrative import validate_claim
        row={'id':'h','issue_id':80,'kind':'history','text':'수정 버전에 문제가 있어 보류 및 롤백을 하려고함'}
        self.assertFalse(validate_claim({'text':'#80은 수정 버전 문제로 롤백 이력이 있습니다.','fact_ids':['h']},{'h':row}))
        self.assertTrue(validate_claim({'text':'#80은 수정 버전 문제로 롤백할 계획이 기록되어 있습니다.','fact_ids':['h']},{'h':row}))

    def test_plan_word_about_other_work_does_not_validate_completed_rollback(self):
        from backend.reports.comparison_narrative import validate_claim
        row={'id':'h','issue_id':80,'kind':'history','text':'롤백하기로 결정함. 이후 이슈를 부활시키도록 하겠습니다.'}
        self.assertFalse(validate_claim({'text':'#80은 롤백·재오픈 이력이 있으며, 다른 방법 모색을 계획 중입니다.','fact_ids':['h']},{'h':row}))
        self.assertTrue(validate_claim({'text':'#80은 롤백 결정과 재오픈 예고가 기록되어 있습니다.','fact_ids':['h']},{'h':row}))

    def test_dated_multiline_module_delivery_is_not_lost(self):
        detail={'notes':[{'id':9,'text':'2012.03.25 개발 담당 수정모듈 전달\n수정 모듈 전달'}]}
        action=similar._recorded_fix(detail)[0]
        self.assertIn('전달',action)
        self.assertIn('노트 9',action)

    def test_memory_focus_uses_the_recorded_symptom_even_when_title_is_generic(self):
        detail={'summary':'메모리 관련 검토 및 개선','description':'추가 메모리 점검 요청',
                'resolution_fields':{'사유 및 원인':'차트 이용 시 메모리 증가 현상에 대한 개선'}}
        self.assertTrue(similar._issue_resource_symptom(detail))
        unrelated={'summary':'차트 분석 도구 가로 연장선 개선','description':'교차점 좌표 조정',
                   'resolution_fields':{'조치 및 개선':'작도 범위를 조정하도록 처리함'}}
        self.assertFalse(similar._issue_resource_symptom(unrelated))

    def test_later_continuing_symptom_survives_prior_qc_and_delivery(self):
        detail={'notes':[{'id':1,'text':'메모리 해제 처리 수정 완료'},
                         {'id':2,'text':'[QC 완료]'}, {'id':3,'text':'[전달]'},
                         {'id':4,'text':'아직도 메모리가 많이 오르고 있는 상황이며 추가 확인 요청 수신'}]}
        caveat=similar._latest_resolution_caveat(detail)
        self.assertIn('아직도',caveat)
        self.assertIn('추가 확인',caveat)

    def test_old_cause_with_continued_allocations_is_not_a_post_fix_caveat(self):
        detail={'notes':[{'id':1,'text':'함수 호출시마다 new가 계속 발생함'},
                         {'id':2,'text':'delete pExistedLogicItem;\nm_strDeclaredInnerFuncInfo = _T("");'}]}
        self.assertEqual(similar._latest_resolution_caveat(detail),'')
