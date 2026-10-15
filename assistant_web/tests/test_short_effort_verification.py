"""실행 계획이 잘못되어도 직전 공수 요청의 누락을 통과시키지 않는다."""
from copy import deepcopy
import unittest

from backend.evaluation.delivery import decide_delivery
from backend.evaluation.verification import build_verification_report
from backend.intent.constraints import extract_query_constraints
from backend.query_planning import build_query_plan, validate_query_plan


class ShortEffortVerificationTests(unittest.TestCase):
    def plan(self):
        text = '2026년 8월 미래에셋증권 MTS 공수 집계'
        constraints = extract_query_constraints(text)
        # 실행 결과가 아닌 인증된 직전 턴에서 얻는 기대 의도.
        constraints['followup_effort'] = {'parent_audit_id': 'a' * 32,
            'parent_question': '2026년 9월 미래에셋증권 MTS 공수 집계', 'question': '그럼 8월은?'}
        plan = build_query_plan(text, {'source': 'mantis'}, constraints)
        plan['resolved_constraints'] = constraints
        return plan

    def test_valid_inherited_aggregate_plan_passes_operation_check(self):
        checks = validate_query_plan('그럼 8월은?', self.plan())['checks']
        self.assertEqual(next(c for c in checks if c['id'] == 'operation_intent')['status'], 'pass')

    def test_search_plan_cannot_pass_by_reinterpreting_the_short_question(self):
        plan = self.plan()
        plan['operation'] = 'search'
        checks = validate_query_plan('그럼 8월은?', plan)['checks']
        self.assertEqual(next(c for c in checks if c['id'] == 'operation_intent')['status'], 'fail')

    def test_missing_effort_execution_is_blocked_even_when_plan_says_aggregate(self):
        for operation in ('search', 'aggregate'):
            with self.subTest(operation=operation):
                plan = self.plan()
                plan['operation'] = operation
                report = build_verification_report('그럼 8월은?', '이슈 0건', [],
                    {'query_plan': plan, 'constraints': deepcopy(plan['resolved_constraints']),
                     'stages': [{'source': 'mantis_snapshot', 'status': 'ok',
                                 'meta': {'search_complete': True}}]}, 'deterministic_issue_list')
                self.assertEqual(report['status'], 'fail')
                self.assertFalse(decide_delivery(report).allowed)

    def test_new_list_request_does_not_keep_previous_effort_expectation(self):
        from test_short_effort_followups import ShortEffortFollowupTests
        context = ShortEffortFollowupTests().context('그럼 8월 이슈 목록 보여줘')
        plan = build_query_plan(context.retrieval_text, {'source': 'mantis'}, context.query_constraints)
        checks = validate_query_plan('그럼 8월 이슈 목록 보여줘', plan)['checks']
        self.assertEqual(next(c for c in checks if c['id'] == 'operation_intent')['status'], 'pass')

    def test_actual_effort_report_passes_independent_recalculation(self):
        from contextlib import nullcontext
        from unittest.mock import patch
        from backend.intent.confirmed_context import confirmed_constraints_scope
        from backend.services import chat_service as chat
        from backend.sources.mantis_collection import IssueBatch
        from test_short_effort_followups import ShortEffortFollowupTests, row
        context = ShortEffortFollowupTests().context('그럼 8월은?')
        batch = IssueBatch([row(31367, '147MiraeAsset(미래에셋증권)', 8, '0.10')],
                           {'search_complete': True, 'snapshot_complete': True, 'status': 'snapshot'})
        with confirmed_constraints_scope(context.retrieval_text, context.query_constraints), \
             patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(batch)):
            result = chat.build_deterministic_report(context.latest_user_text, context.retrieval_text,
                                                    {'mantis': True}, [], None).response
        report = build_verification_report(context.latest_user_text, result['content'], result['sources'],
                                           result['retrieval'], result['raw']['mode'])
        self.assertEqual(report['status'], 'pass', report['checks'])
        self.assertTrue(decide_delivery(report).allowed)
