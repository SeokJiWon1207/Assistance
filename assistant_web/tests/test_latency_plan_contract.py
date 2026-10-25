"""빠른 첫 해석 지침은 기존 조건 검증·후속 상속을 대체하지 않는다."""
import json
from unittest import TestCase
from unittest.mock import patch

from backend.intent.unified import plan_question, validate_plan
from test_unified_planner import proposal


class LatencyPlanContractTests(TestCase):
    def test_model_reads_whole_question_and_unsupported_bound_still_retries(self):
        question='#30534와 비슷한 예전 CPU 부하 건들을 비교하고 원인과 실제 조치를 요약해줘'
        bad=proposal(source='mantis',operation='report',report_kind='similar',scope='target',
                     status={'mode':'unspecified','values':[],'evidence':''},terms=['CPU'],filters=[
                         {'field':'issue_id','operator':'lte','value':30534,'evidence':'#30534'}])
        good={**bad,'filters':[{'field':'issue_id','operator':'equals','value':30534,'evidence':'#30534'}]}
        with patch('backend.intent.unified._propose',side_effect=[bad,good]) as model:
            result=plan_question(question,{'id':'test'},'key',{'mantisUsername':'석지원'},None)
        self.assertEqual(model.call_count,2)
        data=json.loads(model.call_args_list[0].args[2]['messages'][1]['content'])
        self.assertEqual(data['question'],question)
        self.assertIn('lte/gte/between',data['filter_contract']['similar_base'])
        self.assertEqual(result['resolved_constraints']['issue_ids'],[30534])
        self.assertEqual(result['terms'],['CPU'])

    def test_unnumbered_followup_inherits_verified_base_and_only_adds_customer(self):
        first=validate_plan('#9000과 비슷한 CPU 과거 사례를 비교해줘',proposal(
            operation='report',report_kind='similar',scope='target',terms=['CPU'],
            status={'mode':'unspecified','values':[],'evidence':''},filters=[
                {'field':'issue_id','operator':'equals','value':9000,'evidence':'#9000'}]),{}, {})
        second=validate_plan('같은 기준과 CPU 주제는 유지하고 KB증권 사례로 좁혀줘',proposal(
            operation='report',report_kind='similar',scope='inherit',followup=True,terms=['CPU'],
            status={'mode':'inherit','values':[],'evidence':''},filters=[
                {'field':'customer','operator':'equals','value':'kb','evidence':'KB증권'}]),{},first)
        self.assertEqual(second['resolved_constraints']['issue_ids'],[9000])
        self.assertEqual(second['resolved_constraints']['customer_key'],'kb')
