"""고객사 약칭과 정식 명칭 때문에 유효한 월간 조회가 재시도되지 않아야 한다."""
from copy import deepcopy
from datetime import date
import unittest
from unittest.mock import patch

from backend.intent import unified
from backend.intent.unified_validation import validate_execution_contract, validate_plan
from backend.query_filters import matches_source_filters
from test_unified_planner import proposal


class UnifiedCustomerAliasTests(unittest.TestCase):
    def setUp(self):
        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 28)
        self.enterContext(patch('backend.intent.unified_validation.date', FixedDate))
        self.user = {'mantisUsername': '석지원'}

    def customer_plan(self, value='NH투자증권', evidence='NH에서', operator='equals'):
        return proposal(operation='search', scope='team',
                        status={'mode': 'unspecified', 'values': [], 'evidence': ''}, filters=[
            {'field': 'customer', 'operator': operator, 'value': value, 'evidence': evidence},
            {'field': 'created_at', 'operator': 'between',
             'value': ['2026-10-01', '2026-10-31'], 'evidence': '이번달'},
        ])

    def test_monthly_nh_query_accepts_full_name_without_losing_date_on_retry(self):
        question = '이번달 NH에서 들어온 이슈보여줘'
        parsed = self.customer_plan()
        original = deepcopy(parsed)
        with patch.object(unified, '_propose', return_value=parsed) as propose:
            plan = unified.plan_question(question, {'id': 'test'}, 'key', self.user, {})
        self.assertEqual(plan['operation'], 'search')
        self.assertEqual(propose.call_count, 1)
        constraints = plan['resolved_constraints']
        self.assertEqual(constraints['customer_key'], 'NH')
        self.assertEqual(constraints['created_at'], {'from': '2026-10-01', 'to': '2026-10-31'})
        self.assertEqual(constraints['mantis_team_scope'], 'team')
        self.assertTrue(validate_execution_contract(question, plan)['valid'])
        self.assertEqual(parsed, original)
        rows = [
            {'id': 1, 'customer': '120NH', 'created_at': '2026-10-28'},
            {'id': 2, 'customer': '120NH', 'created_at': '2026-09-30'},
            {'id': 3, 'customer': '112KBStar(KB증권)', 'created_at': '2026-10-28'},
        ]
        self.assertEqual([row['id'] for row in rows if matches_source_filters(
            'mantis', row, constraints['filter_clauses'])], [1])

    def test_customer_name_variants_preserve_the_same_scope(self):
        for mention, value in [('NH', 'NH투자증권'), ('nh', 'NH투자증권'),
                               ('NH증권', 'NH투자증권'), ('NH투자증권', 'NH')]:
            with self.subTest(mention=mention, value=value):
                question = f'이번달 {mention}에서 들어온 이슈 보여줘'
                plan = validate_plan(question, self.customer_plan(value, mention), self.user, {})
                self.assertEqual(plan['resolved_constraints']['customer_key'], 'NH')
                self.assertEqual(plan['resolved_constraints']['created_at']['from'], '2026-10-01')

    def test_alias_expansion_does_not_allow_an_unmentioned_customer(self):
        for value in ['KB증권', 'NH투자증권미등록프로젝트']:
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '고객사'):
                validate_plan('이번달 NH에서 들어온 이슈보여줘',
                              self.customer_plan(value), self.user, {})

    def test_customer_exclusion_preserves_every_alias_and_operator(self):
        question = '이번달 NH와 DB를 제외한 이슈 보여줘'
        parsed = self.customer_plan(['NH투자증권', 'DB금융투자'], 'NH와 DB를 제외', 'not_in')
        plan = validate_plan(question, parsed, self.user, {})
        clauses = plan['resolved_constraints']['filter_clauses']
        for project, expected in [('120NH', False), ('101PopcornRenewal(동부증권)', False),
                                  ('112KBStar(KB증권)', True)]:
            with self.subTest(project=project):
                self.assertEqual(matches_source_filters('mantis', {
                    'customer': project, 'created_at': '2026-10-28'}, clauses), expected)

    def test_unregistered_exact_project_is_not_replaced_by_a_customer_alias(self):
        question = '이번달 900DRFN(공용)에서 들어온 이슈 보여줘'
        plan = validate_plan(question, self.customer_plan('900DRFN(공용)', '900DRFN(공용)'), self.user, {})
        customer = next(c for c in plan['filter_clauses'] if c['field'] == 'customer')
        self.assertEqual(customer['value'], '900DRFN(공용)')
        self.assertTrue(validate_execution_contract(question, plan)['valid'])
