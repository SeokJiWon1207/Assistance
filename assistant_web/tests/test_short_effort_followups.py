"""월·고객사만 바꾸는 질문의 확정 범위와 실제 공수 집계를 검증한다."""
from contextlib import nullcontext
import unittest
from unittest.mock import patch

from backend.intent.confirmed_context import confirmed_constraints_scope
from backend.intent.constraints import extract_query_constraints
from backend.intent.context import is_follow_up_question
from backend.intent.search_terms import build_keyword_terms
from backend.query_planning import build_query_plan
from backend.services import chat_service as chat
from backend.sources.mantis_collection import IssueBatch
import test_followup_topic_scope as fixtures

PREVIOUS = '2026년 9월 미래에셋증권 MTS 공수 집계'


def previous_plan():
    return {'source': 'mantis', 'operation': 'aggregate', 'grounding_text': PREVIOUS,
            'terms': [], 'resolved_constraints': extract_query_constraints(PREVIOUS)}


def row(number, customer, month, amount, platform='MTS', handler='이영희'):
    return {'id': number, 'summary': '차트 문의', 'project': {'name': customer},
            'created_at': f'2026-{month:02d}-05T09:00:00+09:00',
            'handler': {'real_name': handler},
            'custom_fields': [{'field': {'name': '매체구분'}, 'value': platform},
                              {'field': {'name': '1문의공수'}, 'value': f'이영희({amount})'}]}


class ShortEffortFollowupTests(unittest.TestCase):
    def context(self, question, plan=None, previous=PREVIOUS):
        return fixtures.FollowupTopicScopeTests.followup(self, question, plan or previous_plan(), previous)

    def test_customer_ellipsis_is_followup_but_independent_requests_are_not(self):
        for text in ('NH증권은?', 'NH증권은요?', '8월은?', '그럼 8월은?'):
            self.assertTrue(is_follow_up_question(text), text)
        for text in ('NH MTS 몇 건이야', 'NH증권 이슈 찾아줘', 'NH증권 소개해줘'):
            self.assertFalse(is_follow_up_question(text), text)

    def test_month_and_customer_sequence_retains_effort_and_remaining_filters(self):
        plan = previous_plan()
        previous = PREVIOUS
        cases = [('그럼 8월은?', '미래에셋증권', '2026-08-01'),
                 ('NH증권은?', 'NH증권', '2026-08-01'),
                 ('그럼 9월은?', 'NH증권', '2026-09-01')]
        for question, customer, start in cases:
            with self.subTest(question=question):
                context = self.context(question, plan, previous)
                constraints = context.query_constraints
                self.assertEqual(constraints['customer_key'], customer)
                self.assertEqual(constraints['platform_key'], 'mts')
                self.assertEqual(constraints['created_at']['from'], start)
                self.assertEqual(constraints['mantis_team_scope'], 'default')
                self.assertEqual(build_keyword_terms(context.retrieval_text, constraints), [])
                plan = build_query_plan(context.retrieval_text, {'source': 'mantis'}, constraints)
                self.assertEqual(plan['operation'], 'aggregate')
                plan['resolved_constraints'] = constraints
                previous = question

    def test_explicit_different_operation_does_not_inherit_effort(self):
        for question, operation in [('그럼 8월 이슈 목록 보여줘', 'search'), ('그럼 8월 몇 건이야', 'count')]:
            with self.subTest(question=question):
                context = self.context(question)
                plan = build_query_plan(context.retrieval_text, {'source': 'mantis'}, context.query_constraints)
                self.assertEqual(plan['operation'], operation)
                self.assertNotIn('followup_effort', context.query_constraints)

    def test_real_report_uses_only_customer_month_platform_and_team_matches(self):
        rows = [row(31367, '147MiraeAsset(미래에셋증권)', 8, '0.10'),
                row(31381, '120NH(NH증권)', 8, '0.20'), row(31398, '120NH(NH증권)', 8, '1.00'),
                row(31399, '120NH(NH증권)', 8, '0.20'), row(31430, '120NH(NH증권)', 8, '0.10'),
                row(31522, '120NH(NH증권)', 9, '0.10'), row(31559, '120NH(NH증권)', 9, '0.55'),
                row(31564, '120NH(NH증권)', 9, '2.50'), row(31566, '120NH(NH증권)', 9, '0.25'),
                row(32000, '120NH(NH증권)', 9, '99', 'HTS'),
                row(32001, '120NH(NH증권)', 9, '99', handler='강민성')]
        batch = IssueBatch(rows, {'search_complete': True, 'snapshot_complete': True, 'status': 'snapshot'})
        august = self.context('그럼 8월은?')
        august_plan = build_query_plan(august.retrieval_text, {'source': 'mantis'}, august.query_constraints)
        august_plan['resolved_constraints'] = august.query_constraints
        cases = [(august, 1, 0.10),
                 (self.context('NH증권은?', august_plan, '그럼 8월은?'), 4, 1.50),
                 (self.context('NH증권은?'), 4, 3.40)]
        for context, count, total in cases:
            with self.subTest(count=count, total=total), \
                 confirmed_constraints_scope(context.retrieval_text, context.query_constraints), \
                 patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(batch)):
                result = chat.build_deterministic_report(context.latest_user_text, context.retrieval_text,
                                                        {'mantis': True}, [], None).response
                self.assertIsNotNone(result)
                self.assertEqual(result['effortChart']['issueCount'], count)
                self.assertAlmostEqual(result['effortChart']['totalEffort'], total)
