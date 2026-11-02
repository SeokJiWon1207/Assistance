"""공수는 전체 저장본 또는 마지막 페이지까지 확인한 이슈를 합산한다."""
from contextlib import closing, nullcontext
import json
from pathlib import Path
import sqlite3
import tempfile
from unittest import TestCase
from unittest.mock import patch

from backend.evaluation.data_scope import build_answer_data_scope
from backend.reports import effort
from backend.sources import mantis_store
from test_effort_delivery import issue


class EffortCompleteCollectionTests(TestCase):
    def test_expired_full_snapshot_keeps_all_effort_and_discloses_its_original_time(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(mantis_store, 'store_path', return_value=Path(directory) / 'complete.sqlite3'), \
             patch.object(mantis_store, 'connection_key', return_value='effort-test'):
            rows = [issue(1, '2026-10-01', 5), issue(2, '2026-10-13', 1.5)]
            mantis_store.sync_complete_store(lambda page: rows if page == 1 else [], workers=1)
            with closing(sqlite3.connect(mantis_store.store_path())) as db, db:
                meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
                meta['fetchedAt'] = '2026-10-01T01:00:00+00:00'
                db.execute('UPDATE metadata SET value=?', (json.dumps(meta),))
            # 라이브 앞부분에 하나만 있어도 전체 저장본의 나머지 공수가 사라지면 안 된다.
            with patch.object(effort, 'fetch_mantis_issues', side_effect=lambda p: rows[:1] if p['page'] == 1 else []):
                report = effort.run_person_effort_report('10월 석지원 공수', '석지원', 2026, 10)
                filtered = effort.run_filtered_effort_report('10월 공수', 2026, 10, {})
                ranking = effort.run_effort_ranking_report('10월 공수 순위', 2026, 10)
            self.assertEqual(report['effortChart']['totalEffort'], 6.5)
            self.assertEqual(report['effortChart']['issueCount'], 2)
            self.assertEqual(sum(p['amount'] for p in report['effortChart']['dailyEffort']), 6.5)
            self.assertEqual(filtered['effortChart']['totalEffort'], 24.5)
            self.assertEqual(next(p['amount'] for p in ranking['effortRanking']['topPeople'] if p['label'] == '석지원'), 6.5)
            for result, key in [(report, 'effortChart'), (filtered, 'effortChart'), (ranking, 'effortRanking')]:
                self.assertTrue(result[key]['complete'])
                self.assertEqual(result[key]['asOf'], meta['fetchedAt'])
                self.assertIn('최신 변경', result[key]['freshnessWarning'])
                self.assertIn('저장', result['content'])
                scope = build_answer_data_scope(result['retrieval'])[0]
                self.assertEqual(scope['coverage'], '저장본 전체 집계 완료')
                self.assertTrue(scope['warning'])
                self.assertEqual(scope['asOf'], meta['fetchedAt'])
            # 일반 검색의 최신성 계약은 그대로 유지한다.
            self.assertFalse(mantis_store.read_store_metadata(allow_stale=True)['search_complete'])

    def test_live_scan_includes_deliveries_after_the_old_fifty_page_limit(self):
        def page(params):
            number = params['page']
            if number > 51:
                return []
            return [issue(number, '2026-10-01' if number == 51 else '2025-01-01', 1.25)]

        with patch.object(mantis_store, 'stream_complete_store', side_effect=lambda **kw: nullcontext(None)), \
             patch.object(mantis_store, 'read_store_metadata', return_value=None), \
             patch.object(effort, 'fetch_mantis_issues', side_effect=page):
            report = effort.run_person_effort_report('10월 석지원 공수', '석지원', 2026, 10)
        self.assertIsNotNone(report['effortChart'])
        self.assertEqual(report['effortChart']['totalEffort'], 1.25)
        self.assertTrue(report['effortChart']['complete'])
        self.assertTrue(report['retrieval']['stages'][0]['meta']['search_complete'])

    def test_repeated_live_pages_never_become_an_exact_total(self):
        with patch.object(mantis_store, 'stream_complete_store', side_effect=lambda **kw: nullcontext(None)), \
             patch.object(mantis_store, 'read_store_metadata', return_value=None), \
             patch.object(effort, 'fetch_mantis_issues', return_value=[issue(1, '2026-10-01', 2)]):
            report = effort.run_person_effort_report('10월 석지원 공수', '석지원', 2026, 10)
        self.assertEqual(report['effortChart']['totalEffort'], 2)
        self.assertFalse(report['effortChart']['complete'])
        self.assertFalse(report['retrieval']['stages'][0]['meta']['search_complete'])
