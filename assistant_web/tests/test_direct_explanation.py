"""질문이 묻는 단계에 직접 답하고 검증된 원문으로만 결론을 뒷받침한다."""
import unittest

from backend.evaluation.explanation_quotes import render_quote_answer, validate_quote_answer
from backend.services.grounded_explanation import timeline_evidence


class DirectExplanationTests(unittest.TestCase):
    def answer(self, question, notes, *, cause='', action='', complete=True, previous_questions=None):
        excerpts = [{'field': f'note:{i}', 'note_id': i, 'created_at': f'2026-10-{i:02}T12:00:00',
                     'text': text} for i, text in enumerate(notes, 1)]
        excerpts += [{'field': field, 'text': text} for field, text in
                     [('사유 및 원인', cause), ('조치 및 개선', action)] if text]
        grounding = {'issues': [{'id':101,'answer_evidence':{'complete':complete,'excerpts':excerpts}},
                     {'id':102,'answer_evidence':{'complete':True,'excerpts':[
                         {'field':'note:999','note_id':999,'text':'고객 검증 완료했습니다.'}]}}]}
        sections = [{'topic':'timeline','unanswered':False,'evidence':timeline_evidence(grounding,[101])}]
        for topic, field, text in [('cause','사유 및 원인',cause),('action','조치 및 개선',action)]:
            if text:sections.append({'topic':topic,'unanswered':False,'evidence':[
                {'issue_id':101,'field':field,'quote':text}]})
        v=validate_quote_answer({'selected_issue_ids':[101],'sections':sections},grounding)
        self.assertTrue(v['valid'],v['errors'])
        return render_quote_answer(v,grounding,question=question,previous_questions=previous_questions)

    def core(self,*args,**kwargs):
        return self.answer(*args,**kwargs).split('### 핵심 근거')[0]

    def test_customer_validation_question_is_not_answered_with_production_deployment(self):
        core=self.core('고객 확인이 끝났어?', ['관련 수정 내역입니다. 확인 부탁드립니다.'])
        self.assertIn('고객 검증 완료를 확인할 수 없습니다',core)
        self.assertNotIn('운영 반영',core)
        self.assertNotIn('노트 999',core)

    def test_qc_delivery_and_customer_validation_are_separate_answers(self):
        core=self.core('내부 QC와 고객사 전달은 끝났어? 고객 검증은?',
                       ['[QC 완료]','[고객사 전달]'])
        self.assertIn('내부 QC 완료가 확인됩니다',core)
        self.assertIn('고객사 전달 완료가 확인됩니다',core)
        self.assertIn('고객 검증 완료를 확인할 수 없습니다',core)
        self.assertIn('노트 1',core)
        self.assertIn('노트 2',core)

    def test_customer_confirmed_result_is_linked_to_its_note(self):
        core=self.core('고객 테스트가 완료됐니?', ['수정본 전달했습니다.','고객사에서 정상 동작을 확인했습니다.'])
        self.assertIn('고객 검증 완료가 확인됩니다',core)
        self.assertIn('노트 2',core)
        self.assertNotIn('운영 반영',core)

    def test_request_future_question_conditional_and_negation_are_not_completion(self):
        for text in ['고객 검증 완료 후 전달할 예정입니다.','고객 검증 완료 여부 확인 부탁드립니다.',
                     '고객 검증 완료됐나요?','고객 검증을 완료하지 못했습니다.',
                     '고객 검증 완료라는 안내는 취소합니다.','고객 확인이 끝나면 종료합니다.']:
            with self.subTest(text=text):
                core=self.core('고객 검증까지 완료됐어?', [text])
                self.assertNotIn('고객 검증 완료가 확인됩니다',core)

    def test_later_qc_ng_supersedes_earlier_completion(self):
        core=self.core('QC는 최종적으로 통과한 거야?', ['[QC 완료]','[QC NG] 추가 오류로 재검수 요청'])
        self.assertIn('내부 QC는 실패·재확인 기록이 있습니다',core)
        self.assertNotIn('내부 QC 완료가 확인됩니다',core)
        self.assertIn('노트 2',core)

    def test_pending_qc_does_not_hide_actual_delivery_in_another_sentence(self):
        core=self.core('QC와 고객 전달은 각각 어디까지야?',
                       ['[QC 완료]\n고객 확인 부탁드립니다.','[고객사 전달]'])
        self.assertIn('내부 QC 완료가 확인됩니다',core)
        self.assertIn('고객사 전달 완료가 확인됩니다',core)

    def test_cause_and_action_come_first_and_do_not_require_long_complete_sentences(self):
        core=self.core('원인과 조치를 알려줘. 고객 검증도 끝났어?',
                       ['수정본 확인 부탁드립니다.'],cause='잘못된 인덱스 참조',action='범위 검사 추가')
        self.assertIn('**원인**: 잘못된 인덱스 참조',core)
        self.assertIn('**조치**: 범위 검사 추가',core)
        self.assertIn('필드 사유 및 원인',core)
        self.assertIn('필드 조치 및 개선',core)
        self.assertLess(core.index('**원인**'),core.index('고객 검증'))

    def test_partial_evidence_does_not_assert_absence_in_full_history(self):
        core=self.core('운영에 반영 완료됐어?', ['QC 요청'],complete=False)
        self.assertIn('운영 반영 완료를 확인할 수 없습니다',core)
        self.assertNotIn('전체 이력에 없습니다',core)

    def test_development_and_production_are_not_inferred_from_ticket_closure(self):
        core=self.core('개발과 운영 적용도 끝난 거지?', ['공수 산정 결과를 전달한 뒤 이슈 종료.'])
        self.assertIn('개발·수정 완료를 확인할 수 없습니다',core)
        self.assertIn('운영 반영 완료를 확인할 수 없습니다',core)
        self.assertNotIn('완료가 확인됩니다',core)

    def test_explicit_production_deployment_is_not_customer_validation(self):
        core=self.core('고객 검증과 운영 반영이 각각 끝났어?', ['운영 서버 반영 완료했습니다.'])
        self.assertIn('운영 반영 완료가 확인됩니다',core)
        self.assertIn('고객 검증 완료를 확인할 수 없습니다',core)

    def test_cause_only_answer_does_not_add_unrequested_completion_claims(self):
        core=self.core('오류의 원인이 뭐였지?', ['확인 부탁드립니다.'],cause='버퍼 크기 계산 오류')
        self.assertIn('버퍼 크기 계산 오류',core)
        self.assertNotIn('운영 반영',core)
        self.assertNotIn('고객 검증',core)

    def test_qc_completion_does_not_prove_delivery_in_a_later_step(self):
        core=self.core('고객사 전달도 완료됐어?', ['QC 완료 후 고객사 전달'])
        self.assertNotIn('고객사 전달 완료가 확인됩니다',core)

    def test_customer_qc_is_not_internal_qc(self):
        core=self.core('내부 QC가 끝났어?', ['고객사 QC 완료했습니다.'])
        self.assertNotIn('내부 QC 완료가 확인됩니다',core)

    def test_speculative_customer_confirmation_is_not_a_completed_result(self):
        core=self.core('고객 검증까지 완료됐어?', ['고객 검증 완료된 것으로 추정됩니다.'])
        self.assertNotIn('고객 검증 완료가 확인됩니다',core)

    def test_elliptical_followup_keeps_customer_validation_subject(self):
        core=self.core('그럼 완료로 보고해도 돼?', ['[QC 완료]','수정본 확인 부탁드립니다.'],
                       previous_questions=['고객 검증까지 끝났는지 알려줘.'])
        self.assertIn('고객 검증 완료를 확인할 수 없습니다',core)
        self.assertNotIn('운영 반영',core)

    def test_explicit_new_stage_does_not_inherit_previous_customer_stage(self):
        core=self.core('이번에는 QC만 완료됐는지 알려줘.', ['[QC 완료]'],
                       previous_questions=['고객 검증도 끝났어?'])
        self.assertIn('내부 QC 완료가 확인됩니다',core)
        self.assertNotIn('고객 검증',core)

    def test_unrequested_production_caveat_is_removed_from_technical_answer(self):
        core=self.core('이 함수의 입력값은 뭐야?', ['함수 인자는 날짜와 시장 구분값입니다.'])
        self.assertNotIn('운영 반영 완료',core)

    def test_storage_path_after_qc_marker_does_not_replace_completion(self):
        core=self.core('QC와 고객 전달은 완료된 거야?',
                       ['[QC 완료]\nZ:\\02_QC요청\\2026\\package','[고객사 전달]'])
        self.assertIn('내부 QC 완료가 확인됩니다',core)
        self.assertNotIn('확인된 기록: “Z:',core)

    def test_later_descriptive_qc_line_does_not_overwrite_actual_result(self):
        core=self.core('검수는 완료됐어?', ['[QC 완료]\nQC 파일 위치 안내'])
        self.assertIn('내부 QC 완료가 확인됩니다',core)

    def test_customer_completion_predicate_does_not_attach_to_action_question(self):
        core=self.core('원인과 수정한 내용이 뭐야? 고객 정상 확인까지 끝났어?',
                       ['관련 수정 내역입니다.'],cause='인덱스 오류',action='방어 로직 추가')
        self.assertIn('고객 검증 완료를 확인할 수 없습니다',core)
        self.assertNotIn('**개발·수정**',core)

    def test_workflow_decision_is_shown_alongside_unconfirmed_development(self):
        core=self.core('검토 결과가 어떻게 정리됐어? 실제 구현도 완료됐어?',
                       ['검토 결과를 전달했습니다.'],action='구현 대신 문서 보완으로 합의하고 이슈 종료')
        self.assertIn('**조치**: 구현 대신 문서 보완으로 합의하고 이슈 종료',core)
        self.assertIn('개발·수정 완료를 확인할 수 없습니다',core)

    def test_same_issue_heading_does_not_hide_action_body(self):
        from backend.evaluation.explanation_quotes import validate_quote_answer,render_quote_answer
        text='101: 대상 문의\n설정 파일의 예외 옵션을 삭제 처리했습니다.'
        g={'issues':[{'id':101,'answer_evidence':{'excerpts':[
            {'field':'note:1','note_id':1,'text':text},
            {'field':'조치 및 개선','text':'조치 내용 안내함'}]}}]}
        v=validate_quote_answer({'selected_issue_ids':[101],'sections':[{'topic':'action','unanswered':False,
            'evidence':[{'issue_id':101,'field':'조치 및 개선','quote':'조치 내용 안내함'},
                        {'issue_id':101,'field':'note:1','quote':text}]}]},g)
        core=render_quote_answer(v,g,question='어떤 조치를 했어?').split('### 핵심 근거')[0]
        self.assertIn('설정 파일의 예외 옵션을 삭제 처리했습니다',core)
        self.assertNotIn('조치 내용 안내함',core)

    def test_technical_answer_covers_selected_short_passages_upfront(self):
        from backend.evaluation.explanation_quotes import validate_quote_answer,render_quote_answer
        rows=[{'field':'note:1','note_id':1,'text':'설정은 사용자가 별도로 하지 않아도 됩니다.'},
              {'field':'note:2','note_id':2,'text':'ReadSeries 함수를 조회 뒤 호출합니다.'},
              {'field':'note:3','note_id':3,'text':'일간 주기만 지원하며 분간 주기는 지원하지 않습니다.'}]
        g={'issues':[{'id':101,'answer_evidence':{'excerpts':rows}}]}
        v=validate_quote_answer({'selected_issue_ids':[101],'sections':[{'topic':'technical','unanswered':False,
            'evidence':[{'issue_id':101,'field':r['field'],'quote':r['text']} for r in rows]}]},g)
        core=render_quote_answer(v,g,question='사용자 설정, 연결 함수, 지원 주기를 설명해줘').split('### 핵심 근거')[0]
        for text in ['별도로 하지 않아도','ReadSeries','분간 주기는 지원하지']:
            self.assertIn(text,core)

    def test_another_stage_completion_does_not_complete_unfinished_customer_validation(self):
        core=self.core('고객 검증까지 완료됐어?', ['운영 반영 완료했습니다. 고객 검증은 미완료입니다.'])
        self.assertNotIn('고객 검증 완료가 확인됩니다',core)

    def test_development_guide_is_not_a_question_about_development_completion(self):
        core=self.core('그 개발 안내가 고객 검증까지 완료됐다는 뜻이야?',
                       ['개발 일정은 검토 중입니다.','요청하신 자료를 전달했습니다.'])
        self.assertIn('고객 검증 완료를 확인할 수 없습니다',core)
        self.assertNotIn('**개발·수정**',core)
