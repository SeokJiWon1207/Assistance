"""고객사 공수 질문의 작업 분류와 원문 재합산 검증."""
import unittest

FOLLOWUP = "그럼 9월 공수는 어떻게돼"


class FilteredEffortVerificationTests(unittest.TestCase):
    def test_conversational_effort_question_is_an_aggregate(self):
        from backend.query_planning import infer_operation
        self.assertEqual(infer_operation(FOLLOWUP), 'aggregate')
        self.assertEqual(infer_operation('9월 공수는 어떻게 되나요'), 'aggregate')
        self.assertEqual(infer_operation('제목에 "공수는 어떻게돼" 포함된 이슈 찾아줘'), 'search')

    def test_customer_effort_is_recalculated_from_all_source_people(self):
        from backend.evaluation.verification import build_verification_report
        question = '미래에셋 MTS 9월 공수 합계'
        for total, expected in [(1.5, 'pass'), (9.0, 'fail')]:
            with self.subTest(total=total):
                report = build_verification_report(question, f'총 공수: **{total:.2f}**\n유형별 공수',
                    [{'issue_id': 12345, 'title': '테스트', 'detail': {'effort_fields': {
                        '1문의공수': '이영희(1.0), 석지원(0.5)'}}}],
                    {'stages': [{'source': 'mantis_effort_filtered', 'status': 'ok', 'meta': {
                        'total_effort': total, 'totals': {'1문의공수': total}, 'displayed_count': 1}}]},
                    'deterministic_filtered_effort_report')
                check = next((c for c in report['checks'] if c['id'] == 'effort_recalculation'), None)
                self.assertIsNotNone(check)
                self.assertEqual(check['status'], expected)
