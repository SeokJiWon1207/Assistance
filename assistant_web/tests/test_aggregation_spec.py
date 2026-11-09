import copy
import unittest
from unittest.mock import patch

from backend.intent.unified_validation import validate_plan, validate_execution_contract


QUESTION = '전사 2026년 8월부터 9월까지 고객사별 월별 이슈 건수와 공수 추이를 전월 대비 분석해줘'


def specification(**changes):
    return {'group_by': ['customer', 'month'], 'metrics': ['issue_count', 'effort'],
            'period': {'from': '2026-08-01', 'to': '2026-09-30', 'evidence': '2026년 8월부터 9월까지'},
            'comparison': 'previous_month', 'top_n': 10, 'explain': True, **changes}


def proposal(**changes):
    return {'source': 'mantis', 'operation': 'aggregate', 'scope': 'all', 'followup': False,
            'filters': [], 'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
            'clear_fields': [], 'terms': [], 'group_by': None, 'limit': None,
            'aggregation': specification(), **changes}


class AggregationSpecTests(unittest.TestCase):
    def test_global_metrics_and_plain_count_keep_their_valid_contracts(self):
        spec = specification(group_by=[], metrics=['issue_count'], comparison=None, explain=False)
        question = '전사 2026년 8월부터 9월까지 이슈 등록 건수를 집계해줘'
        plan = validate_plan(question, proposal(aggregation=spec), {}, {})
        self.assertTrue(validate_execution_contract(question, plan)['valid'])
        question = '전사 이슈가 몇 건이야?'
        plan = validate_plan(question, proposal(operation='count', aggregation=None), {}, {})
        self.assertIsNone(plan['group_by'])
        self.assertTrue(validate_execution_contract(question, plan)['valid'])

    def test_execution_rejects_missing_or_malformed_aggregation_targets(self):
        valid = validate_plan(QUESTION, proposal(), {}, {})
        for aggregation, group in ((None, None), ({}, None), (None, 'screen'),
                                   (specification(), 'customer')):
            with self.subTest(aggregation=aggregation, group=group):
                plan = {**copy.deepcopy(valid), 'aggregation': aggregation, 'group_by': group}
                self.assertFalse(validate_execution_contract(QUESTION, plan)['valid'])

    def test_period_cannot_be_invented_from_a_customer_and_status_phrase(self):
        question = '전사 한투 미해결 이슈를 상태별로 몇 건인지 알려줘.'
        spec = specification(group_by=['status'], metrics=['issue_count'], comparison=None, explain=False,
                             period={'from': '2026-11-06', 'to': '2026-11-06', 'evidence': '전사 한투 미해결 이슈'})
        proposed = proposal(aggregation=spec,
            filters=[{'field': 'customer', 'operator': 'equals', 'value': '한투', 'evidence': '한투'}],
            status={'mode': 'open', 'values': [], 'evidence': '미해결'})
        with self.assertRaisesRegex(ValueError, '기간 근거'):
            validate_plan(question, proposed, {}, {})

    def test_explicit_group_summary_cannot_drop_explanation(self):
        question = '2026년 11월 2일 등록된 미종료 이슈를 고객사별로 요약해줘'
        spec = specification(group_by=['customer'], metrics=['issue_count'], comparison=None,
                             period={'from': '2026-11-02', 'to': '2026-11-02', 'evidence': '2026년 11월 2일'})
        status = {'mode': 'open', 'values': [], 'evidence': '미종료'}
        count = proposal(operation='count', aggregation=None, group_by='customer', status=status,
                         filters=[{'field': 'created_at', 'operator': 'between',
                                   'value': ['2026-11-02', '2026-11-02'], 'evidence': '2026년 11월 2일'}])
        with self.assertRaisesRegex(ValueError, '집계 설명'):
            validate_plan(question, count, {}, {})
        with self.assertRaisesRegex(ValueError, '집계 설명'):
            validate_plan(question, proposal(aggregation={**spec, 'explain': False}, status=status), {}, {})
        valid = validate_plan(question, proposal(aggregation=spec, status=status), {}, {})
        self.assertTrue(valid['aggregation']['explain'])
        from backend.intent import unified
        valid_proposal = proposal(aggregation=spec, status=status)
        calls = []
        def propose(model, key, payload):
            calls.append(copy.deepcopy(payload))
            return copy.deepcopy(count if len(calls) == 1 else valid_proposal)
        with patch.object(unified, '_propose', side_effect=propose), patch.object(
                unified.llm, 'get_model_registry', return_value=[]), patch.object(
                unified, 'get_customer_aliases', return_value={}), patch.object(
                unified, 'extract_people_constraints', return_value={'names': []}):
            repaired = unified.plan_question(question, {'id': 'fixture'}, 'test', {}, {})
        self.assertEqual(len(calls), 2)
        self.assertTrue(repaired['aggregation']['explain'])
        self.assertIn('집계 설명', calls[1]['messages'][-1]['content'])

    def test_plain_counts_table_only_and_quoted_summary_remain_unchanged(self):
        filters = [{'field': 'created_at', 'operator': 'between', 'value': ['2026-11-02', '2026-11-02'],
                    'evidence': '2026년 11월 2일'}]
        for suffix in ('등록 건수를 보여줘', '등록 건수를 표만 보여줘', '등록 건수만 설명 없이 보여줘'):
            question = '2026년 11월 2일 고객사별 ' + suffix
            planned = proposal(operation='count', aggregation=None, group_by='customer', filters=filters)
            self.assertEqual(validate_plan(question, planned, {}, {})['operation'], 'count')
        spec = specification(group_by=['customer'], metrics=['issue_count'], comparison=None, explain=False)
        self.assertFalse(validate_plan('2026년 8월부터 9월까지 제목에 "요약해줘"가 있는 이슈를 고객사별로 세어줘',
                                       proposal(aggregation=spec), {}, {})['aggregation']['explain'])
    def test_compound_spec_keeps_scalar_contract_and_fixed_dates(self):
        plan = validate_plan(QUESTION, proposal(), {}, {})
        self.assertEqual(plan.get('aggregation'), specification())
        self.assertIsNone(plan['group_by'])
        self.assertTrue(validate_execution_contract(QUESTION, plan)['valid'])

    def test_rejects_unknown_expressions_limits_duplicate_axes_and_date_basis(self):
        for spec in (specification(sql='SELECT 1'), specification(date_basis='updated_at'),
                     specification(metrics=['sum(cost)']), specification(top_n=0), specification(top_n=True),
                     specification(group_by=['customer', 'customer']), specification(group_by=['screen']),
                     specification(period={'from': '2026-09-01', 'to': '2026-08-31', 'evidence': '2026년'}),
                     specification(comparison='year_over_year')):
            with self.subTest(spec=spec), self.assertRaises(ValueError):
                validate_plan(QUESTION, proposal(aggregation=spec), {}, {})

    def test_period_cannot_be_ungrounded_or_silently_change_date_basis(self):
        with self.assertRaises(ValueError):
            validate_plan(QUESTION, proposal(aggregation=specification(period={
                'from': '2026-08-01', 'to': '2026-09-30', 'evidence': '지난해'})), {}, {})
        with self.assertRaisesRegex(ValueError, '날짜|등록일|전달일'):
            validate_plan('2026년 8월부터 9월까지 수정일 기준 월별 건수를 분석해줘', proposal(), {}, {})

    def test_followup_inherits_spec_and_filters_but_count_drops_output_shape(self):
        previous = validate_plan(QUESTION, proposal(), {}, {})
        previous['audit_id'] = 'owned-audit'
        p = proposal(followup=True, scope='inherit', aggregation={'top_n': 3}, filters=[
            {'field': 'platform', 'operator': 'equals', 'value': 'hts', 'evidence': 'HTS'}],
            status={'mode': 'inherit', 'values': [], 'evidence': ''})
        plan = validate_plan('그중 HTS만 상위 3개로 보여줘', p, {}, previous)
        self.assertEqual(plan['aggregation']['group_by'], ['customer', 'month'])
        self.assertEqual(plan['aggregation']['top_n'], 3)
        self.assertEqual(plan['resolved_constraints']['inherited_from_audit_id'], 'owned-audit')
        self.assertEqual(plan['resolved_constraints']['platform_key'], 'hts')
        p.pop('aggregation')
        p.update(operation='count', filters=[])
        count = validate_plan('그 조건에서 총 건수만 알려줘', p, {}, plan)
        self.assertNotIn('aggregation', count)
        self.assertEqual(count['resolved_constraints']['created_at'], {'from': '2026-08-01', 'to': '2026-09-30'})

    def test_self_effort_is_contributor_and_independent_question_clears_spec(self):
        q = '2026년 8월부터 9월까지 내 공수를 월별로 보여줘'
        plan = validate_plan(q, proposal(scope='self', aggregation=specification(
            group_by=['month'], metrics=['effort'], comparison=None, explain=False)), {'mantisUsername': '석지원'}, {})
        self.assertEqual(plan['resolved_constraints']['people']['role'], 'effort_person')
        p = proposal(operation='count', aggregation=None)
        result = validate_plan('전사 이슈 총 몇 건?', p, {}, plan)
        self.assertNotIn('aggregation', result)

    def test_execution_contract_rejects_tampered_aggregation(self):
        plan = validate_plan(QUESTION, proposal(), {}, {})
        changed = copy.deepcopy(plan)
        changed['aggregation'] = specification(metrics=['arbitrary'])
        self.assertFalse(validate_execution_contract(QUESTION, changed)['valid'])

    def test_followup_period_change_cannot_keep_old_period(self):
        previous = validate_plan(QUESTION, proposal(), {}, {})
        with self.assertRaises(ValueError):
            validate_plan('그중 지난달로 바꿔줘', proposal(followup=True, scope='inherit', aggregation={},
                status={'mode': 'inherit', 'values': [], 'evidence': ''}), {}, previous)

    def test_conflicting_scalar_group_and_ambiguous_self_metrics_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan(QUESTION, proposal(group_by='owner'), {}, {})
        with self.assertRaises(ValueError):
            validate_plan('2026년 8월부터 9월까지 내 건수와 공수', proposal(scope='self'), {'mantisUsername': '석지원'}, {})

    def test_explicit_explain_without_request_is_rejected(self):
        with self.assertRaises(ValueError):
            validate_plan('2026년 8월부터 9월까지 고객사별 건수만', proposal(), {}, {})

    def test_requested_wrong_metric_date_and_explanation_removal_are_not_ignored(self):
        for question, metric in [('2026년 8월부터 9월까지 전달일 기준 월별 건수', 'issue_count'),
                                 ('2026년 8월부터 9월까지 등록일 기준 월별 공수', 'effort')]:
            with self.subTest(question=question), self.assertRaisesRegex(ValueError, '날짜|등록일|전달일'):
                validate_plan(question, proposal(aggregation=specification(metrics=[metric], explain=False)), {}, {})
        previous = validate_plan(QUESTION, proposal(), {}, {})
        with self.assertRaises(ValueError):
            validate_plan('그 조건으로 설명 없이 표만 보여줘', proposal(followup=True, scope='inherit', aggregation={},
                status={'mode': 'inherit', 'values': [], 'evidence': ''}), {}, previous)

    def test_top_n_followup_cannot_drop_month_dimension(self):
        previous = validate_plan(QUESTION, proposal(aggregation=specification(comparison=None)), {}, {})
        p = proposal(followup=True, scope='inherit', aggregation={'group_by': ['customer'], 'top_n': 3},
                     filters=[{'field': 'platform', 'operator': 'equals', 'value': 'hts', 'evidence': 'HTS'}],
                     status={'mode': 'inherit', 'values': [], 'evidence': ''})
        with self.assertRaisesRegex(ValueError, '그룹|월'):
            validate_plan('그중 HTS만 상위 3개 고객사로 좁혀줘', p, {}, previous)
        accepted = validate_plan('그중 HTS만 월별 구분은 빼고 고객사로 합쳐줘', p, {}, previous)
        self.assertEqual(accepted['aggregation']['group_by'], ['customer'])
