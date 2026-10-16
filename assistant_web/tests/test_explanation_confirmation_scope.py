"""확인 요청·실제 결과와 사건 시점이 핵심 결론에서 뒤바뀌지 않아야 한다."""
import unittest

from backend.evaluation.explanation_quotes import render_quote_answer, validate_quote_answer
from backend.services.grounded_explanation import timeline_evidence


class ExplanationConfirmationScopeTests(unittest.TestCase):
    def conclusion(self, *texts):
        grounding = {'issues': [{'id': 101, 'answer_evidence': {'complete': True, 'excerpts': [
            {'field': f'note:{index}', 'note_id': index, 'created_at': f'2026-09-{index:02}',
             'text': text} for index, text in enumerate(texts, 1)
        ]}}]}
        payload = {'selected_issue_ids': [101], 'sections': [{
            'topic': 'timeline', 'unanswered': False, 'evidence': timeline_evidence(grounding, [101]),
        }]}
        return render_quote_answer(validate_quote_answer(payload, grounding), grounding).split('### 핵심 근거')[0]

    def test_normal_confirmation_request_is_not_reported_as_successful_test(self):
        for text in (
            '정확한 적용 범위와 정상 동작 여부를 확인할 수 있도록, 기대되는 동작 및 확인 방법을 설명 부탁드립니다.',
            '수정 후 정상 동작 확인이 필요합니다.',
            '정상 동작 확인 예정입니다.',
            '정상 동작 확인 결과는 아직 없습니다.',
            '정상 동작을 확인하지 못했습니다.',
            '정상 동작 확인했으면 알려주세요.',
            '정상 동작 확인했다고 보기 어렵습니다.',
            '정상 동작 확인했다고 한 설명은 잘못되었습니다.',
        ):
            with self.subTest(text=text):
                conclusion = self.conclusion('[QC 요청]', text)
                self.assertNotIn('테스트에서는 정상 표시를 확인했습니다', conclusion)
                self.assertNotIn('정상 확인 결과:', conclusion)

    def test_confirmed_build_and_behavior_does_not_become_display_confirmation(self):
        conclusion = self.conclusion(
            '소스 병합 후 정상 빌드 및 동작 확인 되었고 유지보수 수정 부분도 정상 동작 확인했다고 함.')
        self.assertIn('정상 빌드 및 동작', conclusion)
        self.assertIn('유지보수 수정 부분', conclusion)
        self.assertNotIn('정상 표시를 확인했습니다', conclusion)

    def test_questions_and_indirect_questions_are_not_completed_confirmations(self):
        for text in ('수정본에서 정상 동작 확인했나요?',
                     '정상 동작 확인되었는지 알려주세요.',
                     '정상 동작 확인했는지 미확인입니다.'):
            with self.subTest(text=text):
                self.assertNotIn('정상 확인 결과:', self.conclusion('[QC 요청]', text))

    def test_nonreproduction_condition_or_question_is_not_an_observed_result(self):
        for text in ('동일 현상이 재현되지 않으면 종료할 예정입니다.',
                     '현상이 재현되지 않았나요?',
                     '현상이 재현되지 않았는지는 미확인입니다.'):
            with self.subTest(text=text):
                self.assertNotIn('재현 확인 기록:', self.conclusion('[QC 요청]', text))

    def test_declarative_confirmation_and_reported_result_remain_available(self):
        for text in ('정상 동작 확인했습니다.', '정상 동작 확인되었음.',
                     '정상 동작 확인했다고 함.', '정상 동작 확인 완료.'):
            with self.subTest(text=text):
                self.assertIn('정상 확인 결과:', self.conclusion(text))
        for text in ('현상이 재현되지 않았습니다.', '현상이 재현되지 않음.',
                     '현상이 재현되지 않는다고 하였습니다.'):
            with self.subTest(text=text):
                self.assertIn('재현 확인 기록:', self.conclusion(text))

    def test_post_delivery_reproduction_result_keeps_its_subject_and_time(self):
        conclusion = self.conclusion(
            '종목코드 구분자 수정 후 [QC 완료]', '[고객사 전달]',
            '전달 파일로 빌드 시 차트 종료 현상이 발생했습니다.\n'
            '차트 종료 현상은 재현 시도했으나 재현되지 않는다고 하였습니다.')
        self.assertNotIn('초기 확인', conclusion)
        self.assertNotIn('같은 현상', conclusion)
        self.assertIn('차트 종료 현상', conclusion)
        self.assertIn('재현되지 않는다고', conclusion)
        self.assertLess(conclusion.index('고객사 전달'), conclusion.index('차트 종료 현상'))

    def test_request_in_another_sentence_does_not_erase_confirmed_observation(self):
        conclusion = self.conclusion(
            'QC 담당자와 예상가 정상 표시되는 것 확인하였습니다.\n추가 검증은 고객 확인 부탁드립니다.')
        self.assertIn('예상가 정상 표시되는 것 확인하였습니다', conclusion)


if __name__ == '__main__':
    unittest.main()
