"""표시명 변경이 원본 프로젝트별 집계·검색 범위를 바꾸지 않아야 한다."""
import unittest
from backend.reports.aggregate import format_count_group_lines, count_group_key
from backend.sources.mantis import build_sources_from_issue_rows
from backend.workspace import _issue_summary
from backend.reports.team_brief import _decision_summary


class CustomerDisplayCoverageTests(unittest.TestCase):
    def test_count_labels_keep_separate_raw_groups(self):
        groups = {'143WooriFuture(우리선물)': 2, '164NHFuture': 3, '147MiraeAsset(미래에셋증권)': 4}
        text = '\n'.join(format_count_group_lines(groups, 'customer'))
        self.assertNotIn('147MiraeAsset', text)
        self.assertIn('미래에셋증권 — 4건', text)
        self.assertIn('NH선물 — 2건', text)
        self.assertIn('NH선물 — 3건', text)
        self.assertEqual(len(groups), 3)
        for raw in groups:
            self.assertEqual(count_group_key('customer')({'project': raw}), raw)

    def test_sources_and_workspace_keep_raw_identity_with_display_field(self):
        issue = {'id': 77, 'summary': '원문 147MiraeAsset 로그', 'project': '159eBest', 'detail': {'description': '159eBest literal'}}
        for item in (build_sources_from_issue_rows([issue])[0], _issue_summary(issue)):
            self.assertEqual(item['project'], '159eBest')
            self.assertEqual(item['customer_name'], 'LS증권')
        self.assertEqual(build_sources_from_issue_rows([issue])[0]['detail'], issue['detail'])

    def test_dashboard_decision_uses_display_title_but_keeps_exact_question(self):
        summary = _decision_summary({'newIssues': 4}, [{'name':'147MiraeAsset(미래에셋증권)', 'count':4}], [], [])
        self.assertIn('미래에셋증권에서', summary['signals'][0])
        self.assertNotIn('147MiraeAsset', summary['signals'][0])
        action = summary['recommendedActions'][0]
        self.assertEqual(action['title'], '미래에셋증권 증가 원인 확인')
        self.assertIn('147MiraeAsset(미래에셋증권)', action['question'])

    def test_browser_directory_uses_the_existing_customer_naming_source(self):
        from backend.api.templates import list_customer_names
        from backend.config import CUSTOMER_DIRECTORY
        from backend.display import customer_name
        names = list_customer_names()
        self.assertEqual(set(names), {'exact', 'projects'})
        for entry in CUSTOMER_DIRECTORY.values():
            for project in entry.get('projects', []):
                self.assertEqual(names['projects'][project], entry['name'])
                self.assertEqual(names['exact'][project], customer_name(project))

    def test_aitas_display_is_separate_without_changing_filter_aliases(self):
        from backend.config import CUSTOMER_ALIASES
        from backend.display import customer_name
        self.assertEqual(customer_name('098Itas(신한 아이타스)'), '신한펀드파트너스')
        self.assertNotIn('098Itas', CUSTOMER_ALIASES.get('신한', []))
        self.assertEqual(customer_name('132SHGM(신한굿모닝증권)'), '신한투자증권')

    def test_legacy_alias_projects_are_in_browser_name_directory(self):
        from backend.api.templates import list_customer_names
        from backend.display import customer_name
        names = list_customer_names()
        for project in ('098Itas', '106Koscom', '106KoscomOil', '116KMB', '122KoscomCME', '139PATS'):
            self.assertEqual(names['projects'].get(project), customer_name(project))
