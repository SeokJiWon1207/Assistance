"""긴 이슈의 결정 변경 이력과 제한된 근거 예산을 함께 보존한다."""
import unittest

from backend.sources.mantis_answer_evidence import build_answer_evidence


class DecisionHistoryEvidenceTests(unittest.TestCase):
    def test_long_issue_keeps_each_decision_change_and_last_four_notes(self):
        query = '핵심FID 수신값 계산식과 진행 내용을 알려줘'
        notes = [
            {'id': i, 'text': ('핵심FID 수신값 계산식 진행 내용 확인\n' * 70)}
            for i in range(1, 21)
        ]
        changes = {
            21: '프로젝트 SVN에서 이전 개발 내역 롤백 완료',
            22: '고객 요청으로 해당 기능 개발을 취소했습니다.',
            23: '운영 반영은 보류되었습니다.',
            24: '이번 배포에서 해당 기능을 제외했습니다.',
        }
        # 결정 변경이 긴 노트의 첫 구간 밖에 있어도 실제 문맥 구간을 보존해야 한다.
        notes.extend({'id': note_id, 'text': '배경자료\n' * 300 + text + '\n' + '참고자료\n' * 300}
                     for note_id, text in changes.items())
        notes.extend({'id': i, 'text': '핵심FID 수신값 계산식 진행 내용 확인\n' * 70}
                     for i in range(25, 41))
        notes.extend({'id': i, 'text': f'후속 점검 기록 {i}'} for i in range(41, 45))
        issue = {'id': 9, 'summary': '오랜 기간 검토한 기능', 'notes': notes}

        result = build_answer_evidence(issue, query)
        by_id = {note['id']: note['text'] for note in notes}
        excerpts = result['excerpts']
        provided_ids = {e.get('note_id') for e in excerpts}
        self.assertLessEqual(result['provided_chars'], 18000)
        self.assertFalse(result['complete'])
        self.assertTrue({41, 42, 43, 44}.issubset(provided_ids))
        for note_id, required in changes.items():
            with self.subTest(note_id=note_id):
                self.assertIn(required, ''.join(e['text'] for e in excerpts if e.get('note_id') == note_id))
        for excerpt in excerpts:
            if excerpt.get('note_id'):
                self.assertEqual(excerpt['text'], by_id[excerpt['note_id']][excerpt['start']:excerpt['end']])

    def test_many_decision_changes_cannot_displace_question_evidence_or_latest_notes(self):
        changes = [
            {'id': i, 'text': f'변경 기록 {i}: 운영 반영 보류 및 이전 배포 취소.\n' + '배경자료\n' * 180}
            for i in range(1, 41)
        ]
        required = 'GetStableValue = inputFID * 7;'
        notes = [*changes, {'id': 41, 'text': required},
                 *[{'id': i, 'text': f'후속 확인 기록 {i}'} for i in range(42, 46)]]
        result = build_answer_evidence({'id': 9, 'notes': notes}, 'GetStableValue inputFID 계산식')
        excerpts = result['excerpts']
        provided_ids = {e.get('note_id') for e in excerpts}

        self.assertLessEqual(result['provided_chars'], 18000)
        self.assertEqual(result['provided_chars'], sum(len(e['text']) for e in excerpts))
        self.assertFalse(result['complete'])
        self.assertIn(required, ''.join(e['text'] for e in excerpts if e.get('note_id') == 41))
        self.assertTrue({42, 43, 44, 45}.issubset(provided_ids))
        # 사건이 많아도 전부 확장하지 않으며 질문 근거와 최신 기록의 자리를 남긴다.
        self.assertLess(len(provided_ids & set(range(1, 41))), 40)


if __name__ == '__main__':
    unittest.main()
