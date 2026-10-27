"""원본과 같은 세대의 검색문이 누락·별칭·동시성·메모리 회귀를 만들지 않는다."""
from contextlib import closing
import json
import sqlite3
import tempfile
import tracemalloc
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from backend.execution import execution_scope, RequestStopped
from backend.sources import mantis, mantis_store as store

TOPIC = "VB스크립트 자바스크립트 교체"
HTS = {"platform_key": "hts", "platform_aliases": ["HTS"],
       "filter_clauses": [{"field": "platform", "operator": "equals", "value": "hts"}]}


def row(number, text="", *, status="confirmed", media="HTS"):
    return {"id": number, "summary": "문의", "description": text,
            "project": {"name": "KB증권"}, "status": {"name": status}, "notes": [],
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "custom_fields": [{"field": {"name": "매체구분"}, "value": media}]}


class RawSearchProjectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / 'complete.sqlite3'
        for name, value in (("store_path", self.path), ("connection_key", "test")):
            target = patch.object(store, name, return_value=value)
            target.start()
            self.addCleanup(target.stop)

    def sync(self, rows):
        return store.sync_complete_store(lambda page: rows if page == 1 else [], page_size=len(rows)+1, workers=1)

    def search(self, topic=TOPIC, constraints=HTS, limit=5):
        return mantis.execute_mantis_page_scan(topic, limit, constraints)

    def test_nonmatching_bodies_are_not_decoded_and_rebuilt_per_question(self):
        self.sync([row(i, 'HTS 일반 문의 ' + '무관한 본문 ' * 100) for i in range(1, 41)])
        with patch.object(mantis, 'issue_to_search_text', wraps=mantis.issue_to_search_text) as build:
            result = self.search()
        self.assertEqual(result['meta']['found_count'], 0)
        self.assertEqual(result['meta']['scanned_issue_ids'], list(range(1, 41)))
        self.assertEqual(build.call_count, 0, '검색과 무관한 원본은 재조립하지 않는다')
        self.assertTrue(result['meta']['search_complete'])

    def test_notes_aliases_whitespace_casefold_and_platform_fields_are_preserved(self):
        rows = [row(1), row(2, 'HTS VBScript만 존재'), row(3, 'MTS VBScript JavaScript 교체', media='MTS'),
                row(4, 'HTS VB\tScript JAVASCRIPT 교체'), row(5, 'HTS Straße')]
        rows[0]['notes'] = [{'text': 'HTS VB Script JScript 교체'}]
        rows.append(row(6, 'HTS'))
        rows[-1]['additional_information'] = 'VBScript JavaScript 교체'
        self.sync(rows)
        self.assertEqual(self.search()['meta']['matched_issue_ids'], [1, 4, 6])
        self.assertEqual(self.search('STRASSE')['meta']['matched_issue_ids'], [5])
        self.assertEqual(self.search('VB스크립트 자바스크립트 없는단어')['meta']['found_count'], 0)

    def test_date_search_forms_and_structured_constraints_keep_same_matches(self):
        rows = [row(1, 'HTS'), row(2, 'MTS', media='MTS')]
        for item in rows:
            item['custom_fields'].append({'field': {'name': '고객사요청일'}, 'value': 1735689600000})
        self.sync(rows)
        self.assertEqual(self.search('2025년 1월')['meta']['matched_issue_ids'], [1])
        self.assertEqual(self.search('20250101')['meta']['matched_issue_ids'], [1])

    def test_incremental_refresh_changes_projection_and_raw_atomically(self):
        self.sync([row(1, 'HTS VBScript JavaScript 교체'), row(2, '일반')])
        changed = [row(1, '이제 해당 없음'), row(3, 'HTS VB Script J Script 교체')]
        store.refresh_changed_issues(lambda page: changed if page == 1 else [], page_size=10)
        self.assertEqual(self.search()['meta']['matched_issue_ids'], [3])
        self.assertEqual(self.search()['meta']['scanned_issue_ids'], [1, 2, 3])

    def test_partial_projection_falls_back_without_losing_a_result(self):
        self.sync([row(1, 'HTS VBScript JavaScript 교체'), row(2, '일반')])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM search_projection WHERE id=1')
        self.assertEqual(self.search()['meta']['matched_issue_ids'], [1])

    def test_external_payload_update_invalidates_projection(self):
        self.sync([row(1, '일반')])
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE issues SET payload=? WHERE id=1', (json.dumps(row(1, 'HTS VBScript JavaScript 교체')),))
        self.assertEqual(self.search()['meta']['matched_issue_ids'], [1])

    def test_old_projection_version_falls_back(self):
        self.sync([row(1, 'HTS VBScript JavaScript 교체')])
        with closing(sqlite3.connect(self.path)) as db, db:
            meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            meta['searchProjectionVersion'] = -1
            db.execute('UPDATE metadata SET value=?', (json.dumps(meta),))
            db.execute("UPDATE search_projection SET normalized_text='unrelated'")
        self.assertEqual(self.search()['meta']['matched_issue_ids'], [1])

    def test_utc_date_projection_cannot_hide_a_korean_month_boundary(self):
        item = row(31345, 'HTS')
        item['custom_fields'].append({'field': {'name': '고객사전달일'}, 'value': '1790780400'})
        self.sync([item])
        with closing(sqlite3.connect(self.path)) as db, db:
            meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            meta['searchProjectionVersion'] = 1
            db.execute('UPDATE metadata SET value=?', (json.dumps(meta),))
            db.execute("UPDATE search_projection SET normalized_text='hts20260930'")
        self.assertEqual(self.search('20261001')['meta']['matched_issue_ids'], [31345])

    def test_status_exclusion_count_still_includes_other_topics(self):
        self.sync([row(1, 'HTS unrelated', status='closed'), row(2, 'HTS VBScript JavaScript 교체')])
        result = self.search(constraints={**HTS, 'statuses': ['confirmed']})
        self.assertEqual(result['meta']['matched_issue_ids'], [2])
        self.assertEqual(result['meta']['status_excluded_count'], 1)

    def test_request_keeps_one_generation_across_replacement_and_parallel_scans(self):
        self.sync([row(1, 'HTS VBScript JavaScript 교체')])
        with store.store_request_scope():
            self.assertEqual(self.search()['meta']['matched_issue_ids'], [1])
            self.sync([row(2, 'HTS VBScript JavaScript 교체')])
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = [pool.submit(copy_context().run, self.search) for _ in range(2)]
                self.assertEqual([f.result()['meta']['matched_issue_ids'] for f in results], [[1], [1]])
        self.assertEqual(self.search()['meta']['matched_issue_ids'], [2])

    def test_all_match_counts_and_ranking_remain_bounded_in_memory(self):
        rows = [row(i, 'HTS VBScript JavaScript 교체 ' + '본문' * 16000) for i in range(1, 301)]
        self.sync(rows)
        del rows
        tracemalloc.start()
        try:
            result = self.search(limit=3)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(result['meta']['found_count'], 300)
        self.assertEqual(result['meta']['matched_issue_ids'], list(range(1, 301)))
        self.assertEqual([x['id'] for x in result['issues_list']], [300, 299, 298])
        self.assertLess(peak, 3 * 1024 * 1024)

    def test_deployment_upgrade_preserves_source_time_and_failure_keeps_old_file(self):
        self.sync([row(1, 'HTS VBScript JavaScript 교체')])
        with closing(sqlite3.connect(self.path)) as db, db:
            before = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            before.pop('searchProjectionVersion')
            db.execute('UPDATE metadata SET value=?', (json.dumps(before),))
        original = self.path.read_bytes()
        with patch.object(store, 'build_projection', side_effect=RuntimeError('build failed')):
            with self.assertRaises(RuntimeError):
                store.upgrade_search_projection()
        self.assertEqual(self.path.read_bytes(), original)
        result = store.upgrade_search_projection()
        self.assertEqual(result['fetchedAt'], before['fetchedAt'])
        with patch.object(mantis, 'issue_to_search_text', wraps=mantis.issue_to_search_text) as build:
            self.assertEqual(self.search('없는키워드')['meta']['found_count'], 0)
        self.assertEqual(build.call_count, 0)
        self.assertEqual(list(self.path.parent.glob('mantis-complete-*')), [])

    def test_cancellation_during_sql_id_scan_preserves_request_stop_reason(self):
        self.sync([row(i, 'unrelated') for i in range(1, 1001)])
        original = store._StoreStream._check_query_budget
        with execution_scope() as budget:
            def cancel_in_sql():
                budget.cancel()
                return original()
            with patch.object(store._StoreStream, '_check_query_budget', staticmethod(cancel_in_sql)):
                with self.assertRaises(RequestStopped) as stopped:
                    self.search()
            self.assertEqual(stopped.exception.status_code, 499)

    def test_cancelled_query_does_not_continue(self):
        self.sync([row(1, 'HTS VBScript JavaScript 교체')])
        with execution_scope() as budget:
            budget.cancel()
            with self.assertRaises(RequestStopped):
                self.search()
