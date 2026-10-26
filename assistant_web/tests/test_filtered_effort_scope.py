"""조건별 공수의 전체 저장본 사용과 불완전한 실시간 수집 표시."""
import unittest
from contextlib import nullcontext
import tempfile
from pathlib import Path
from unittest.mock import patch
from backend.reports import effort
from backend.sources.mantis_collection import IssueBatch
from backend.evaluation.data_scope import build_answer_data_scope


def issue(number, created='2026-09-01T09:00:00+09:00'):
    return {'id': number, 'summary': '[MTS] 테스트', 'project': {'name': '147MiraeAsset(미래에셋증권)'},
            'created_at': created, 'custom_fields': [{'field': {'name': '고객사전달일'}, 'value': created[:10]}, {'field': {'name': '3PC공수'}, 'value': '이영희(0.5)'}]}


class FilteredEffortScopeTests(unittest.TestCase):
    def report(self):
        return effort.run_filtered_effort_report('미래에셋 MTS 9월 공수', 2026, 9, {})

    def test_complete_snapshot_counts_all_rows_and_propagates_scope(self):
        batch = IssueBatch([issue(1), issue(2)], {'status': 'snapshot', 'search_complete': True,
                           'fetchedAt': '2026-10-15T00:00:00+00:00', 'snapshot_complete': True})
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(batch)), \
             patch.object(effort, 'fetch_mantis_issues', return_value=[]):
            result = self.report()
        self.assertIsNotNone(result['effortChart'])
        self.assertEqual(result['effortChart']['totalEffort'], 1.0)
        self.assertEqual(result['effortChart']['issueCount'], 2)
        scope = build_answer_data_scope(result['retrieval'])[0]
        self.assertFalse(scope['warning'])
        self.assertEqual(scope['basis'], '저장본')
        self.assertEqual(scope['asOf'], '2026-10-15T00:00:00+00:00')

    def test_exhausted_live_results_have_confirmed_scope(self):
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(None)), \
             patch.object(effort, 'fetch_mantis_issues', side_effect=lambda p: [issue(1)] if p['page']==1 else []):
            result = self.report()
        self.assertEqual(result['effortChart']['issueCount'], 1)
        scope = build_answer_data_scope(result['retrieval'])[0]
        self.assertFalse(scope['warning'])
        self.assertEqual(scope['basis'], '조회 결과')
        self.assertTrue(scope['asOf'])

    def test_old_pages_do_not_prove_exhaustion(self):
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(None)), \
             patch.object(effort, 'fetch_mantis_issues', return_value=[issue(1,'2025-01-01')]):
            result = self.report()
        self.assertTrue(build_answer_data_scope(result['retrieval'])[0]['warning'])
        self.assertIs(result['retrieval']['stages'][0]['meta'].get('search_complete'), False)

    def test_snapshot_preserves_body_keyword_and_requester_filters(self):
        from backend.sources import mantis_store as store
        row = issue(1)
        row['description'] = 'From. 석지원 긴급분석 요청'
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(store, 'store_path', return_value=Path(directory)/'mantis_complete.sqlite3'), \
             patch.object(store, 'connection_key', return_value='fixture'):
            store.sync_complete_store(lambda page: [row] if page == 1 else [], workers=1)
            for constraints in [
                {'filter_clauses': [{'field': 'keyword', 'operator': 'contains', 'value': '긴급분석'}]},
                {'people': {'names': ['석지원'], 'role': 'requester'}},
            ]:
                with self.subTest(constraints=constraints):
                    result = effort.run_filtered_effort_report('9월 공수', 2026, 9, constraints)
                    self.assertIsNotNone(result['effortChart'])
                    self.assertEqual(result['effortChart']['totalEffort'], 0.5)
                    self.assertEqual(result['effortChart']['issueCount'], 1)
