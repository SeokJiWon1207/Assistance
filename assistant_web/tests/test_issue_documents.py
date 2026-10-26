"""이슈 문서 초안은 완료 단계를 구분하고 외부 문안에 내부 ID를 노출하지 않는다."""
from unittest import TestCase
from unittest.mock import patch


class IssueDocumentTests(TestCase):
    def build(self,kind,notes):
        from backend.reports.issue_documents import build_issue_document_report
        raw={'id':31003,'summary':'[1768] 화면 사용자 개체 증가','project':{'name':'138Meritz(메리츠)'},
             'status':{'name':'closed'},'notes':[{'id':i+1,'created_at':f'2026-05-{i+1:02}', 'text':text}
                                                for i,text in enumerate(notes)]}
        with patch('backend.reports.issue_documents.fetch_mantis_issue_by_id',return_value=raw):
            return build_issue_document_report('#31003 문서 초안',{'issue_ids':[31003]},kind)

    def test_handoff_distinguishes_requests_from_completed_work(self):
        r=self.build('handoff',['타이머 삭제 수정 요청','[QC 요청]'])
        self.assertIn('다음 행동',r['content'])
        self.assertIn('주의사항',r['content'])
        self.assertNotIn('QC 완료',r['content'])
        self.assertEqual(r['sources'][0]['issue_id'],31003)

    def test_postmortem_keeps_unknown_root_cause_and_no_invented_prevention(self):
        r=self.build('postmortem',['[QC 완료]','[전달완료]'])
        for item in ['타임라인','근본 원인','재발 방지','제안']:
            self.assertIn(item,r['content'])
        self.assertIn('확정하지',r['content'])
        self.assertNotIn('운영 반영 완료',r['content'])

    def test_postmortem_evaluates_recorded_work_without_repeating_sections(self):
        r=self.build('postmortem',['- 현상 원인\n타이머 해제 누락\n- 작업 내역\n그리드 초기화 시 타이머 삭제 추가', '[QC 완료]', '[전달완료]'])
        self.assertEqual(r['content'].count('### 직접 원인'),1)
        self.assertNotIn('### 확인한 내용',r['content'])
        positives=r['content'].split('### 잘된 점')[1].split('### 개선점')[0]
        self.assertIn('원인과 수정 항목',positives)
        self.assertIn('QC',positives)

    def test_delivery_record_does_not_replace_actual_changes(self):
        r=self.build('handoff',['- 현상 원인\n타이머 해제 누락\n- 작업 내역\n1. 그리드 초기화 시 타이머 삭제 추가\n2. 재진입 시 타이머 삭제 추가','[전달완료]'])
        actions=r['content'].split('### 시도한 조치')[1].split('### 진행 단계')[0]
        self.assertIn('그리드 초기화 시 타이머 삭제 추가',actions)
        self.assertIn('재진입 시 타이머 삭제 추가',actions)
        self.assertNotIn('\\n',actions)

    def test_customer_notice_has_no_issue_identifier_or_paths(self):
        r=self.build('notice',['[QC 완료] z:\\internal\\31003.zip','[전달완료]'])
        for forbidden in ['31003','z:\\','노트 1','KillTimer']:
            self.assertNotIn(forbidden,r['content'])
        for expected in ['확인된 사실','고객 영향','진행 상황','다음 안내','확정되지']:
            self.assertIn(expected,r['content'])

class DocumentPresentationTests(TestCase):
    def build(self, text, kind):
        from backend.reports.issue_documents import build_issue_document_report
        raw = {'id': 31003, 'summary': '[1768] 화면 사용자 개체 증가',
               'project': {'name': '138Meritz'}, 'status': {'name': 'closed'},
               'notes': [{'id': 11, 'text': '원인: 타이머 해제 누락 때문에 누수 발생. 타이머 삭제 수정함.', 'created_at': '2026-05-01'},
                         {'id': 12, 'text': '[전달완료]', 'created_at': '2026-05-02'}]}
        with patch('backend.reports.issue_documents.fetch_mantis_issue_by_id', return_value=raw):
            return build_issue_document_report(text, {'issue_ids': [31003]}, kind)

    def test_five_line_review_keeps_facts_and_evidence(self):
        result = self.build('#31003 사후 분석을 5줄 요약 형식으로 작성해줘', 'postmortem')
        lines = [line for line in result['content'].splitlines() if line.strip()]
        self.assertEqual(len(lines), 5)
        self.assertIn('타이머', result['content'])
        self.assertIn('노트 11', result['content'])
        self.assertNotIn('운영 반영 완료', result['content'])
        self.assertEqual(result['sources'][0]['issue_id'], 31003)

    def test_meeting_agenda_is_not_the_full_postmortem(self):
        meeting = self.build('#31003 사후 분석을 회의 안건 형식으로 작성해줘', 'postmortem')['content']
        full = self.build('#31003 사후 분석서 형식으로 작성해줘', 'postmortem')['content']
        self.assertNotEqual(meeting, full)
        self.assertIn('회의 안건', meeting)
        self.assertIn('제안', meeting)

    def test_messenger_and_polite_email_have_distinct_forms_without_identifiers(self):
        messenger = self.build('#31003 간결한 메신저 답변으로 작성해줘', 'notice')['content']
        email = self.build('#31003 정중한 고객 안내 메일로 작성해줘', 'notice')['content']
        self.assertNotEqual(messenger, email)
        self.assertLess(len(messenger), len(email))
        for answer in [messenger, email]:
            self.assertNotIn('31003', answer)
            self.assertIn('확인', answer)

    def test_handoff_audience_changes_focus_without_inventing_done_work(self):
        dev = self.build('#31003 개발팀 인수인계', 'handoff')['content']
        lead = self.build('#31003 팀장 인수인계', 'handoff')['content']
        self.assertNotEqual(dev, lead)
        self.assertIn('개발팀', dev)
        self.assertIn('팀장', lead)
        self.assertIn('타이머', dev)
        self.assertNotIn('운영 반영 완료', lead)

    def test_meeting_agenda_accepts_descriptive_format_wording(self):
        result = self.build('#31003 회의에서 다룰 안건 형식으로 바꿔줘', 'postmortem')
        self.assertIn('회의 안건', result['content'])
        self.assertIn('검토·결정', result['content'])

    def test_every_notice_channel_honors_all_three_tones_and_keeps_uncertainty(self):
        for channel in ['메신저 답변', '점검 공지', '회의 공유문', '고객 안내 메일']:
            answers = [self.build(f'#31003 {tone} 말투의 {channel} 형식으로 작성해줘', 'notice')['content']
                       for tone in ['간결한', '정중한', '상세한']]
            with self.subTest(channel=channel):
                self.assertEqual(len(set(answers)), 3)
                self.assertLess(len(answers[0]), len(answers[2]))
                for answer in answers:
                    self.assertNotIn('31003', answer)
                    self.assertNotIn('노트 11', answer)
                    self.assertIn('다음 안내', answer)
                    self.assertRegex(answer, '확인|확정')
