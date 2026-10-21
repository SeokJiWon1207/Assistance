"""긴 노트의 경계와 분류 실패 시 이미 있는 원문 진단을 보존한다."""
from unittest import TestCase
from unittest.mock import patch

import requests

from backend.reports import similar
from backend.reports.comparison_facts import review_comparison_facts


class ComparisonEvidenceBoundaryTests(TestCase):
    def test_multisystem_note_keeps_confirmed_cause_and_actual_actions(self):
        issue={'id':71,'summary':'조건 반영 중 CPU 증가','notes':[{'id':81,'text':
            '현상 : 라이브러리 반영 중 CPU 증가\n'
            '원인 : 운영서버 : 동적 라이브러리 반영 중, 프로세스 재기동 전에 조건을 조회하여 CPU 증가됨.\n'
            '백업서버 : 원인 파악중 (다른 원인으로 유추됨)\n'
            '조치 :\n운영/백업 메모리 복구 후 고객사에 원인 설명\n'
            '로드 되지 않은 메모리가 있어. 로드 시킴\n추가 확인 예정'}]}
        cause=similar._recorded_cause(issue)[0]
        action=similar._recorded_fix(issue)[0]
        self.assertIn('프로세스 재기동',cause)
        self.assertNotIn('백업서버',cause)
        self.assertIn('백업서버',similar._unconfirmed_cause(issue))
        self.assertNotIn('운영서버',similar._unconfirmed_cause(issue))
        for value in ['메모리 복구','로드 시킴','노트 81']:
            self.assertIn(value,action)
        self.assertIn('로드 되지 않은 메모리',action)
        self.assertIn('백업서버', '\n'.join(similar._comparison_lines({'id':90},[issue])))
        self.assertNotEqual(action,'조치 [노트 81]')

    def test_bare_action_heading_is_not_a_completed_action(self):
        self.assertEqual(similar._recorded_fix({'notes':[{'text':'조치 :\n추가 확인 예정'}]}),('',''))

    def test_confirmed_field_does_not_resurrect_an_older_unscoped_guess(self):
        issue={'id':71,'resolution_fields':{'사유 및 원인':'타이머 해제 누락'},
               'notes':[{'id':7,'text':'외부 연결이 원인으로 추측됩니다.'}]}
        self.assertNotIn('외부 연결', '\n'.join(similar._comparison_lines({'id':90},[issue])))

    def test_unlabelled_progress_paragraph_does_not_promote_an_isolated_error_line(self):
        issue={'notes':[{'id':8,'text':'서버 개발자 재확인 할 예정\n=> 확인 결과, 클라이언트 오류로 확인됨\n수정 후 재 테스트 예정'}]}
        self.assertEqual(similar._recorded_cause(issue),('',''))

    def test_timeout_preserves_the_body_diagnosis_between_request_lines(self):
        issue={'id':72,'summary':'메모리릭 개선 요청','description':
            '증상의 원인 파악과 수정 요청드립니다.\n'
            '차트에서 public static Activity current; 로 Activity를 정적 변수로 참조하게 되어 나타나는 현상으로 파악됩니다.\n'
            '다른 방식으로 개선하거나 제거가 필요할 것 같습니다.',
            'notes':[{'id':82,'text':'malloc 이후 상태를 분석 중'}]}
        with patch('backend.reports.comparison_facts.post_model_chat',side_effect=requests.Timeout) as post:
            meta=review_comparison_facts([issue],{'id':'company'},'key')
        post.assert_called_once()
        self.assertEqual(meta['status'],'unavailable')
        cause,origin=similar._recorded_cause(issue)
        self.assertIn('정적 변수로 참조',cause)
        self.assertEqual(origin,'description')
        self.assertEqual(similar._recorded_fix(issue),('',''))

    def test_tentative_body_mechanism_is_not_promoted_by_a_request(self):
        issue={'description':'원인 확인 요청\n정적 변수로 참조하여 나타나는 현상으로 추측됩니다.\n제거 필요'}
        self.assertEqual(similar._recorded_cause(issue),('',''))
