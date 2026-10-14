"""월간 고객사 보고 뒤 단일 월 공수 질문의 조건·보고서 선택 회귀."""
import unittest
from unittest.mock import patch

from backend.intent.confirmed_context import confirmed_constraints_scope, restore_confirmed_constraints
from backend.intent.constraints import extract_query_constraints
from backend.intent.search_terms import build_keyword_terms
from backend.reports.effort_trend import build_effort_trend_report_from_plan, wants_effort_trend
from backend.services import chat_service as chat
import test_followup_topic_scope as fixtures

FIRST = '2026년 10월 미래에셋증권 MTS 월간 고객사 리포트를 만들어줘'
FOLLOWUP = '그럼 9월 공수는 어떻게돼'


class MonthlyReportEffortFollowupTests(unittest.TestCase):
    def old_plan(self):
        return {'source': 'mantis', 'grounding_text': FIRST, 'terms': ['월간', '리포트', '만들어줘'],
                'resolved_constraints': extract_query_constraints(FIRST)}

    def test_report_command_does_not_become_search_terms(self):
        self.assertEqual(build_keyword_terms(FIRST, extract_query_constraints(FIRST)), [])

    def test_saved_report_plan_does_not_inherit_report_commands(self):
        restored = restore_confirmed_constraints(self.old_plan())
        self.assertEqual(restored['inherited_terms'], [])

    def test_effort_followup_does_not_pollute_the_next_month_question(self):
        text = '2026년 9월 미래에셋증권 MTS 그럼 9월 공수는 어떻게돼'
        plan = {'grounding_text': text, 'source': 'mantis', 'terms': ['공수', '어떻게돼'],
                'resolved_constraints': extract_query_constraints(text)}
        context = fixtures.FollowupTopicScopeTests.followup(self, '그럼 8월 공수는 어떻게돼',
                                                           plan=plan, previous=FOLLOWUP)
        self.assertEqual(context.query_constraints['inherited_terms'], [])
        self.assertEqual(context.query_constraints['created_at']['from'], '2026-08-01')

    def test_explicit_report_keyword_is_preserved(self):
        text = '2026년 10월 미래에셋 MTS 제목에 "월간 리포트" 포함된 이슈 고객사 리포트를 만들어줘'
        constraints = extract_query_constraints(text)
        plan = {'grounding_text': text, 'terms': ['월간 리포트', '만들어줘'],
                'resolved_constraints': constraints}
        self.assertEqual(restore_confirmed_constraints(plan)['inherited_terms'], ['월간 리포트'])

    def test_single_month_question_with_quoted_monthly_topic_is_not_trend(self):
        self.assertFalse(wants_effort_trend('2026년 9월 미래에셋 MTS 공수 키워드 "월간" 포함'))
        self.assertTrue(wants_effort_trend('미래에셋 MTS 월별 공수 추이'))

    def test_model_fallback_cannot_promote_a_quoted_topic_to_trend(self):
        with patch('backend.reports.effort_trend.run_effort_trend_report') as trend:
            result = build_effort_trend_report_from_plan(
                '2026년 9월 미래에셋 MTS 공수 키워드 "월간" 포함',
                {'source': 'mantis', 'effort': {'type': 'trend'}},
            )
        self.assertIsNone(result)
        trend.assert_not_called()

    def test_followup_dispatch_uses_september_effort_with_preserved_customer(self):
        context = fixtures.FollowupTopicScopeTests.followup(self, FOLLOWUP, plan=self.old_plan(), previous=FIRST)
        constraints = context.query_constraints
        self.assertEqual(constraints['customer_key'], '미래에셋증권')
        self.assertEqual(constraints['platform_key'], 'mts')
        self.assertEqual(constraints['created_at']['from'], '2026-09-01')
        self.assertEqual(constraints['created_at']['to'], '2026-09-30')
        self.assertEqual(constraints['inherited_terms'], [])
        self.assertFalse(any(c['field'] == 'keyword' for c in constraints['filter_clauses']))
        with confirmed_constraints_scope(context.retrieval_text, constraints), patch(
            'backend.reports.effort.run_filtered_effort_report',
            return_value={'content': '9월 공수', 'raw': {'mode': 'deterministic_effort_report'}},
        ) as aggregate:
            result = chat.build_deterministic_report(FOLLOWUP, context.retrieval_text, {'mantis': True}, [], None)
        self.assertEqual(result.response['raw']['mode'], 'deterministic_effort_report')
        args = aggregate.call_args.args
        self.assertEqual(args[3]['customer_key'], '미래에셋증권')
        self.assertEqual(args[3]['platform_key'], 'mts')
        self.assertEqual(args[4]['from'], '2026-09-01')
        self.assertEqual(args[4]['to'], '2026-09-30')

