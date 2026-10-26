"""운영 템플릿 실패의 날짜·보고서 계약과 불완전 집계 회귀."""
from datetime import date, timedelta
from unittest import TestCase
from unittest.mock import patch
from unittest.mock import Mock
import json

from backend.intent.unified_validation import validate_plan
from backend.evaluation.verification import build_verification_report
from backend.retrieval import collect_retrieved_issues
from backend.answers import build_issue_intro_line


def proposal(**kwargs):
    return {'source': 'mantis', 'operation': 'search', 'scope': 'team', 'followup': False,
            'filters': [], 'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
            'clear_fields': [], 'terms': [], 'group_by': None, 'limit': None, **kwargs}


class TemplatePlanTests(TestCase):
    def test_weekly_metrics_do_not_become_an_open_only_filter(self):
        q='지난주 NH 신규 이슈, 미종료 이슈, 장기 적체, 공수와 처리시간을 주간 보고서로 작성해줘'
        start = date.today() - timedelta(days=date.today().weekday() + 7)
        filters = [{'field':'created_at','operator':'between', 'value':[str(start),str(start + timedelta(days=6))], 'evidence':'지난주'}]
        plan=validate_plan(q,proposal(operation='report',report_kind='weekly',filters=filters),{}, {})
        self.assertEqual(plan['resolved_constraints']['statuses'],[])
        with self.assertRaises(ValueError):
            validate_plan(q,proposal(operation='report',report_kind='weekly',
                status={'mode':'open','values':[],'evidence':'미종료 이슈'}),{}, {})

    def test_report_followup_retains_kind_and_order(self):
        previous=validate_plan('NH 프로필',proposal(operation='report',report_kind='profile',
            sort={'field':'created_at','direction':'asc'}),{}, {})
        result=validate_plan('그대로 다시 보여줘',proposal(operation='report',followup=True,scope='inherit',
            status={'mode':'inherit','values':[],'evidence':''}),{},previous)
        self.assertEqual(result['report_kind'],'profile')
        self.assertEqual(result['sort']['direction'],'asc')

    def test_invalid_status_proposal_is_repaired_before_execution(self):
        from backend.intent.unified import plan_question
        good = proposal(operation='report', report_kind='monthly')
        bad = {**good, 'status': {'mode':'all','values':[],'evidence':''}}
        def response(p):
            return Mock(json=lambda:{'choices':[{'message':{'content':json.dumps(p)}}]})
        with patch('backend.intent.unified.post_model_chat', side_effect=[response(bad),response(good)]):
            result=plan_question('2026년 9월 NH 월간 고객사 리포트',{'id':'company'},'key',{}, {})
        self.assertEqual(result['operation'],'report')

    def test_calendar_last_month_rejects_rolling_window(self):
        p = proposal(filters=[{'field':'created_at','operator':'between',
                    'value':['2026-09-20','2026-10-19'],'evidence':'지난달'}])
        with patch('backend.intent.unified_validation.date') as today:
            today.today.return_value=date(2026,10,20)
            today.fromisoformat.side_effect=date.fromisoformat
            with self.assertRaisesRegex(ValueError,'2026-09-01'):
                validate_plan('지난달 이슈 보여줘',p,{}, {})

    def test_specialized_report_kinds_are_accepted(self):
        for kind,question in [('profile','NH 고객사 프로필'),('monthly','2026년 9월 NH 월간 고객사 리포트'),
                              ('triage','NH 장애 초동 대응'),('effort_sla','NH 공수와 평균 처리시간')]:
            with self.subTest(kind=kind):
                plan=validate_plan(question,proposal(operation='report',report_kind=kind),{}, {})
                self.assertEqual(plan['report_kind'],kind)

    def test_sort_is_available_to_search_and_formatting(self):
        p=validate_plan('오래된 순으로 보여줘',proposal(sort={'field':'created_at','direction':'asc'}),{}, {})
        self.assertEqual(p['resolved_constraints']['sort'],{'field':'created_at','direction':'asc'})

    def test_oldest_selection_happens_before_display_limit(self):
        retrieval={'constraints':{'sort':{'field':'created_at','direction':'asc'}},'stages':[
            {'source':'mantis_page_scan','status':'ok','issues_list':[
                {'id':9,'created_at':'2026-09-01'},{'id':3,'created_at':'2026-01-01'}]}]}
        self.assertEqual([r['id'] for r in collect_retrieved_issues(retrieval,1)],[3])
        self.assertIn('오래된',build_issue_intro_line(retrieval['constraints'],[],1))


class PartialCountTests(TestCase):
    def check(self,content,complete=False):
        retrieval={'constraints':{},'stages':[{'source':'mantis_count','status':'ok' if complete else 'partial_error',
            'meta':{'operation':'count','total_count':36,'applied_filters':{},'search_complete':complete}}]}
        report=build_verification_report('이슈 몇 건?',content,[],retrieval,'deterministic_mantis_count')
        return next(c['status'] for c in report['checks'] if c['id']=='count_result')

    def test_matching_partial_reference_is_a_warning(self):
        self.assertEqual(self.check('정확한 건수를 확정할 수 없습니다. 확인된 범위의 참고값은 36건이며 현재 전체 값과 다를 수 있습니다.'),'warning')

    def test_partial_must_not_claim_an_exact_total(self):
        self.assertEqual(self.check('총 36건입니다.'),'fail')

    def test_wrong_partial_and_wrong_complete_counts_still_fail(self):
        self.assertEqual(self.check('확인된 범위의 참고값은 35건이며 현재 전체 값과 다를 수 있습니다.'),'fail')
        self.assertEqual(self.check('총 35건입니다.',True),'fail')
        self.assertEqual(self.check('총 36건입니다.',True),'pass')

class ReportEvidenceScopeTests(TestCase):
    def test_triage_references_can_cross_customer_but_not_platform(self):
        c={'customer_key':'메리츠','customer_aliases':['메리츠'],'platform_key':'hts','platform_aliases':['HTS']}
        s={'type':'mantis','issue_id':1,'project':'120NH(NH증권)','media_type':'HTS',
           'status':'closed','detail':{'media_type':'HTS'}}
        retrieval={'constraints':c,'stages':[{'source':'chroma_incident_triage','status':'ok',
                    'meta':{'reference_issue_ids':[1]}}]}
        def status():
            v=build_verification_report('메리츠 HTS 장애 초동 대응','#1 참고 사례',[s],retrieval,'deterministic_incident_triage')
            return next(x['status'] for x in v['checks'] if x['id']=='constraints')
        self.assertEqual(status(),'pass')
        s['media_type']=s['detail']['media_type']='MTS'
        self.assertEqual(status(),'fail')

    def test_backlog_can_predate_report_but_cannot_change_customer(self):
        c={'customer_key':'NH','customer_aliases':['NH'],'created_at':{'from':'2026-09-01','to':'2026-09-07'},
           'filter_clauses':[{'field':'created_at','operator':'between','value':['2026-09-01','2026-09-07']}]}
        s={'type':'mantis','issue_id':1,'project':'120NH(NH증권)','status':'assigned','created_at':'2020-01-01','detail':{}}
        retrieval={'constraints':c,'stages':[{'source':'mantis_weekly_report','status':'ok',
                    'meta':{'backlog_ids':[1],'search_complete':True}}]}
        def status():
            v=build_verification_report('NH 2026년 9월 1일~7일 주간 보고서','#1 장기 미종료',[s],retrieval,'deterministic_weekly_report')
            return next(x['status'] for x in v['checks'] if x['id']=='constraints')
        self.assertEqual(status(),'pass')
        s['project']='108SK(SK증권)'
        self.assertEqual(status(),'fail')

    def test_triage_request_priority_is_not_a_reference_issue_attribute(self):
        from backend.reports.incident_triage import build_incident_triage_report
        q="높음 우선순위로 HTS 메리츠에서 발생한 '리소스 증가' 이슈의 영향 범위, 초동 확인 순서, 유사 사례와 담당자 전달 내용을 정리해줘"
        example={'id':31003,'project':'120NH(NH증권)','summary':'리소스 증가 확인',
                 'status':'closed','priority':'normal','media_type':'HTS','notes':[]}
        with patch('backend.reports.incident_triage.execute_chroma_search',
                   return_value={'status':'ok','documents':[{'latest_issue':example}]}):
            r=build_incident_triage_report(q)
        v=build_verification_report(q,r['content'],r['sources'],r['retrieval'],r['raw']['mode'])
        self.assertEqual(next(c['status'] for c in v['checks'] if c['id']=='issue_fields'),'pass')
