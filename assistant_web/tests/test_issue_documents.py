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
