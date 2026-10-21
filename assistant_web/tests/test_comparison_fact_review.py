"""불명확한 비교 근거만 원문 문단 ID로 선택하며 출처·버전·지연을 제한한다."""
import importlib
import importlib.util
import json
from unittest import TestCase
from unittest.mock import Mock, patch

import requests


class ComparisonFactReviewTests(TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('backend.reports.comparison_facts'))
        self.review = importlib.import_module('backend.reports.comparison_facts')
        with self.review._LOCK:
            self.review._CACHE.clear()

    def run_review(self, issues, selection):
        def respond(model, key, payload, **kwargs):
            catalog = json.loads(payload['messages'][1]['content'])['passages']
            value = selection(catalog)
            return Mock(raise_for_status=lambda: None, json=lambda: {'choices': [{'message': {'content': json.dumps(value)}}]})
        with patch.object(self.review, 'post_model_chat', side_effect=respond):
            return self.review.review_comparison_facts(issues, {'id': 'company', 'model': 'company'}, 'key')

    def test_negative_defect_description_does_not_hide_completed_fix(self):
        from backend.reports.similar import _recorded_fix, _recorded_cause
        issues = [{'id': 1, 'notes': [{'id': 10, 'text': '조건중 malloc후 free 안하는 조건들이 있어서 수정함'}]}]
        def select(rows):
            return {'issues': [{'issue_id': 1, 'cause': [rows[0]['id']], 'action': [rows[0]['id']]}]}
        meta = self.run_review(issues, select)
        self.assertEqual(meta['status'], 'reviewed')
        self.assertIn('free 안하는', _recorded_fix(issues[0])[0])
        self.assertIn('노트 10', _recorded_cause(issues[0])[0])

    def test_selection_cannot_cross_an_issue_boundary_or_invent_a_passage(self):
        issues = [{'id': 1, 'notes': [{'id': 10, 'text': 'malloc 후 free 안해서 수정함'}]},
                  {'id': 2, 'notes': [{'id': 20, 'text': 'delete 안하는 부분 수정함'}]}]
        def select(rows):
            other = next(r['id'] for r in rows if r['issue_id'] == 2)
            return {'issues': [{'issue_id': 1, 'cause': [other], 'action': ['invented']} ]}
        meta = self.run_review(issues, select)
        self.assertEqual(meta['status'], 'rejected')
        self.assertNotIn('comparison_facts', issues[0])

    def test_cache_reuses_only_facts_bound_to_the_same_current_source(self):
        first = {'id': 1, 'notes': [{'id': 10, 'text': 'malloc 후 free 안해서 수정함'}]}
        self.run_review([first], lambda rows: {'issues': [{'issue_id': 1, 'cause': [], 'action': [rows[0]['id']]}]})
        same = {'id': 1, 'notes': [{'id': 10, 'text': 'malloc 후 free 안해서 수정함'}]}
        with patch.object(self.review, 'post_model_chat', side_effect=AssertionError('불필요한 재호출')):
            meta = self.review.review_comparison_facts([same], {'id': 'company', 'model': 'company'}, 'key')
        self.assertEqual(meta['cache_hits'], 1)
        changed = {'id': 1, 'notes': [{'id': 10, 'text': 'malloc 후 free 안해서 수정함'},
                                    {'id': 11, 'text': '앞선 수정 롤백 예정'}]}
        meta = self.run_review([changed], lambda rows: {'issues': [{'issue_id': 1, 'cause': [], 'action': []}]})
        self.assertEqual(meta['cache_hits'], 0)

    def test_complete_structured_work_does_not_add_a_model_call(self):
        issues = [{'id': 1, 'notes': [{'id': 10, 'text': '- 현상 원인\n타이머 해제 누락\n- 작업 내역\n그리드 타이머 삭제 추가'}]}]
        with patch.object(self.review, 'post_model_chat', side_effect=AssertionError('빠른 경로')):
            meta = self.review.review_comparison_facts(issues, {'id': 'company'}, 'key')
        self.assertEqual(meta['status'], 'not_needed')

    def test_source_change_outside_the_short_model_context_invalidates_cache(self):
        first = {'id': 1, 'notes': [{'id': 9, 'text': '과거 원문 ' * 500},
                                  {'id': 10, 'text': 'malloc 후 free 안해서 수정함'}]}
        self.run_review([first], lambda rows: {'issues': [{'issue_id': 1, 'cause': [], 'action': [rows[0]['id']]}]})
        changed = {'id': 1, 'notes': [{'id': 9, 'text': '정정한 원문 ' * 500},
                                    {'id': 10, 'text': 'malloc 후 free 안해서 수정함'}]}
        meta = self.run_review([changed], lambda rows: {'issues': [{'issue_id': 1, 'cause': [], 'action': []}]})
        self.assertEqual(meta['cache_hits'], 0)

    def test_timeout_preserves_the_safe_fallback_without_caching_failure(self):
        issues = [{'id': 1, 'notes': [{'id': 10, 'text': 'malloc 후 free 안해서 수정함'}]}]
        with patch.object(self.review, 'post_model_chat', side_effect=requests.Timeout):
            meta = self.review.review_comparison_facts(issues, {'id': 'company'}, 'key')
        self.assertEqual(meta['status'], 'unavailable')
        self.assertNotIn('comparison_facts', issues[0])

    def test_rollbacks_and_workflow_completion_are_not_confirmed_causes(self):
        issues = [{'id': 1, 'notes': [{'id': 10, 'text': '메모리릭 수정 버전에 문제가 있어 롤백을 하려고함'}]}]
        self.run_review(issues, lambda rows: {'issues': [{'issue_id': 1, 'cause': [rows[0]['id']], 'action': []}]})
        from backend.reports.similar import _recorded_cause
        self.assertEqual(_recorded_cause(issues[0]), ('', ''))

    def test_unavailable_review_does_not_promote_a_test_environment_failure_to_cause(self):
        from backend.reports.similar import _recorded_cause
        self.assertEqual(_recorded_cause({'notes': [{'text': '코스콤은 오류로 인해 테스트 불가'}]}), ('', ''))

    def test_unrecognized_technical_action_is_reviewed_without_a_completed_keyword(self):
        issues = [{'id': 1, 'notes': [{'id': 10, 'text': 'CPU 로그 사용량을 조절해 부하가 낮아짐'}]}]
        meta = self.run_review(issues, lambda rows: {'issues': [{'issue_id': 1, 'cause': [], 'action': [rows[0]['id']]}]})
        from backend.reports.similar import _recorded_fix
        self.assertEqual(meta['status'], 'reviewed')
        self.assertIn('부하가 낮아짐', _recorded_fix(issues[0])[0])

    def test_tentative_diagnosis_is_separate_from_a_confirmed_cause(self):
        issues = [{'id': 1, 'notes': [{'id': 10, 'text': 'CPU 테스트 결과 비교봉수 조건이 원인으로 보입니다'}]}]
        self.run_review(issues, lambda rows: {'issues': [{'issue_id': 1, 'cause': [rows[0]['id']], 'action': []}]})
        from backend.reports.similar import _recorded_cause, _unconfirmed_cause
        self.assertEqual(_recorded_cause(issues[0]), ('', ''))
        self.assertIn('원인으로 보입니다', _unconfirmed_cause(issues[0]))

    def test_progress_or_recommendation_is_not_a_completed_action(self):
        issues = [{'id': 1, 'description': '메모리의 물리적인 량의 증가 (고객사 시스템 파트에서 진행)'}]
        self.run_review(issues, lambda rows: {'issues': [{'issue_id': 1, 'cause': [], 'action': [rows[0]['id']]}]})
        from backend.reports.similar import _recorded_fix
        self.assertEqual(_recorded_fix(issues[0]), ('', ''))

    def test_completed_action_in_the_title_is_preserved_as_title_evidence(self):
        issues = [{'id': 1, 'summary': '메모리가 급증되어 서버 전환 시킴'}]
        self.run_review(issues, lambda rows: {'issues': [{'issue_id': 1, 'cause': [], 'action': [rows[0]['id']]}]})
        from backend.reports.similar import _recorded_fix
        self.assertIn('서버 전환 시킴', _recorded_fix(issues[0])[0])

    def test_short_context_keeps_the_title_when_there_are_many_notes(self):
        issues = [{'id': 1, 'summary': '메모리가 급증되어 서버 전환 시킴',
                   'notes': [{'id': i, 'text': '메모리 사용량 확인 자료 '+('x'*100)} for i in range(10, 50)]}]
        def select(rows):
            title = [r['id'] for r in rows if r['field'] == 'summary']
            return {'issues': [{'issue_id': 1, 'cause': [], 'action': title}]}
        self.run_review(issues, select)
        from backend.reports.similar import _recorded_fix
        self.assertIn('서버 전환 시킴', _recorded_fix(issues[0])[0])

    def test_code_action_requires_completion_in_the_same_note(self):
        issues = [{'id': 1, 'notes': [{'id': 10, 'text': 'delete pItem;\n메모리 누수 수정 완료'}]}]
        def select(rows):
            action = next(r['id'] for r in rows if 'delete' in r['text'])
            done = next(r['id'] for r in rows if '수정 완료' in r['text'])
            return {'issues': [{'issue_id': 1, 'cause': [], 'action': [[action, done]]}]}
        # 이슈가 이미 명시적 수정으로 추출되면 빠른 경로를 그대로 유지한다.
        issues[0]['notes'][0]['text'] += '\nnew가 계속 발생하며 메모리 증가'
        meta = self.run_review(issues, select)
        self.assertEqual(meta['status'], 'reviewed')
        from backend.reports.similar import _recorded_fix
        self.assertIn('delete pItem', _recorded_fix(issues[0])[0])

    def test_recorded_operator_action_does_not_skip_an_unlabelled_resource_cause(self):
        issues=[{'id':1,'notes':[{'id':10,'text':'특정 사용자 전략이 자원을 많이 사용하는 전략이었음. 운영자에게 중지 방법 안내함.'}]}]
        def select(rows):
            cause=next(r['id'] for r in rows if '자원' in r['text'])
            return {'issues':[{'issue_id':1,'cause':[cause],'action':[]}]}
        meta=self.run_review(issues,select)
        self.assertEqual(meta['status'],'reviewed')
