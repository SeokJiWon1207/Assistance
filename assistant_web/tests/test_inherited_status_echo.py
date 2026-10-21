"""확정 상태의 반복은 상속으로 보존하고 새 값·모순은 계속 거부한다."""
import copy
import unittest

from backend.intent.unified_validation import validate_plan, validate_execution_contract
from backend.evaluation.verification import ungrounded_constraint_labels
from backend.config import MANTIS_OPEN_STATUSES
from test_unified_planner import proposal

USER = {'uid': 'status-echo-test', 'mantisUsername': '석지원'}


class InheritedStatusEchoTests(unittest.TestCase):
    def previous(self, mode='exclude', values=None):
        phrase={'open':'미종료','include':'확인과 개발완료'}.get(mode,'종료를 제외')
        q=f'전사에서 우동헌이 등록한 2025년 이슈 중 {phrase} 건수'
        p=validate_plan(q,proposal(scope='all',status={'mode':mode,'values':['closed'] if values is None else values,'evidence':phrase},filters=[
            {'field':'registered_reporter','operator':'equals','value':'우동헌','evidence':'우동헌이 등록한'},
            {'field':'created_at','operator':'between','value':['2025-01-01','2025-12-31'],'evidence':'2025년'},
        ]),USER,{})
        p['audit_id']='a'*32
        return p

    def followup(self, value=None, operator='not_in'):
        return proposal(operation='aggregate',scope='inherit',followup=True,group_by='status',
            status={'mode':'inherit','values':[],'evidence':'상태 조건'},clear_fields=['created_at'],filters=[
                {'field':'registered_reporter','operator':'equals','value':'우동헌','evidence':'같은 등록자'},
                {'field':'status','operator':operator,'value':value or ['closed'],'evidence':'상태 조건'},
            ])

    def test_clear_date_with_repeated_exclusion_preserves_registered_reporter_and_status(self):
        q='등록 기간만 해제하고 같은 등록자와 상태 조건으로 합쳐줘.'
        result=validate_plan(q,self.followup(),USER,self.previous())
        c=result['resolved_constraints']
        self.assertIsNone(c['created_at'])
        self.assertEqual(c['excluded_statuses'],['closed'])
        self.assertEqual(c['statuses'],[])
        self.assertEqual(c['people']['role'],'registered_reporter')
        self.assertEqual(c['people']['names'],['우동헌'])
        self.assertEqual([f for f in c['filter_clauses'] if f['field']=='status'],[
            {'field':'status','operator':'not_in','value':['closed']}])
        self.assertEqual(result['interpretation']['status']['mode'],'inherit')
        self.assertTrue(validate_execution_contract(q,result)['valid'])
        self.assertEqual(ungrounded_constraint_labels(q,c,'mantis',query_plan=result),[])

    def test_equivalent_include_and_open_exclusion_echo_keep_original_canonical_state(self):
        for original,values,operator,echo in [
            ('include',['confirmed','developed'],'in',['developed','confirmed']),
            ('open',[],'not_in',['resolved','closed']),
            ('open',[],'in',sorted(MANTIS_OPEN_STATUSES)),
            ('exclude',['closed'],'not_equals',['closed']),
        ]:
            with self.subTest(original=original,operator=operator):
                prev=self.previous(original,values)
                p=self.followup(echo,operator)
                result=validate_plan('등록 기간만 해제하고 같은 등록자와 상태 조건으로 합쳐줘.',p,USER,prev)
                c=result['resolved_constraints']
                self.assertEqual(c['statuses'],prev['resolved_constraints']['statuses'])
                self.assertEqual(c['excluded_statuses'],prev['resolved_constraints']['excluded_statuses'])

    def test_different_value_direction_unknown_state_and_clear_status_do_not_bypass_conflict(self):
        for change in [
            lambda p:p['filters'][-1].update(value=['closed','resolved']),
            lambda p:p['filters'][-1].update(operator='in'),
            lambda p:p['filters'][-1].update(value=['unknown']),
            lambda p:p['clear_fields'].append('status'),
            lambda p:p['status'].update(values=['closed']),
        ]:
            p=self.followup();change(p)
            with self.subTest(p=p),self.assertRaises(ValueError):
                validate_plan('등록 기간만 해제하고 같은 등록자와 상태 조건으로 합쳐줘.',p,USER,self.previous())

    def test_missing_previous_wrong_source_and_nonfollowup_still_fail(self):
        for prev,followup in [({},True),(dict(self.previous(),source='notion'),True),(self.previous(),False)]:
            p=self.followup();p['followup']=followup
            with self.subTest(previous=bool(prev),followup=followup),self.assertRaises(ValueError):
                validate_plan('등록 기간만 해제하고 같은 등록자와 상태 조건으로 합쳐줘.',p,USER,prev)

    def test_explicit_new_state_and_original_payload_are_preserved(self):
        p=self.followup(['resolved'],'in');p['status']={'mode':'include','values':['resolved'],'evidence':'해결만'}
        p['filters'][-1]['evidence']='해결만';original=copy.deepcopy(p)
        result=validate_plan('등록 기간만 해제하고 해결만 세어줘.',p,USER,self.previous())
        self.assertEqual(result['resolved_constraints']['statuses'],['resolved'])
        self.assertEqual(result['resolved_constraints']['excluded_statuses'],[])
        self.assertEqual(p,original)
