"""평가 대상 밖 원문은 모델에게 전달할 원장에 게시하지 않는다."""
import unittest
from unittest.mock import patch

from backend.tools.ledger import EvidenceLedger
from backend.tools.registry import ToolRegistry
from scripts.accuracy.benchmark_scope import ApprovalScopeError, approved_mantis_issues


class ScopeTests(unittest.TestCase):
    def test_allowed_rows_and_related_notion_are_unchanged(self):
        ledger = EvidenceLedger()
        with approved_mantis_issues({1, 2}) as events:
            ledger.add('mantis', {'issues_list': [{'id': 1}], 'meta': {'matched_issue_ids': [1, 2]}},
                       grade='scoped', unapplied=(), call_id='a')
            ledger.add('notion', {'notion_pages': [{'id': 'p', 'content': '이슈 #3 참고'}]},
                       grade='reference', unapplied=(), call_id='b')
        self.assertEqual(len(ledger.stages), 2)
        self.assertEqual(events, [])

    def test_unapproved_row_or_count_population_fails_before_publication(self):
        for stage in ({'issues_list': [{'id': 3, 'description': 'secret'}]},
                      {'issues_list': [], 'meta': {'matched_issue_ids': [1, 3]}}):
            ledger = EvidenceLedger()
            with approved_mantis_issues({1, 2}) as events:
                with self.assertRaises(ApprovalScopeError) as raised:
                    ledger.add('mantis', stage, grade='scoped', unapplied=(), call_id='a')
            self.assertEqual(ledger.stages, [])
            self.assertEqual(events[0]['denied_issue_ids'], [3])
            self.assertNotIn('secret', str(raised.exception))

    def test_report_with_internal_model_calls_cannot_run_outside_matrix(self):
        with patch.object(ToolRegistry, '_execute') as execute, approved_mantis_issues({1}) as events:
            with self.assertRaises(ApprovalScopeError):
                ToolRegistry([])._execute('build_issue_report', {}, '', None)
            execute.assert_not_called()
        self.assertEqual(events[0]['tool'], 'build_issue_report')

    def test_empty_scope_is_rejected(self):
        with self.assertRaises(ValueError), approved_mantis_issues(set()):
            pass
