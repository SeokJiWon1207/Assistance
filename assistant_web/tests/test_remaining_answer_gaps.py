"""기술 조치·회신·최신 이력을 서로의 완료 근거로 대체하지 않는다."""
from unittest import TestCase
from unittest.mock import Mock, patch

from backend.evaluation.explanation_direct import _stage_event
from backend.intent.unified import plan_question
from backend.intent.unified_validation import validate_plan
from backend.reports.issue_documents import build_issue_document_report
import test_direct_explanation as direct_tests
from test_unified_planner import proposal


class SentReplyTests(TestCase):
    core = direct_tests.DirectExplanationTests.core
    answer = direct_tests.DirectExplanationTests.answer
    def test_sent_explanation_is_delivered_communication_without_patch_completion(self):
        text = ('2026-08-10 메일 회신 To. 고객담당자\n'
                '배열 범위를 벗어날 때의 예외처리는 의도된 동작이며 실제 메모리 누수가 아닙니다.\n'
                '수정이 가능한 부분이 아님을 알려 드립니다.')
        core = self.core('그 조치가 고객사 전달까지 진행됐어?', [text])
        self.assertIn('회신 전달', core)
        self.assertIn('노트 1', core)
        self.assertIn('수정본 전달', core)
        self.assertNotIn('고객사 전달 완료가 확인됩니다', core)

    def test_received_planned_draft_quoted_or_unsent_reply_is_not_sent(self):
        for text in ['2026-08-10 메일 수신 From. 고객담당자\n설명 확인했습니다.',
                     '2026-08-10 메일 회신 To. 고객담당자 예정',
                     '메일 회신 초안 To. 고객담당자', '메일 회신 To. 고객담당자 미발송',
                     '> 2026-08-10 메일 회신 To. 다른 담당자',
                     '고객에게 메일 회신 요청', '추후 메일 답변을 발송하겠습니다.']:
            with self.subTest(text=text):
                self.assertFalse(any(state == 'done' for state, *_ in _stage_event('reply', {'text':text})))

    def test_question_about_patch_does_not_use_explanation_as_patch_delivery(self):
        core = self.core('수정본이 고객사에 전달 완료됐어?',
                         ['2026-08-10 메일 회신 To. 고객담당자\n의도된 동작이라 수정하지 않습니다.'])
        self.assertIn('수정본 전달 완료를 확인할 수 없습니다', core)
        self.assertNotIn('고객사 전달 완료가 확인됩니다', core)


class DocumentEvidenceSeparationTests(TestCase):
    def build(self, notes, text='사후 분석을 5줄로 작성해줘', kind='postmortem'):
        issue = {'id':9000, 'summary':'차트 메모리 검토', 'status':{'name':'closed'},
                 'notes':[{'id':i+1, 'created_at':f'2026-05-{i+1:02}', 'text':note}
                          for i,note in enumerate(notes)]}
        with patch('backend.reports.issue_documents.fetch_mantis_issue_by_id',return_value=issue):
            return build_issue_document_report(text,{'issue_ids':[9000]},kind)['content']

    def test_qc_and_delivery_do_not_fill_the_technical_action_field(self):
        for text in ['사후 분석을 5줄로 작성해줘', '사후 분석을 회의 안건으로 작성해줘']:
            answer = self.build(['원인: 전체를 다시 그리는 부분 때문에 CPU 부하 발생',
                                 '[QC 요청]', '[QC 완료]', '[고객사 전달]'],text)
            action = next(line for line in answer.splitlines() if line.startswith('기술 조치:'))
            self.assertIn('구체적인 기술 수정 내용은 확인하지 못했습니다', action)
            self.assertNotIn('QC', action)
            self.assertIn('내부 QC: 완료 기록 확인', answer)
            self.assertIn('고객사 전달: 완료 기록 확인', answer)
            self.assertIn('노트 3', answer)
            self.assertIn('노트 4', answer)

    def test_recorded_technical_change_remains_separate_from_qc(self):
        answer = self.build(['원인: 타이머 해제 누락 때문에 메모리 누수 발생. 타이머 삭제 수정함.',
                             '[QC 완료]', '[전달완료]'])
        action = next(line for line in answer.splitlines() if line.startswith('기술 조치:'))
        self.assertIn('타이머 삭제 수정함', action)
        self.assertIn('노트 1', action)
        self.assertNotIn('QC', action)

    def test_delivery_notice_does_not_crowd_out_the_second_technical_change(self):
        answer=self.build(['수정하여 소스 전달 드립니다.\n기본 값을 보이도록 수정하였습니다.\n점선 표시를 조정하였습니다.'])
        action=next(line for line in answer.splitlines() if line.startswith('기술 조치:'))
        self.assertIn('기본 값을',action)
        self.assertIn('점선 표시',action)

    def test_latest_customer_recheck_is_in_core_handoff_and_next_action(self):
        for audience in ['개발팀','팀장']:
            answer = self.build(['class TimeMeterPDLL에서 Delete 되지 않는 부분 확인\n'
                                 '처음 로드하는 클래스의 소멸자에서 해제하도록 처리, 정상 해제 확인',
                                 '[QC 완료]', '[고객사 전달]',
                                 '2026.05.22 유선 with. 고객담당자\n아직도 메모리가 많이 오르고 있는 상황이며 추가 확인 요청 수신'],
                                audience+' 인수인계', 'handoff')
            core = answer.split('### 타임라인')[0]
            self.assertIn('아직도 메모리가', core)
            self.assertIn('노트 4', core)
            self.assertIn('2026-05-04', core)
            self.assertIn('소멸자', core)
            next_action = core.split('### 다음 행동')[1].split('### 주의사항')[0]
            self.assertIn('재현', next_action)
            self.assertNotIn('별도의 막힘', core)

    def test_later_explicit_resolution_supersedes_old_recheck(self):
        answer = self.build(['아직도 메모리가 증가하여 추가 확인 요청 수신',
                             '추가 누수 수정 완료. 고객사 정상 동작 확인했습니다.'], '팀장 인수인계','handoff')
        blocked = answer.split('### 막힌 점')[1].split('### 다음 행동')[0]
        self.assertNotIn('아직도', blocked)

    def test_action_section_with_request_is_not_a_completed_technical_change(self):
        answer=self.build(['- 작업 내역\n타이머 해제 수정 요청', '[QC 완료]'])
        action=next(line for line in answer.splitlines() if line.startswith('기술 조치:'))
        self.assertIn('구체적인 기술 수정 내용은 확인하지 못했습니다',action)

    def test_delivery_date_field_is_preserved_without_inventing_the_artifact(self):
        from backend.reports.issue_documents import _progress
        answer='\n'.join(_progress([],{'고객사전달일':'2026-05-04','QC 완료 목표일':'2026-05-03'}))
        self.assertIn('2026-05-04 [필드 고객사전달일]',answer)
        self.assertIn('전달물 상세 확인 필요',answer)
        self.assertIn('내부 QC: 완료 근거 미확인',answer)

    def test_late_long_note_technical_change_is_not_lost_to_display_limits(self):
        notes=['현장 확인 ' * 500 + '\n타이머 해제 추가 수정함.']+['단순 연락 기록']*14
        answer=self.build(notes)
        self.assertIn('타이머 해제 추가 수정함',answer)
        self.assertIn('노트 1',answer)

    def test_customer_decision_is_not_lost_when_it_has_no_recheck_keyword(self):
        answer=self.build(['[고객사 전달]',
                           '2026-05-02 메일 수신 From. 고객담당자\n\n'
                           '현재 제안은 사용성이 떨어져 적용이 어려울 것 같습니다.\n\n'
                           '내부적으로 다른 방식으로 진행할 지 논의해보고 추후 필요한 개발이 생기면 요청드리겠습니다.'],
                          '개발팀 인수인계','handoff')
        core=answer.split('### 타임라인')[0]
        self.assertIn('최신 고객 기록',core)
        self.assertIn('추후 필요한 개발',core)
        self.assertIn('노트 2',core)
        self.assertNotIn('개발 완료',core)

    def test_customer_record_for_another_issue_is_not_imported_into_handoff(self):
        answer=self.build(['2026-05-02 메일 수신 From. 고객담당자\n\n'
                           '9001: 다른 이슈 제목\n\n내부적으로 논의하고 추후 개발을 요청하겠습니다.'],
                          '개발팀 인수인계','handoff')
        core=answer.split('### 타임라인')[0]
        self.assertNotIn('추후 개발을',core)

    def test_latest_customer_report_keeps_android_and_ios_observations(self):
        answer=self.build(['2026-05-02 메일 수신 From. 고객담당자\n\n'
                           'Android\n\n색상 팝업 닫기 버튼 늘어남\n\n설정 라디오 캡션 안보임\n\n'
                           'iOS\n\n설정 화면 하단 잘림\n\n지표 팝업 상단 잘림\n\n+ 기타 사항은 점검 후 안내하겠습니다.'],
                          '개발팀 인수인계','handoff')
        core=answer.split('### 타임라인')[0]
        for fact in ['Android','캡션 안보임','iOS','하단 잘림','상단 잘림']:
            self.assertIn(fact,core)
        self.assertNotIn('별도의 막힘',core)

    def test_symptom_repeated_in_cause_field_is_not_a_technical_cause(self):
        raw={'id':9000,'summary':'[MTS] 선택한 지표가 표시되지 않는 현상','status':{'name':'closed'},
             'custom_fields':[{'field':{'name':'사유 및 원인'},'value':'선택한 지표가 표시되지 않는 현상'},
                              {'field':{'name':'조치 및 개선'},'value':'배열 초기화 로직 추가'}]}
        with patch('backend.reports.issue_documents.fetch_mantis_issue_by_id',return_value=raw):
            answer=build_issue_document_report('사후 분석서',{'issue_ids':[9000]},'postmortem')['content']
        self.assertIn('기술 원인은 확정하지 못했습니다',answer)
        self.assertNotIn('원인과 수정 항목이 함께 기록',answer)

    def test_administrative_closure_is_kept_without_becoming_a_technical_fix(self):
        raw={'id':9000,'summary':'차트 오류 문의','status':{'name':'closed'},
             'custom_fields':[{'field':{'name':'조치 및 개선'},'value':'재현되지 않아 이슈 종료처리'}]}
        with patch('backend.reports.issue_documents.fetch_mantis_issue_by_id',return_value=raw):
            answer=build_issue_document_report('5줄 사후 분석',{'issue_ids':[9000]},'postmortem')['content']
        self.assertIn('별도 업무 처리: 재현되지 않아 이슈 종료처리',answer)
        self.assertIn('기술 조치: 구체적인 기술 수정 내용은 확인하지 못했습니다',answer)


class ReplyContentGroundingTests(TestCase):
    def grounding(self):
        return {'issues':[{'id':9000,'answer_evidence':{'complete':True,'excerpts':[
            {'field':'note:1','note_id':1,'text':'2026-05-02 메일 회신 To. 고객담당자\n\n요청 내용은 접수되었습니다.\n\n향후 문의 제목 앞에 MTS를 표기 요청드립니다.'},
            {'field':'description','text':'고객은 네트워크 지연 때문일 것으로 추정함.'}]}}]}

    def test_reply_summary_cannot_import_a_customer_hypothesis_from_the_description(self):
        from backend.evaluation.explanation_summary import summary_items
        v={'valid':True,'selected_issue_ids':[9000],'answer_items':[
            {'question_part':'어떤 답변','draft_answer':'네트워크 지연이 원인입니다.','unanswered':False,
             'evidence':[{'issue_id':9000,'field':'description','text':'고객은 네트워크 지연 때문일 것으로 추정함.'}]}]}
        self.assertEqual(summary_items(v,self.grounding(),'고객에게 어떤 답변을 했어?'),[])

    def test_fallback_reply_content_preserves_receipt_and_separate_instruction(self):
        from backend.evaluation.explanation_direct import direct_conclusion
        g=self.grounding();content='\n'.join(direct_conclusion('고객에게 어떤 답변을 했어?',[],[9000],{9000:g['issues'][0]}))
        self.assertIn('접수되었습니다',content)
        self.assertIn('MTS를 표기 요청',content)
        self.assertNotIn('네트워크',content)


class PlannerTaskTransitionTests(TestCase):
    def previous(self, kind='similar'):
        return validate_plan('#9000 이슈 원인과 조치 비교' if kind=='similar' else '#9000 사후 분석서',
                             proposal(operation='report',report_kind=kind,scope='target',
                                      status={'mode':'unspecified','values':[],'evidence':''},
                                      filters=[{'field':'issue_id','operator':'equals','value':9000,'evidence':'#9000'}],
                                      terms=['CPU'] if kind=='similar' else []), {}, {})

    def test_explicit_end_preserves_base_and_clears_only_topic(self):
        q='비교는 끝내고 기준 이슈 하나의 현재 상태만 알려줘'
        p=validate_plan(q,proposal(operation='get',scope='inherit',followup=True,
                                  status={'mode':'inherit','values':[],'evidence':''},clear_fields=['terms']),
                        {},self.previous())
        self.assertEqual(p['resolved_constraints']['issue_ids'],[9000])
        self.assertEqual(p['terms'],[])
        self.assertIsNone(p['report_kind'])
        for invalid in ['group_by','sort','report_kind']:
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError,invalid):
                validate_plan(q,proposal(operation='get',scope='inherit',followup=True,
                              status={'mode':'inherit','values':[],'evidence':''},clear_fields=[invalid]),
                              {},self.previous())

    def test_document_first_prompt_carries_explicit_task_and_filter_clear_contract(self):
        payload=proposal(operation='report',report_kind='postmortem',scope='inherit',followup=True,
                         status={'mode':'inherit','values':[],'evidence':''})
        response=Mock();response.json.return_value={'choices':[{'message':{'content':__import__('json').dumps(payload)}}]}
        with patch('backend.intent.unified.post_model_chat',return_value=response) as model:
            plan=plan_question('그 분석은 그대로 두고 회의용 안건 형식으로 바꿔줘',
                               {'id':'test'},'test-key',{},self.previous('postmortem'))
        self.assertEqual(plan['report_kind'],'postmortem')
        self.assertEqual(model.call_count,1)
        user_message=__import__('json').loads(model.call_args.args[2]['messages'][1]['content'])
        self.assertEqual(user_message['previous_task']['report_kind'],'postmortem')
        self.assertNotIn('group_by', user_message['allowed_clear_fields'])

    def test_current_status_only_answer_omits_body_without_changing_source(self):
        from backend.answers import format_deterministic_issue_answer
        row={'id':9000,'summary':'기준 건','status':'closed','project':'메리츠','handler':'담당자',
             'created_at':'2026-05-01','detail':{'description':'비교 대상의 긴 원문','updated_at':'2026-05-04'}}
        retrieval={'stages':[{'source':'mantis_issue','status':'ok','issues_list':[row],
                              'meta':{'search_complete':True}}]}
        answer=format_deterministic_issue_answer('비교는 끝내고 기준 이슈의 현재 상태만 알려줘',
                                                {'issue_ids':[9000]},retrieval,5)
        self.assertIn('#9000',answer)
        self.assertIn('종료된 이슈',answer)
        self.assertIn('현재 상태 필드',answer)
        self.assertNotIn('긴 원문',answer)

    def test_validated_get_cannot_reenter_similarity_because_of_the_word_comparison(self):
        from backend.retrieval import run_retrieval_pipeline
        p=validate_plan('비교는 끝내고 기준 이슈 하나의 현재 상태만 알려줘',
                        proposal(operation='get',scope='inherit',followup=True,
                                 status={'mode':'inherit','values':[],'evidence':''},clear_fields=['terms']),{},self.previous())
        with patch('backend.retrieval.execute_mantis_id_lookup',return_value={'status':'ok','issues_list':[]}) as lookup, \
             patch('backend.retrieval.execute_chroma_search') as vector:
            run_retrieval_pipeline(p['grounding_text'],5,{'mantis':True,'vectorDb':True},p)
        lookup.assert_called_once()
        vector.assert_not_called()

    def test_compact_continuation_still_calls_model_and_validates_the_previous_scope(self):
        response=Mock();response.json.return_value={'choices':[{'message':{'content':'{"document_presentation_only":true}'}}]}
        previous=self.previous('postmortem')
        with patch('backend.intent.unified.post_model_chat',return_value=response) as model:
            p=plan_question('그 분석은 그대로 두고 회의용 안건 형식으로 바꿔줘',{'id':'test'},'key',{},previous)
        self.assertEqual(model.call_count,1)
        self.assertEqual(p['operation'],'report')
        self.assertEqual(p['resolved_constraints']['issue_ids'],[9000])
        self.assertEqual(p['interpretation']['clear_fields'],[])
        self.assertEqual(p['report_kind'],'postmortem')

    def test_new_conditions_or_targets_never_use_the_compact_document_contract(self):
        from backend.intent.document_continuation import eligible
        previous=self.previous('postmortem')
        for q in ['#9001을 회의 안건으로 바꿔줘','고객사 조건을 해제하고 회의 안건으로 바꿔줘',
                  '내 이슈 몇 건이야?','같은 문서를 정중한 고객 메일로 바꿔줘',
                  '지난달만 회의 안건으로 바꿔줘','팀장 홍길동 이슈를 회의 안건으로 바꿔줘',
                  '같은 문서에서 종료 이슈를 제외하고 회의 안건으로 바꿔줘']:
            with self.subTest(q=q):self.assertFalse(eligible(q,previous))

    def test_explicit_status_indifference_is_grounded_without_expanding_quantity_only(self):
        p=validate_plan('메리츠 MTS 이슈를 종료 여부와 관계없이 몇 건인지 세어줘',
                        proposal(scope='team',status={'mode':'all','values':[],'evidence':'종료 여부와 관계없이'}),{}, {})
        self.assertEqual(p['resolved_constraints']['statuses'],[])

    def test_comparison_candidate_filter_is_not_a_filter_on_the_base_issue(self):
        previous=validate_plan('KB증권 사례로만 좁혀줘',proposal(
            operation='report',report_kind='similar',scope='inherit',followup=True,
            status={'mode':'inherit','values':[],'evidence':''},filters=[
                {'field':'customer','operator':'equals','value':'kb','evidence':'KB증권'}]),{},self.previous())
        q='비교를 중단하고 기준 이슈 한 건의 현재 상태만 보여줘'
        p=proposal(operation='get',scope='inherit',followup=True,
                   status={'mode':'inherit','values':[],'evidence':''},clear_fields=['terms'])
        with self.assertRaisesRegex(ValueError,'후보 조건'):
            validate_plan(q,p,{},previous)
        p['clear_fields']=['terms','customer']
        correct=validate_plan(q,p,{},previous)
        self.assertEqual(correct['resolved_constraints']['issue_ids'],[9000])
        self.assertIsNone(correct['resolved_constraints']['customer_key'])
