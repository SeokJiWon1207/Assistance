"""전체 원본의 게시/격리/실패 보존과 실제 검색 경로 회귀."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from threading import Event
from unittest.mock import patch
from datetime import datetime
import requests

from backend.sources import mantis, mantis_store
from backend.sources.mantis_collection import collection_metadata, _collect
from backend import workspace_data


class MantisCompleteStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(workspace_data.clear_workspace_snapshot_cache)
        self.addCleanup(workspace_data.clear_workspace_live_cache)
        self.path = Path(self.tmp.name) / 'complete.sqlite3'
        for target, value in [('store_path', self.path), ('connection_key', 'connection-a')]:
            mock = patch.object(mantis_store, target, return_value=value)
            mock.start()
            self.addCleanup(mock.stop)

    def sync(self, rows):
        return mantis_store.sync_complete_store(
            lambda page: rows[(page - 1)*2:page*2], page_size=2, max_pages=5, workers=2,
        )

    def test_complete_snapshot_includes_old_open_issue_before_display_limit(self):
        self.sync([{'id': 9, 'summary': '종료'}, {'id': 3, 'summary': '옛 미종료', 'handler': {'real_name': '김지원'}, 'notes': [{'text': None}]}])
        with patch.object(workspace_data, 'fetch_mantis_issues') as live:
            rows = workspace_data.fetch_workspace_recent_issues(1)
        self.assertEqual([row['id'] for row in rows], [9, 3])
        self.assertTrue(collection_metadata(rows)['search_complete'])
        live.assert_not_called()
        with patch.object(mantis, '_fetch_mantis_raw_issues') as scan:
            stage = mantis.execute_mantis_page_scan('옛', limit=1)
        self.assertEqual(stage['issues_list'][0]['id'], 3)
        self.assertTrue(stage['meta']['search_complete'])
        scan.assert_not_called()

    def test_error_duplicate_and_limit_preserve_previous_generation(self):
        self.sync([{'id': 1}])
        original = self.path.read_bytes()
        for fetch, error in [
            (lambda page: [{'id': 2}, {'id': 2}] if page == 1 else [], RuntimeError),
            (lambda page: [{'id': page*2}, {'id': page*2+1}], RuntimeError),
        ]:
            with self.assertRaises(error):
                mantis_store.sync_complete_store(fetch, page_size=2, max_pages=2)
            self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaises(RuntimeError):
            mantis_store.sync_complete_store(lambda page: (_ for _ in ()).throw(RuntimeError('offline')))
        self.assertEqual(self.path.read_bytes(), original)

    def test_connection_change_never_serves_previous_account_data(self):
        self.sync([{'id': 1}])
        with patch.object(mantis_store, 'connection_key', return_value='connection-b'):
            self.assertIsNone(mantis_store.read_complete_store(allow_stale=True))

    def test_stale_snapshot_keeps_original_time_and_is_not_complete(self):
        self.sync([{'id': 1}])
        with sqlite3.connect(self.path) as db:
            meta = json.loads(db.execute('SELECT value FROM metadata').fetchone()[0])
            meta['fetchedAt'] = '2020-01-01T00:00:00+00:00'
            db.execute('UPDATE metadata SET value=?', (json.dumps(meta),))
        self.assertIsNone(mantis_store.read_complete_store())
        stored = mantis_store.read_complete_store(allow_stale=True)
        self.assertFalse(stored.meta['search_complete'])
        self.assertEqual(stored.meta['fetchedAt'], meta['fetchedAt'])

    def test_short_page_completeness_differs_from_full_limit(self):
        partial = _collect(lambda page: [{'id': page}], 2, 1, 2)
        full = _collect(lambda page: [{'id': 1}] if page == 1 else [], 2, 1, 2)
        self.assertFalse(collection_metadata(partial)['search_complete'])
        self.assertTrue(collection_metadata(full)['search_complete'])

    def test_expired_team_count_does_not_fall_back_to_old_chroma(self):
        from backend.reports.aggregate import build_mantis_count_report
        self.sync([{'id': 77, 'handler': {'real_name': '석지원'}, 'status': {'name': 'assigned'}, 'created_at': '2026-09-10'}])
        with patch.object(mantis_store, 'MAX_AGE_SECONDS', -1), \
             patch.object(mantis_store, 'start_store_refresh'), \
             patch('backend.tools.mantis_snapshot._scan_chroma_documents') as legacy:
            response = build_mantis_count_report('우리 팀 미종료 이슈 몇 건이야', {'operation': 'count', 'source': 'mantis'})
        legacy.assert_not_called()
        self.assertIn('갱신이 지연', response['content'])
        self.assertEqual(response['retrieval']['stages'][0]['status'], 'error')

        # 새 세대에서는 동일 명단으로 정상 집계한다.
        response = build_mantis_count_report('우리 팀 미종료 이슈 몇 건이야', {'operation': 'count', 'source': 'mantis'})
        self.assertIn('총 1건', response['content'])
        self.assertEqual(response['retrieval']['stages'][0]['meta']['tool'], 'mantis_complete_snapshot')
        dated = build_mantis_count_report('우리 팀 2026년 9월 등록된 미종료 이슈 몇 건이야', {'operation': 'count', 'source': 'mantis'})
        self.assertIn('총 1건', dated['content'])
        self.assertIn('전체 저장본', dated['content'])
        self.assertNotIn('실시간 병합', dated['content'])

    def test_expired_regular_search_requests_refresh_without_using_older_index(self):
        from backend.tools import mantis_snapshot
        self.sync([{'id': 77, 'summary': '옛 제목'}])
        with (patch.object(mantis_store, 'MAX_AGE_SECONDS', -1),
              patch.object(mantis_store, 'start_store_refresh') as refresh,
              patch.object(mantis_snapshot, '_scan_chroma_documents') as legacy):
            stage = mantis.execute_mantis_snapshot_search(2, {'customer_key': 'HMC증권'})
        refresh.assert_called_once()
        legacy.assert_not_called()
        self.assertEqual(stage['status'], 'error')
        self.assertEqual(stage['issues_list'], [])
        self.assertIn('갱신', stage['reason'])

        self.sync([{'id': 78, 'summary': '새 제목'}])
        with patch.object(mantis_store, 'start_store_refresh') as refresh:
            stage = mantis.execute_mantis_snapshot_search(2, {})
        refresh.assert_not_called()
        self.assertTrue(stage['meta']['search_complete'])
        self.assertEqual([row['id'] for row in stage['issues_list']], [78])

    def test_concurrent_refresh_requests_share_one_worker_and_can_retry(self):
        entered, release = Event(), Event()
        def sync():
            entered.set()
            release.wait(5)
        with (patch.object(mantis_store, '_refresh_thread', None),
              patch.object(mantis_store, 'sync_complete_store', side_effect=sync) as collect):
            first = mantis_store.start_store_refresh()
            try:
                self.assertTrue(entered.wait(2))
                self.assertIs(mantis_store.start_store_refresh(), first)
                collect.assert_called_once()
            finally:
                release.set()
                first.join(5)
            second = mantis_store.start_store_refresh()
            second.join(5)
            self.assertIsNot(first, second)
            self.assertEqual(collect.call_count, 2)

    def test_structured_search_uses_same_new_generation(self):
        from backend.config import CHROMA_DB_PATH
        from backend.common import normalize_chroma_issue_document
        from backend.tools.mantis_snapshot import scan_mantis_documents
        self.sync([{'id': 77, 'summary': '새로운 제목', 'status': {'name': 'assigned'},
                    'handler': {'real_name': '김지원'}, 'created_at': '2026-09-29'}])
        result = scan_mantis_documents(CHROMA_DB_PATH, lambda text: '새로운 제목' in text, normalize_chroma_issue_document)
        self.assertEqual(result['matched_ids'], {77})
        self.assertTrue(result['search_complete'])

    def test_duplicate_first_pass_recovers_after_two_consistent_passes(self):
        attempt = 0
        def fetch(page):
            nonlocal attempt
            if page == 1:
                attempt += 1
            if attempt == 1:
                return {1: [{"id": 3}, {"id": 2}], 2: [{"id": 2}]}.get(page, [])
            return {1: [{"id": 3}, {"id": 2}], 2: [{"id": 1}]}.get(page, [])
        result = mantis_store.sync_complete_store(fetch, page_size=2, workers=1, max_pages=3)
        self.assertEqual(result['passes'], 3)
        self.assertEqual([row['id'] for row in mantis_store.read_complete_store()], [3, 2, 1])

    def test_deletion_shift_cannot_publish_the_incomplete_first_pass(self):
        attempt = 0
        def fetch(page):
            nonlocal attempt
            if page == 1:
                attempt += 1
            ids = [6, 5, 3, 2, 1] if attempt == 1 else [5, 4, 3, 2, 1]
            return [{'id': value} for value in ids[(page-1)*2:page*2]]
        result = mantis_store.sync_complete_store(fetch, page_size=2, workers=1, max_pages=4)
        self.assertEqual(result['passes'], 3)
        self.assertEqual([row['id'] for row in mantis_store.read_complete_store()], [5, 4, 3, 2, 1])

    def test_continual_changes_leave_previous_snapshot_untouched(self):
        self.sync([{'id': 1}])
        before = self.path.read_bytes()
        attempt = 0
        def fetch(page):
            nonlocal attempt
            if page == 1:
                attempt += 1
            return [{'id': attempt + 1, 'summary': str(attempt)}] if page == 1 else []
        with self.assertRaisesRegex(RuntimeError, '계속 변경'):
            mantis_store.sync_complete_store(fetch, page_size=2, workers=1, max_pages=2)
        self.assertEqual(attempt, 3)
        self.assertEqual(before, self.path.read_bytes())

    def test_content_updates_do_not_require_a_quiet_workspace(self):
        attempt = 0
        def fetch(page):
            nonlocal attempt
            if page == 1:
                attempt += 1
            return [{'id': 1, 'summary': str(attempt)}] if page == 1 else []
        result = mantis_store.sync_complete_store(fetch, page_size=2, workers=1, max_pages=2)
        self.assertEqual(result['passes'], 2)
        self.assertEqual(mantis_store.read_complete_store()[0]['summary'], '2')

    def test_transient_failed_pass_is_retried_within_budget(self):
        attempts = 0
        def fetch(page):
            nonlocal attempts
            if page == 1:
                attempts += 1
                if attempts == 1:
                    raise requests.Timeout()
            return [{'id': 1}] if page == 1 else []
        result = mantis_store.sync_complete_store(fetch, page_size=2, workers=1, max_pages=2)
        self.assertEqual(result['passes'], 3)

    def test_read_failure_is_not_cached(self):
        self.sync([{'id': 1}])
        with (patch.object(workspace_data, 'store_path', return_value=self.path),
              patch.object(workspace_data, 'connection_key', return_value='connection-a'),
              patch.object(workspace_data, 'read_complete_store', side_effect=[None, mantis_store.read_complete_store()]) as read):
            self.assertIsNone(workspace_data.load_complete_workspace())
            self.assertIsNotNone(workspace_data.load_complete_workspace())
            self.assertEqual(read.call_count, 2)

    def test_refresh_retains_complete_generation_until_atomic_replacement(self):
        from backend.reports import team_brief
        self.sync([{'id': 9}, {'id': 1, 'summary': '오래된 미종료'}])
        with (patch.object(workspace_data, 'store_path', return_value=self.path),
              patch.object(workspace_data, 'connection_key', return_value='connection-a'),
              patch.object(workspace_data, 'start_store_refresh') as refresh,
              patch.object(workspace_data, 'refresh_in_progress', return_value=True),
              patch.object(team_brief, 'fetch_workspace_recent_issues') as partial):
            rows, meta = team_brief._load_issues(force_refresh=True)
            self.assertEqual([row['id'] for row in rows], [9, 1])
            self.assertTrue(meta['search_complete'])
            self.assertTrue(meta['refreshPending'])
            refresh.assert_called_once()
            partial.assert_not_called()
            self.sync([{'id': 10}, {'id': 9}, {'id': 1}])
            rows, meta = workspace_data.load_complete_workspace()
            self.assertEqual([row['id'] for row in rows], [10, 9, 1])
            self.assertTrue(meta['search_complete'])

    def test_expired_cache_is_rejected_but_refresh_keeps_valid_complete_rows(self):
        from backend.workspace import _my_mantis_issues
        self.sync([{'id': 1, 'handler': {'real_name': '사용자'}}])
        with (patch.object(workspace_data, 'store_path', return_value=self.path),
              patch.object(workspace_data, 'connection_key', return_value='connection-a'),
              patch.object(workspace_data, 'start_store_refresh') as refresh,
              patch.object(workspace_data, 'get_mantis_settings', return_value={'url':'u','token':'t'}),
              patch.object(workspace_data, 'fetch_mantis_issues', return_value=[{'id': 2, 'handler': {'real_name': '사용자'}}])):
            cached = workspace_data.load_complete_workspace()
            timestamp = datetime.fromisoformat(cached[1]['fetchedAt']).timestamp()
            with patch.object(workspace_data.time, 'time', return_value=timestamp+3601):
                self.assertIsNone(workspace_data.load_complete_workspace())
                _, rows, meta = _my_mantis_issues({'mantisUsername':'사용자'}, prefer_snapshot=True)
                self.assertEqual([row['id'] for row in rows], [2])
                self.assertEqual(meta['status'], 'live')
            _, rows, meta = _my_mantis_issues({'mantisUsername':'사용자'}, force_refresh=True)
            self.assertEqual([row['id'] for row in rows], [1])
            self.assertEqual(meta['status'], 'snapshot')
            self.assertTrue(meta['search_complete'])
            refresh.assert_called_once()

    def test_aggregate_cache_is_immutable_and_personal_copy_follows_filter(self):
        self.sync([{'id': i, 'handler': {'real_name': '사용자'}} for i in range(1, 6)])
        with (patch.object(workspace_data, 'store_path', return_value=self.path),
              patch.object(workspace_data, 'connection_key', return_value='connection-a')):
            first, _ = workspace_data.load_complete_workspace()
            with patch.object(workspace_data, 'deepcopy', wraps=workspace_data.deepcopy) as copy:
                second, _ = workspace_data.load_complete_workspace()
                copy.assert_not_called()
                personal, _ = workspace_data.load_complete_workspace(predicate=lambda row: row['id'] == 1)
                # 중첩 필드의 복사는 선택한 행 내부에서만 일어난다.
                self.assertEqual(len(personal), 1)
                self.assertTrue(copy.called)
            self.assertIs(first[0], second[0])
            with self.assertRaises(TypeError):
                first[0]['handler'] = '변조'
            with self.assertRaises(TypeError):
                first[0]['date_fields']['완료예정일'] = '2099-01-01'
            personal[0]['handler'] = '개인 복사본 변경'
            self.assertEqual(workspace_data.load_complete_workspace()[0][-1]['handler'], '사용자')

    def test_request_scope_reads_once_and_does_not_cache_across_requests(self):
        from backend import retrieval
        self.sync([{'id': 1, 'summary': 'HTS 조회', 'project': {'name': 'KB증권'}}])
        with (patch.object(mantis_store, '_read_complete_store', wraps=mantis_store._read_complete_store) as read,
              patch.object(retrieval, 'revalidate_mantis_snapshot_stage', side_effect=lambda stage, constraints: stage),
              patch.object(retrieval, 'execute_mantis_filter_search', return_value={'source':'mantis_filter', 'status':'skipped'})):
            for expected in (1, 2):
                retrieval.run_retrieval_pipeline('KB증권 HTS 최근 이슈 5개 보여줘', 5,
                                                {'mantis':True, 'vectorDb':False}, {'source':'mantis'})
                self.assertEqual(read.call_count, expected)

    def test_expired_store_and_live_failure_do_not_fall_back_to_old_metrics(self):
        from backend.workspace import _my_mantis_issues
        from backend.reports.team_brief import _load_issues
        self.sync([{'id': 1, 'handler': {'real_name': '사용자'}}])
        timestamp = datetime.fromisoformat(mantis_store.read_complete_store().meta['fetchedAt']).timestamp()
        with (patch.object(workspace_data, 'store_path', return_value=self.path),
              patch.object(workspace_data, 'connection_key', return_value='connection-a'),
              patch.object(workspace_data.time, 'time', return_value=timestamp+3601),
              patch.object(workspace_data, 'get_mantis_settings', return_value={'url':'u','token':'t'}),
              patch.object(workspace_data, 'fetch_mantis_issues', side_effect=requests.Timeout())):
            _, rows, meta = _my_mantis_issues({'mantisUsername':'사용자'})
            self.assertEqual(rows, [])
            self.assertEqual(meta['status'], 'error')
            rows, meta = _load_issues()
            self.assertEqual(rows, [])
            self.assertEqual(meta['status'], 'error')

    def test_count_report_also_shares_the_full_source_between_scan_and_count(self):
        from backend.reports.aggregate import build_mantis_count_report
        from backend.query_planning import build_query_plan
        self.sync([{'id': 1, 'created_at': '2026-09-01', 'summary': 'HTS 조회'}])
        question = '2026년 9월에 등록된 이슈 몇 개야'
        plan = build_query_plan(question, {'source': 'mantis'})
        with patch.object(mantis_store, '_read_complete_store', wraps=mantis_store._read_complete_store) as read:
            result = build_mantis_count_report(question, plan)
            self.assertIsNotNone(result)
            self.assertEqual(read.call_count, 1)


class MantisNullableTextTests(unittest.TestCase):
    def test_null_note_text_does_not_break_search_or_ranking(self):
        issue = {'id': 1, 'summary': 'HTS 조회', 'notes': [{'text': None}, {'text': '차트'}]}
        self.assertIn('차트', mantis.issue_to_search_text(issue))
        self.assertGreater(mantis.mantis_keyword_match_score(issue, ['HTS']), 0)
