"""원문 노트의 기술 설명과 처리 단계가 해결 요약에서 왜곡되지 않는지 검증한다."""

import unittest
from unittest.mock import patch

from backend.reports import similar


class ResolutionEvidenceTest(unittest.TestCase):
    def test_administrative_closure_does_not_replace_a_recorded_technical_fix(self):
        admin="지난 완료 이슈를 일괄적으로 종료 처리함"
        self.assertEqual(similar._recorded_fix({'notes':[{'id':2,'text':admin}]}),('',''))
        fix,_=similar._recorded_fix({'notes':[{'id':1,'text':'타이머 삭제 로직 수정 완료.'},{'id':2,'text':admin}]})
        self.assertIn('타이머',fix)
        self.assertNotIn('일괄',fix)

    def test_requests_partial_outcomes_and_credentials_do_not_become_conclusions(self):
        for note in ('테스트 버전 개발이 금일 내로 완료가 가능하실까요?',
                     '수정해주신 내용 관련해서 운영에 적용했음에도 문제가 남아 있습니다.'):
            with self.subTest(note=note):
                self.assertEqual(similar._recorded_fix({'notes':[{'text':note}]}), ('',''))
        for note in ('현재 구조상 개선책을 찾기 어려운 상황입니다.',
                     '그럴 경우, 조건마다 확인하기 때문에 부하가 발생합니다.',
                     '이럴 때, 초기화 키가 다르기 때문에 설정을 불러오지 못합니다.',
                     '해당 화면을 원인으로 보고 있습니다.'):
            with self.subTest(note=note):
                self.assertEqual(similar._recorded_cause({'notes':[{'text':note}]}), ('',''))
        detail={'notes':[{'text':'ID: sample-user\nPASS: sample-secret\nCerpass: sample-cert\n직접 원인: 타이머 해제 누락으로 인해 증가합니다.'}]}
        cause,_=similar._recorded_cause(detail)
        self.assertIn('타이머',cause)
        self.assertNotIn('sample-',cause)

    def test_resource_shortage_uses_symptom_filter(self):
        self.assertTrue(similar._resource_symptom('차트 전환 후 리소스 부족 현상'))
        self.assertFalse(similar._resource_symptom('차트 시세 표시 변경 요청'))

    def test_work_section_survives_later_qc_and_delivery_notes(self):
        detail = {'notes': [
            {'id': 1, 'text': '- 현상 원인\nSetTimer 이후 KillTimer 누락\n\n- 작업 내역\n1. 그리드 초기화 시 타이머 삭제\n2. 재진입 타이머 삭제 시점 추가'},
            {'id': 2, 'text': '[QC 완료]'}, {'id': 3, 'text': '[전달완료]'},
        ]}
        action, _ = similar._recorded_fix(detail)
        self.assertIn('그리드 초기화', action)
        self.assertIn('재진입 타이머', action)
        self.assertIn('전달', action)
        self.assertEqual(similar._recorded_cause(detail)[0], 'SetTimer 이후 KillTimer 누락')

    def test_editing_instructions_and_deferral_are_not_symptom_causes(self):
        for note in ('시스템 부하 문제로 라는 문구는 삭제', '메리츠 소스 프리징으로 인해 보류 및 롤백'):
            with self.subTest(note=note):
                self.assertEqual(similar._recorded_cause({'notes': [{'text': note}]}), ('', ''))

    def test_resource_symptom_excludes_same_screen_with_different_problem(self):
        base = {'id': 100, 'summary': '[1768] 화면 유지 시 리소스가 급격하게 증가하는 현상'}
        unrelated = {'issue_id': 90, 'latest_issue': {'id': 90, 'summary': '[1768] 화면 상단 문구 수정 요청'}}
        relevant = {'issue_id': 80, 'latest_issue': {'id': 80, 'summary': '[9000] 사용자 개체가 계속 증가하는 현상'}}
        with patch.object(similar, 'extract_query_constraints', return_value={'issue_ids':[100]}), \
             patch.object(similar, 'fetch_mantis_issue_by_id', return_value=base), \
             patch.object(similar, 'execute_chroma_search', side_effect=[
                 {'status':'ok','documents':[unrelated]}, {'status':'ok','documents':[relevant]}]):
            report = similar.build_similar_resolution_report('#100과 비슷한 과거 이슈의 원인과 해결 내용을 비교해줘')
        self.assertIn('#80', report['content'])
        self.assertNotIn('#90', report['content'])
        self.assertEqual({s['issue_id'] for s in report['sources']}, {100,80})

    def test_parameter_contract_cause_preserves_identifier_and_meaning(self):
        detail = {"notes": [{"id": 92418, "text": (
            "COMUtil.substr(str, startIndex, endIndex) 함수 만들때\n"
            "파라미터명을 endIndex로 만들어놓고 실제로는 length로 구현해 놓았음.\n"
            "그래서 이 함수를 사용할 때 어디서는 endIndex로 입력했고, 어디서는 length로 입력했음"
        )}]}
        cause, origin = similar._recorded_cause(detail)
        self.assertEqual(origin, "note")
        for token in ("COMUtil.substr", "endIndex", "실제로는 length"):
            self.assertIn(token, cause)

    def test_confirmed_wrong_auxiliary_message_is_cause_without_cause_keyword(self):
        cause, _ = similar._recorded_cause({"notes": [{
            "text": "장전에만 셋팅되어야 하는 보조메시지가 셋팅되고 있는 것 확인함."
        }]})
        self.assertIn("장전에만", cause)
        self.assertIn("보조메시지", cause)

    def test_historical_data_limit_is_cause_without_claiming_a_fix(self):
        detail = {"notes": [{"text": (
            "현재 시스템 구조상 해당 과거 시점의 시장 변수 데이터를 소급하여 적용하는 데 기술적인 한계가 있어, "
            "과거 시점 기준의 그래프 데이터 제공은 불가한 것으로 판단되었습니다."
        )}]}
        cause, _ = similar._recorded_cause(detail)
        self.assertIn("시장 변수 데이터", cause)
        self.assertIn("제공은 불가", cause)
        self.assertEqual(similar._recorded_fix(detail), ("", ""))

    def test_svn_change_and_delivery_are_evidence_without_inventing_implementation(self):
        detail = {"notes": [
            {"id": 62383, "text": "svn URL: svn://example/repo\nsvn 변경내용 : drsvnloger://diff.75/example/repo\n리비전: 75\nU   ProjSrc/ScrDll/LogicEditCtrl.cpp"},
            {"id": 62384, "text": "[고객사 전달]\nftp://example/release.zip"},
        ]}
        action, _ = similar._recorded_fix(detail, max_chars=220)
        self.assertIn("LogicEditCtrl.cpp", action)
        self.assertIn("고객사 전달", action)
        self.assertNotIn("drsvnloger", action)
        self.assertNotIn("배포 완료", action)

    def test_qc_and_delivery_do_not_imply_production_deployment(self):
        detail = {"notes": [
            {"id": 22684, "text": "ftp://example/QC.xls\nQC 결과 정상적으로 작동하였습니다(OK)"},
            {"id": 22833, "text": "[고객사 전달]\nftp://example/release.zip"},
        ]}
        action, _ = similar._recorded_fix(detail, max_chars=220)
        self.assertIn("QC 결과 정상적으로", action)
        self.assertIn("고객사 전달", action)
        self.assertNotIn("배포", action)

    def test_latest_customer_resolution_wins_over_earlier_cancellation(self):
        detail = {"notes": [
            {"id": 107035, "text": "고객사 자체 수정으로 이슈 종료합니다."},
            {"id": 106532, "text": "고객사측 요청으로 해당 이슈 종료합니다."},
        ]}
        action, _ = similar._recorded_fix(detail)
        self.assertIn("고객사 자체 수정", action)
        self.assertNotIn("고객사측 요청", action)

    def test_technical_action_does_not_split_cpp_filename(self):
        action, _ = similar._recorded_fix({"notes": [{"text": "LogicEditCtrl.cpp에서 연산자 보존 로직을 수정 완료."}]})
        self.assertIn("LogicEditCtrl.cpp", action)

    def test_full_matching_note_beats_truncated_note_with_same_id(self):
        detail = {"notes": [{"id": 100, "text": "확인 결과 안내드립니다."}],
                  "matching_evidence": [{"field": "note:100", "text": "확인 결과 안내드립니다. 파라미터명을 endIndex로 만들어놓고 실제로는 length로 구현해 놓았음."}]}
        cause, _ = similar._recorded_cause(detail)
        self.assertIn("실제로는 length", cause)

    def test_unrecognized_notes_are_not_reported_as_nonexistent_records(self):
        content = "\n".join(similar._comparison_lines({"id": 1, "notes": [{"text": "콜스택과 파라미터를 검토하였습니다."}]}, []))
        self.assertNotIn("기록 없음", content)
        self.assertNotIn("기준 이슈 없음", content)

    def test_calculation_requirements_and_current_behavior_are_not_completed_actions(self):
        for note in (
            "위 산출식을 적용하여 과거 손익 그래프를 재현하기 위해서는 시장 변수 데이터의 이력이 필수적으로 요구됩니다.",
            "리턴해주는 값을 기초자산지수로 하여 기준가로 적용합니다.",
            "* 계산된 이론가격이 5,000 미만이면 5,000으로 적용",
            "위에서 계산된 만기손익에 종목 구분에 따른 승수를 적용함",
            "nStrike는 실제 값에 100,000을 곱한 정수값으로 전달됨",
            "5) 승수 적용",
            "문의 내용 추가 :",
            "화면 상단 그리드에서 사용자가 수정 가능한 항목입니다.",
            "서버 오류로 프로세스가 강제 종료되는 현상입니다.",
        ):
            with self.subTest(note=note):
                self.assertEqual(similar._recorded_fix({"notes": [{"text": note}]}), ("", ""))

    def test_answer_evidence_restores_unabridged_technical_cause(self):
        detail = {"notes": [{"id": 100, "text": "확인 결과 안내드립니다."}],
                  "answer_evidence": {"excerpts": [{"field": "note:100", "note_id": 100,
                      "text": "COMUtil.substr의 파라미터명을 endIndex로 만들어놓고 실제로는 length로 구현해 놓았음."}]}}
        cause, _ = similar._recorded_cause(detail)
        self.assertIn("COMUtil.substr", cause)
        self.assertIn("실제로는 length", cause)

    def test_latest_unresolved_or_scheduled_state_qualifies_earlier_completed_action(self):
        for latest in ("패치 배포 예정입니다", "수정 후에도 아직 해결하지 못했습니다"):
            detail = {"id": 7, "notes": [{"id": 1, "text": "코드 수정 완료."},
                                           {"id": 2, "text": latest}]}
            with self.subTest(latest=latest):
                self.assertIn(latest, similar._summarize_resolution(detail))
                self.assertIn(latest, "\n".join(similar._comparison_lines({}, [detail])))
                self.assertIn("코드 수정 완료", similar._summarize_resolution(detail))

    def test_negative_and_scheduled_notes_remain_unconfirmed(self):
        for note in ("배포 예정입니다.", "고객사 전달 예정", "QC 결과 실패", "원인은 파라미터 문제로 추정됩니다."):
            with self.subTest(note=note):
                self.assertEqual(similar._recorded_fix({"notes": [{"text": note}]}), ("", ""))
