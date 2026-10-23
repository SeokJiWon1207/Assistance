"""출력 형식 전환과 확정 상태 반복이 검색 조건을 훼손하지 않는다."""
import copy
import unittest

from backend.intent.unified_validation import validate_plan, validate_execution_contract
from backend.evaluation.verification import ungrounded_constraint_labels
from backend.config import MANTIS_OPEN_STATUSES
from test_unified_planner import proposal

USER = {'uid': 'followup-status-test', 'mantisUsername': '석지원'}


class FollowupStatusTableTests(unittest.TestCase):
    def previous(self):
        result = validate_plan('2026년 10월 등록된 미종료 이슈를 고객사별로 집계해줘', proposal(
            scope='team', operation='aggregate', group_by='customer',
            status={'mode': 'open', 'values': [], 'evidence': '미종료'}, filters=[
                {'field': 'created_at', 'operator': 'between',
                 'value': ['2026-10-01', '2026-10-31'], 'evidence': '2026년 10월'}]), USER, {})
        result['audit_id'] = 'a' * 32
        return result

    def followup(self, **updates):
        return proposal(scope='inherit', followup=True,
            status={'mode': 'inherit', 'values': [], 'evidence': ''},
            filters=[{'field': 'customer', 'operator': 'equals', 'value': '미래에셋', 'evidence': '미래에셋증권'}],
            clear_fields=['group_by'], **updates)

    def test_aggregate_to_count_can_clear_only_the_output_grouping(self):
        p = self.followup(); original = copy.deepcopy(p); previous = self.previous()
        q = '그중 미래에셋증권만 건수로 알려줘'
        result = validate_plan(q, p, USER, previous)
        c = result['resolved_constraints']
        self.assertEqual(result['operation'], 'count')
        self.assertIsNone(result['group_by'])
        self.assertEqual(c['customer_key'], '미래에셋')
        for key in ('statuses', 'excluded_statuses', 'created_at', 'mantis_team_members'):
            self.assertEqual(c[key], previous['resolved_constraints'][key])
        self.assertTrue(validate_execution_contract(q, result)['valid'])
        self.assertEqual(ungrounded_constraint_labels(q, c, 'mantis', query_plan=result), [])
        self.assertEqual(p, original)

    def test_repeated_open_plan_and_equivalent_filter_keep_inherited_conditions(self):
        for op, values in [('in', sorted(MANTIS_OPEN_STATUSES)), ('not_in', ['resolved', 'closed'])]:
            p = self.followup()
            p['status'] = {'mode': 'open', 'values': [], 'evidence': '미종료'}
            p['filters'].append({'field': 'status', 'operator': op, 'value': values, 'evidence': '미종료'})
            result = validate_plan('그중 미래에셋증권만 건수로 알려줘', p, USER, self.previous())
            self.assertEqual(result['resolved_constraints']['statuses'], ['open'])
            self.assertEqual(result['interpretation']['status']['mode'], 'inherit')

    def test_equivalent_duplicate_current_status_retains_current_evidence(self):
        p = proposal(scope='team', status={'mode': 'open', 'values': [], 'evidence': '미종료'},
                     filters=[{'field': 'status', 'operator': 'not_in',
                               'value': ['resolved', 'closed'], 'evidence': '미종료'}])
        result = validate_plan('우리 팀 미종료 이슈 몇 건?', p, USER, {})
        self.assertEqual(result['resolved_constraints']['statuses'], ['open'])
        self.assertEqual(result['interpretation']['status']['mode'], 'open')

    def test_conflicts_unknown_values_and_explicit_changes_are_not_inherited(self):
        for mode, values, echo, question in [
            ('open', [], ['closed'], '그중 미래에셋증권만 건수로 알려줘'),
            ('open', ['bogus'], sorted(MANTIS_OPEN_STATUSES), '그중 미래에셋증권만 건수로 알려줘'),
            ('open', [], ['bogus'], '그중 미래에셋증권만 건수로 알려줘'),
            ('open', [], sorted(MANTIS_OPEN_STATUSES), '그중 미래에셋증권 해결만 알려줘'),
        ]:
            p = self.followup(); p['clear_fields'] = []
            p['status'] = {'mode': mode, 'values': values, 'evidence': '미종료'}
            p['filters'].append({'field': 'status', 'operator': 'in', 'value': echo, 'evidence': '미종료'})
            with self.subTest(question=question, values=values, echo=echo), self.assertRaises(ValueError):
                validate_plan(question, p, USER, self.previous())

    def test_only_supported_presentation_transition_is_normalized(self):
        for edit in [lambda p:p.update(operation='aggregate'), lambda p:p.update(group_by='status'),
                     lambda p:p['clear_fields'].append('report_kind')]:
            p = self.followup(); edit(p)
            with self.subTest(p=p), self.assertRaises(ValueError):
                validate_plan('그중 미래에셋증권만 건수로 알려줘', p, USER, self.previous())

    def test_explicit_change_and_date_clear_still_apply(self):
        p = self.followup(); p['clear_fields'] = ['created_at', 'group_by']
        p['status'] = {'mode': 'include', 'values': ['resolved'], 'evidence': '해결 상태만'}
        c = validate_plan('등록 기간은 해제하고 미래에셋증권 해결 상태만 세어줘', p, USER,
                          self.previous())['resolved_constraints']
        self.assertEqual(c['statuses'], ['resolved'])
        self.assertIsNone(c['created_at'])
        self.assertEqual(c['customer_key'], '미래에셋')
