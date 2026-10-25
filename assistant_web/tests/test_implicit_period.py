"""등록을 생략한 조회 기간은 누락·상속 오류를 검증하고 조건을 임의로 추가하지 않는다."""
import copy
from datetime import date
import unittest
from unittest.mock import patch

from backend.intent.unified_validation import validate_plan, validate_execution_contract
from test_unified_planner import proposal

USER = {'uid': 'implicit-period-test', 'mantisUsername': '석지원'}


class ImplicitPeriodTests(unittest.TestCase):
    def setUp(self):
        class FixedDate(date):
            @classmethod
            def today(cls):
                return cls(2026, 10, 23)
        patcher = patch('backend.intent.unified_validation.date', FixedDate)
        patcher.start()
        self.addCleanup(patcher.stop)

    def plan(self, **updates):
        return proposal(scope='team', status={'mode': 'open', 'values': [], 'evidence': '미종료'}, **updates)

    def validate(self, question, plan, previous=None):
        return validate_plan(question, plan, USER, previous or {})

    def period(self, start='2026-10-01', end='2026-10-31', evidence='이번 달'):
        return {'field': 'created_at', 'operator': 'between', 'value': [start, end], 'evidence': evidence}

    def previous(self):
        result = self.validate('이번 달 미종료 이슈 몇 건?', self.plan(filters=[self.period()]))
        result['audit_id'] = 'a' * 32
        return result

    def test_missing_period_is_rejected_for_count_list_and_grouping(self):
        for question, operation, group in [
            ('이번 달 우리 팀 미종료 이슈를 고객사별 건수로 정리해줘', 'aggregate', 'customer'),
            ('이번 주 미종료 이슈를 목록으로 보여줘', 'search', None),
            ('지난달 미종료 이슈는 몇 건?', 'count', None),
            ('최근 10일 미종료 이슈 수를 세어줘', 'count', None),
        ]:
            with self.subTest(question=question), self.assertRaisesRegex(ValueError, '등록 기간'):
                self.validate(question, self.plan(operation=operation, group_by=group))

    def test_valid_period_retains_status_scope_and_original_proposal(self):
        p = self.plan(filters=[self.period()]); original = copy.deepcopy(p)
        q = '이번 달 우리 팀 미종료 이슈 몇 건?'
        result = self.validate(q, p)
        self.assertEqual(result['resolved_constraints']['created_at'], {'from': '2026-10-01', 'to': '2026-10-31'})
        self.assertEqual(result['resolved_constraints']['statuses'], ['open'])
        self.assertEqual(result['resolved_constraints']['mantis_team_scope'], 'team')
        self.assertTrue(validate_execution_contract(q, result)['valid'])
        self.assertEqual(p, original)

    def test_followup_cannot_silently_keep_the_old_month(self):
        p = self.plan(followup=True, filters=[])
        p['status'] = {'mode': 'inherit', 'values': [], 'evidence': ''}
        with self.assertRaisesRegex(ValueError, '2026-09-01~2026-09-30'):
            self.validate('지난달 것으로 바꿔줘', p, self.previous())
        p['filters'] = [self.period('2026-09-01', '2026-09-30', '지난달')]
        result = self.validate('지난달 것으로 바꿔줘', p, self.previous())
        self.assertEqual(result['resolved_constraints']['statuses'], ['open'])
        self.assertEqual(result['resolved_constraints']['created_at']['from'], '2026-09-01')

    def test_customer_narrowing_keeps_period_and_date_clear_keeps_other_conditions(self):
        p = self.plan(followup=True, filters=[{'field': 'customer', 'operator': 'equals', 'value': '미래에셋', 'evidence': '미래에셋'}])
        p['status'] = {'mode': 'inherit', 'values': [], 'evidence': ''}
        current = self.validate('그중 미래에셋만 세어줘', p, self.previous())
        self.assertEqual(current['resolved_constraints']['created_at']['from'], '2026-10-01')
        p['filters'] = []; p['clear_fields'] = ['created_at']
        cleared = self.validate('이번 달 등록 기간 조건만 해제해줘', p, current)['resolved_constraints']
        self.assertIsNone(cleared['created_at'])
        self.assertEqual(cleared['statuses'], ['open'])
        self.assertEqual(cleared['customer_key'], '미래에셋')
        self.assertIsNone(self.validate('이번 달 조건만 빼줘', p, current)['resolved_constraints']['created_at'])

    def test_as_of_today_recent_order_and_period_free_queries_are_not_registration_ranges(self):
        for q in ['오늘 기준 우리 팀 미종료 이슈는 몇 건?', '이번 달 기준 미종료 이슈 현황 보여줘',
                  '현재 미종료 이슈 몇 건?', '우리 팀 미종료 이슈 중 최근 두 개만 보여줘',
                  '전일정산 관련 미종료 이슈 몇 건?',
                  '담당자가 전주희인 미종료 이슈 몇 건?',
                  '등록일과 관계없이 이번 달 기준 미종료 이슈 몇 건?']:
            with self.subTest(question=q):
                filters = ([{'field': 'owner', 'operator': 'equals', 'value': '전주희', 'evidence': '전주희'}]
                           if '전주희' in q else [])
                self.assertIsNone(self.validate(q, self.plan(filters=filters))['resolved_constraints']['created_at'])

    def test_literal_periods_in_title_or_body_do_not_become_registration_dates(self):
        for field, q, value in [
            ('title', '제목에 "이번 달"이 포함된 미종료 이슈 수', '이번 달'),
            ('keyword', '본문에 ‘지난주’가 포함된 미종료 이슈 수', '지난주'),
            ('title', '제목에 이번 주 문구가 들어간 미종료 이슈 몇 건?', '이번 주'),
        ]:
            p = self.plan(filters=[{'field': field, 'operator': 'contains', 'value': value, 'evidence': value}])
            with self.subTest(question=q):
                self.assertIsNone(self.validate(q, p)['resolved_constraints']['created_at'])

    def test_explanations_and_report_periods_keep_their_existing_contract(self):
        for operation, report in [('explain', None), ('report', 'postmortem')]:
            p = proposal(scope='target', operation=operation, report_kind=report,
                status={'mode': 'unspecified', 'values': [], 'evidence': ''},
                filters=[{'field': 'issue_id', 'operator': 'equals', 'value': 31668, 'evidence': '#31668'}])
            q = '#31668의 이번 달 고객 요청을 설명해줘' if operation == 'explain' else '#31668의 이번 달 고객 요청으로 사후 분석서를 작성해줘'
            self.assertIsNone(self.validate(q, p)['resolved_constraints']['created_at'])

    def test_qualified_day_range_is_not_replaced_by_whole_month(self):
        p = self.plan(filters=[self.period('2026-09-15', '2026-09-30', '지난달 15일 이후')])
        c = self.validate('지난달 15일 이후 미종료 이슈 몇 건?', p)['resolved_constraints']
        self.assertEqual(c['created_at']['from'], '2026-09-15')

    def test_missing_period_retries_once_and_failure_never_runs_an_unfiltered_query(self):
        import backend.intent.unified as unified
        bad = self.plan(); good = self.plan(filters=[self.period()]); q = '이번 달 미종료 이슈 몇 건?'
        with patch.object(unified, '_propose', side_effect=[bad, good]) as call:
            result = unified.plan_question(q, {'id': 'test'}, 'key', USER, {})
        self.assertEqual(call.call_count, 2)
        self.assertEqual(result['resolved_constraints']['created_at']['from'], '2026-10-01')
        with patch.object(unified, '_propose', return_value=bad):
            result = unified.plan_question(q, {'id': 'test'}, 'key', USER, {})
        self.assertEqual(result['operation'], 'clarify')
        self.assertNotIn('resolved_constraints', result)
