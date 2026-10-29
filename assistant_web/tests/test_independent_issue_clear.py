"""단일 이슈 독립 조회의 무효한 이전 조건 해제만 정규화한다."""
from copy import deepcopy
import unittest

from backend.intent.unified_validation import validate_execution_contract, validate_plan
from test_unified_planner import proposal


QUESTION = '#31684 변경 이력이나 참고할 만한 내용을 더 자세히 알려줘'
USER = {'mantisUsername': '석지원'}
NO_STATUS = {'mode':'unspecified','values':[],'evidence':''}


class IndependentIssueClearTests(unittest.TestCase):
    def setUp(self):
        self.previous = validate_plan('2026년 10월 NH 이슈 보여줘', proposal(
            operation='search', scope='team', status=NO_STATUS, filters=[
                {'field':'customer', 'operator':'equals', 'value':'NH', 'evidence':'NH'},
                {'field':'created_at', 'operator':'between', 'value':['2026-10-01','2026-10-31'],
                 'evidence':'2026년 10월'},
            ]), USER, {})
        self.parsed = proposal(operation='get', scope='target', followup=False, status=NO_STATUS,
            filters=[{'field':'issue_id', 'operator':'equals', 'value':'31684', 'evidence':'#31684'}],
            clear_fields=['created_at','customer'])

    def test_independent_issue_does_not_inherit_or_clear_previous_filters(self):
        original = deepcopy(self.parsed)
        plan = validate_plan(QUESTION, self.parsed, USER, self.previous)
        c = plan['resolved_constraints']
        self.assertEqual(c['issue_ids'], [31684])
        self.assertFalse(c['customer_key'])
        self.assertFalse(c['created_at'])
        self.assertEqual(plan['interpretation']['clear_fields'], [])
        self.assertTrue(validate_execution_contract(QUESTION, plan)['valid'])
        self.assertEqual(self.parsed, original)

    def test_current_explicit_filters_remain_even_if_the_model_lists_them_as_cleared(self):
        question = '#31684 중 2026년 10월 NH 이슈를 자세히 보여줘'
        parsed = {**self.parsed, 'filters': [*self.parsed['filters'],
            *deepcopy(self.previous['interpretation']['evidence'])]}
        plan = validate_plan(question, parsed, USER, self.previous)
        c = plan['resolved_constraints']
        self.assertEqual(c['customer_key'], 'NH')
        self.assertEqual(c['created_at'], {'from':'2026-10-01','to':'2026-10-31'})
        self.assertEqual(c['issue_ids'], [31684])

    def test_scope_and_unknown_condition_clears_are_still_rejected(self):
        for clear in [['team'], ['owner'], ['unknown'], ['customer','team']]:
            with self.subTest(clear=clear), self.assertRaises(ValueError):
                validate_plan(QUESTION, {**self.parsed, 'clear_fields':clear}, USER, self.previous)

    def test_list_queries_and_missing_context_do_not_gain_condition_clear_permissions(self):
        for question,parsed,previous in [
            ('NH 이슈 보여줘', {**self.parsed, 'operation':'search'}, self.previous),
            (QUESTION,self.parsed,{}),
            ('그 이슈 자세히 알려줘',self.parsed,self.previous),
            ('그중 #31684 자세히 알려줘',self.parsed,self.previous),
        ]:
            with self.subTest(question=question, previous=bool(previous)), self.assertRaises(ValueError):
                validate_plan(question, parsed, USER, previous)

    def test_explicit_issue_id_must_still_be_present_and_grounded(self):
        for filters in [[], [{'field':'issue_id','operator':'equals','value':'31683','evidence':'#31684'}]]:
            with self.subTest(filters=filters), self.assertRaises(ValueError):
                validate_plan(QUESTION,{**self.parsed,'filters':filters},USER,self.previous)
