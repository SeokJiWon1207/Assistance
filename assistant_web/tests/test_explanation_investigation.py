"""누락된 원문만 선택 이슈 안에서 한 번 읽고 충분한 근거는 재조회하지 않는다."""
import unittest
from backend.services.explanation_investigation import investigation_plan


class ExplanationInvestigationTests(unittest.TestCase):
    def plan(self, *, complete=False, unanswered=False, query='조회 함수는?', request=None, legacy=False):
        v={'selected_issue_ids':[91,92,93], 'sections':[{'topic':'technical','unanswered':unanswered}],
           'answer_items':[{'question_part':'조회 함수','unanswered':unanswered,'draft_answer':'확인할 근거가 없습니다.' if unanswered else 'ReadValue입니다.'}]}
        if legacy:v['answer_items']=[]
        g={'issues':[{'id':i,'answer_evidence':{'complete':complete}} for i in [91,92,93,94]]}
        return investigation_plan(query,v,g,request)

    def test_sufficient_partial_source_is_not_fetched_again(self):
        self.assertEqual(self.plan()['issue_ids'],[])

    def test_missing_information_and_incomplete_history_are_bounded_to_two_selected_issues(self):
        self.assertEqual(self.plan(unanswered=True)['issue_ids'],[91,92])
        self.assertEqual(self.plan(query='QC 완료와 최종 이력은?')['issue_ids'],[91,92])

    def test_absence_in_complete_source_does_not_trigger_repeat_search(self):
        self.assertEqual(self.plan(complete=True,unanswered=True)['issue_ids'],[])

    def test_arbitrary_issue_requests_cannot_expand_scope(self):
        self.assertEqual(self.plan(request=[94,999])['issue_ids'],[])
        self.assertEqual(self.plan(request=[92,91,93],unanswered=True)['issue_ids'],[92,91])

    def test_legacy_selection_still_preserves_missing_note_safety(self):
        self.assertEqual(self.plan(legacy=True)['issue_ids'],[91,92])

    def test_request_alone_cannot_force_extra_io_when_answer_is_sufficient(self):
        self.assertEqual(self.plan(request=[91])['issue_ids'],[])

    def test_optional_deadline_returns_partial_evidence_and_does_not_cancel_parent(self):
        from unittest.mock import patch
        from backend.services.explanation_investigation import bounded_investigation
        from backend.execution import ExecutionBudget, execution_scope, RequestStopped
        now=[1.0]
        plan={'issue_ids':[91],'timeout_seconds':8}
        parent=ExecutionBudget(deadline=100)
        def action():
            now[0]=10
            raise RequestStopped()
        with patch('backend.services.explanation_investigation.time.monotonic',side_effect=lambda:now[0]):
            with execution_scope(parent):
                g,meta=bounded_investigation(action,{'issues':[]},plan)
                self.assertTrue(meta['timed_out'])
                self.assertFalse(parent.cancelled.is_set())
                self.assertEqual(g,{'issues':[]})
                parent.remaining()

    def test_user_cancellation_is_not_swallowed_as_optional_timeout(self):
        from unittest.mock import patch
        from backend.services.explanation_investigation import bounded_investigation
        from backend.execution import ExecutionBudget, execution_scope, RequestStopped
        parent=ExecutionBudget(deadline=100)
        def action():
            parent.cancel()
            raise RequestStopped(cancelled=True)
        with patch('backend.services.explanation_investigation.time.monotonic',return_value=1):
            with execution_scope(parent):
                with self.assertRaises(RequestStopped):
                    bounded_investigation(action,{}, {'issue_ids':[91],'timeout_seconds':8})
