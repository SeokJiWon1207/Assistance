"""요약→상세 전환, 공유 관찰 기준선, 부분 실패의 실제 회귀 경로."""
import json
import tempfile
import unittest
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from backend.execution import ExecutionBudget
from backend.suggestions import sources, service
from backend.suggestions.contracts import EvidenceBatch, SuggestionBundle
from backend.suggestions.ranking import build_suggestions
from backend.suggestions.repository import SuggestionRepository
from backend.workspace import _matches_person
from backend.workspace_notifications import collect_workspace_events


class SuggestionRegressionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.repo = SuggestionRepository(Path(temp.name) / 'workspace.sqlite3')
        self.service = service.SuggestionService(self.repo)
        self.addCleanup(self.service.jobs.stop)
        self.now = datetime.now(timezone.utc)
        self.user = {'uid': 'review', 'displayName': '김담당', 'mantisUsername': '김담당', 'approved': True}
        self.meta = {'search_complete': True, 'fetchedAt': self.now.isoformat(),
                     'cacheUntil': (self.now + timedelta(minutes=10)).isoformat()}
        self.raws = [{'id': i, 'summary': '결제 화면 수정', 'status': {'name': 'assigned'},
                     'handler': {'real_name': '김담당'}, 'updated_at': self.now.isoformat(),
                     'description': '현재 본문', 'additional_information': '추가 정보',
                     'notes': [{'id': 10, 'text': '현재 노트'}]} for i in range(1, 7)]
        self.scope = sources.current_scope_key(self.user)
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(patch.object(sources, 'get_mantis_settings', return_value={'url': 'https://mantis.test/api/rest/issues'}))
        self.stack.enter_context(patch.object(service.auth, 'get_user_by_uid', return_value=self.user))

    def collect(self, *, missing=()):
        # 실제 summary_only 저장본과 동일하게 본문/노트 필드를 제외한다.
        rows = [sources.normalize_issue({k: v for k, v in raw.items()
                 if k not in {'description', 'additional_information', 'notes'}}) for raw in self.raws if raw['id'] not in missing]
        batch = EvidenceBatch()
        with patch.object(sources, 'load_complete_workspace', return_value=(rows, self.meta)), \
             patch('backend.storage.workspace_schema.workspace_db_path', return_value=self.repo.path), \
             patch('backend.workspace_notifications.collect_workspace_events', return_value=(
                 [{'type': 'note', 'issueId': 5, 'observedAt': self.now.isoformat()}], {})), \
             patch.object(sources, 'fetch_mantis_issue_by_id', side_effect=lambda i: self.raws[i - 1]):
            sources._collect_mantis(self.user, self.now, batch)
        return batch

    def publish(self, items):
        lease = self.repo.claim(self.user['uid'], self.scope, self.now)
        self.assertIsNotNone(lease)
        self.repo.publish(lease, SuggestionBundle(items=items, state='ready'), self.now)
        self.repo.release(lease, self.now, retry=True)

    def test_note_promoted_fifth_candidate_and_reserve_prepare_without_false_410(self):
        items = build_suggestions(self.collect(), now=self.now, limit=24)
        self.assertEqual(items[0].sources[0].id, '5')
        self.publish(items)
        with patch.object(sources, 'fetch_mantis_issue_by_id', side_effect=lambda i: self.raws[i - 1]):
            for item in items:
                self.assertIn('Mantis #', self.service.prepare(self.user, item.id, self.now)['prompt'])

    def test_dismissal_survives_moving_into_detail_window(self):
        items = build_suggestions(self.collect(), now=self.now, limit=24)
        self.publish(items)
        self.service.dismiss(self.user, items[0].id, self.now)
        self.publish(build_suggestions(self.collect(missing=(1,)), now=self.now, limit=24))
        self.assertNotIn('5', [i.sources[0].id for i in self.repo.read(self.user['uid'], self.scope, self.now).items])

    def test_real_update_removes_only_changed_recommendation(self):
        items = build_suggestions(self.collect(), now=self.now, limit=24)
        self.publish(items)
        changed = {**self.raws[4], 'updated_at': (self.now + timedelta(seconds=1)).isoformat()}
        with patch.object(sources, 'fetch_mantis_issue_by_id', return_value=changed):
            with self.assertRaises(HTTPException) as caught:
                self.service.prepare(self.user, items[0].id, self.now)
        self.assertEqual(caught.exception.status_code, 410)
        remaining = self.repo.read(self.user['uid'], self.scope, self.now).items
        self.assertEqual({i.id for i in remaining}, {i.id for i in items[1:]})

    def notion_case(self, *, grade='properties', content='#1 관련 업무'):
        page = {'id': 'a' * 32, 'title': '관련 문서', 'status': '진행', 'owner_user_ids': ['other'], 'last_edited_time_raw': 'v1'}
        full = {**page, '_verified_body': True, '_owned_segments': [{'block_id': 'b', 'text': content}], 'content': content}
        raw = {**self.raws[0], 'additional_information': 'https://www.notion.so/' + page['id']}
        m = sources._issue_candidate(sources.normalize_issue(raw), self.now, self.meta).sources[0]
        n = sources.page_candidate(full if grade == 'body' else page, self.now, relation='related').sources[0]
        self.stack.enter_context(patch.object(sources, 'fetch_mantis_issue_by_id', return_value=raw))
        self.stack.enter_context(patch.object(sources, 'resolve_notion_settings', return_value={'token': 'test', 'database_ids': ['db']}))
        self.stack.enter_context(patch.object(sources, 'resolve_notion_read_scope', return_value=({'db': 'db'}, True)))
        self.stack.enter_context(patch.object(sources, 'notion_owner', return_value=('', '')))
        return [m, n], page, full

    def test_related_properties_page_can_gain_body_without_revision_change(self):
        refs, _, full = self.notion_case()
        with patch.object(sources, '_current_page', return_value=full):
            self.assertEqual(sources.revalidate_sources(self.user, refs, budget=ExecutionBudget.create()), refs)

    def test_related_body_still_rejects_changed_content_at_same_timestamp(self):
        refs, _, full = self.notion_case(grade='body')
        full['_owned_segments'] = [{'block_id': 'b', 'text': '다른 기록'}]
        with patch.object(sources, '_current_page', return_value=full):
            with self.assertRaises(sources.SourceRevoked):
                sources.revalidate_sources(self.user, refs, budget=ExecutionBudget.create())

    def test_related_body_read_failure_is_temporary_not_revoked(self):
        refs, page, _ = self.notion_case(grade='body')
        with patch.object(sources, '_current_page', return_value=page):
            with self.assertRaises(sources.SourceUnavailable):
                sources.revalidate_sources(self.user, refs, budget=ExecutionBudget.create())

    def test_lost_direct_link_still_rejects_unrelated_body(self):
        refs, _, full = self.notion_case(content='무관한 업무')
        with patch.object(sources, 'fetch_mantis_issue_by_id', return_value=self.raws[0]), \
             patch.object(sources, '_current_page', return_value=full):
            with self.assertRaises(sources.SourceRevoked):
                sources.revalidate_sources(self.user, refs, budget=ExecutionBudget.create())

    def test_suggestions_and_workspace_share_owner_observation_set(self):
        rows = [sources.normalize_issue({**self.raws[0], 'handler': {'real_name': 'sjw (김담당)'}})]
        def snapshot(issues, meta):
            return 'connection', {i['id']: {'title': i['summary'], 'notes': {}} for i in issues}
        with patch.object(sources, 'load_complete_workspace', side_effect=lambda **kw: ([r for r in rows if kw['predicate'](r)], self.meta)), \
             patch('backend.storage.workspace_schema.workspace_db_path', return_value=self.repo.path), \
             patch('backend.workspace_notifications.read_notification_facts', side_effect=snapshot), \
             patch.object(sources, 'fetch_mantis_issue_by_id', return_value={**self.raws[0], 'handler': {'real_name': 'sjw (김담당)'}}):
            batch = EvidenceBatch()
            sources._collect_mantis(self.user, self.now, batch)
            later = {**self.meta, 'fetchedAt': (self.now + timedelta(seconds=1)).isoformat()}
            events, _ = collect_workspace_events(self.repo.path, self.user['uid'], '김담당',
                [r for r in rows if _matches_person(r['handler'], '김담당')], later)
        self.assertEqual(events, [])
        self.assertEqual(len(batch.candidates), 1)

    def test_unlinked_sources_are_not_partial_failures(self):
        candidate = sources._issue_candidate(sources.normalize_issue(self.raws[0]), self.now, self.meta)
        notion = sources.page_candidate({'id': 'a' * 32, 'title': '내 문서'}, self.now, relation='owner')
        for statuses, candidates, expected in [({'mantis': 'ok', 'notion': 'unlinked'}, [candidate], 'ready'),
                ({'mantis': 'unlinked', 'notion': 'ok'}, [notion], 'ready'),
                ({'mantis': 'ok', 'notion': 'unlinked'}, [], 'ready'),
                ({'mantis': 'unlinked', 'notion': 'unlinked'}, [], 'ready'),
                ({'mantis': 'ok', 'notion': 'unavailable'}, [candidate], 'partial')]:
            with self.subTest(statuses=statuses, count=len(candidates)):
                lease = self.repo.claim(self.user['uid'], self.scope, self.now)
                with patch.object(service, 'collect_candidates', return_value=EvidenceBatch(candidates=candidates, sourceStatus=statuses)), \
                     patch.object(service, 'select_candidates', return_value=None):
                    self.service.generate(lease, ExecutionBudget.create())
                self.repo.release(lease, self.now, retry=True)
                self.assertEqual(self.repo.read(self.user['uid'], self.scope, self.now).state, expected)

    def test_owner_filter_routing_fixture_uses_workspace_matching(self):
        path = Path(__file__).parent / 'fixtures/routing_cases.jsonl'
        cases = [json.loads(line)['suggestion_owner_filter'] for line in path.read_text().splitlines()
                 if 'suggestion_owner_filter' in json.loads(line)]
        self.assertTrue(cases)
        for case in cases:
            for handler, expected in case['handlers'].items():
                with self.subTest(handler=handler):
                    user = {**self.user, 'mantisUsername': case['person']}
                    self.assertEqual(sources._owned({'handler': handler}, user), expected)
                    self.assertEqual(_matches_person(handler, case['person']), expected)

    def test_known_owner_revocation_is_not_hidden_by_body_failure(self):
        refs, page, _ = self.notion_case(grade='body')
        refs[1].relation = 'owner'
        with patch.object(sources, 'notion_owner', return_value=('김담당', 'current-user')), \
             patch.object(sources, '_current_page', return_value=page):
            with self.assertRaises(sources.SourceRevoked):
                sources.revalidate_sources(self.user, refs, budget=ExecutionBudget.create())
