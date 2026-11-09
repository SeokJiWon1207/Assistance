"""두 원천 후보·현재 근거와 관련성 경계."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from backend.execution import ExecutionBudget
from backend.suggestions import sources
from backend.suggestions.contracts import EvidenceBatch


class SuggestionSourceTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        self.user = {'uid': 'u', 'displayName': '김담당', 'mantisUsername': '김담당'}

    def test_scope_key_does_not_discover_databases(self):
        with patch('backend.notion.discovery.resolve_notion_settings', side_effect=AssertionError('network')):
            self.assertEqual(sources.current_scope_key(self.user), sources.current_scope_key(self.user))
            self.assertNotEqual(sources.current_scope_key(self.user), sources.current_scope_key({**self.user, 'uid': 'other'}))

    def test_both_sources_are_collected_and_fail_independently(self):
        for failed in ('mantis', 'notion', ''):
            with self.subTest(failed=failed), patch.object(sources, '_collect_mantis') as mantis, patch.object(sources, '_collect_notion') as notion:
                mantis.side_effect = RuntimeError('offline') if failed == 'mantis' else None
                notion.side_effect = RuntimeError('offline') if failed == 'notion' else None
                batch = sources.collect_candidates(self.user, now=self.now, budget=ExecutionBudget.create())
                mantis.assert_called_once()
                notion.assert_called_once()
                if failed:
                    self.assertEqual(batch.sourceStatus[failed], 'unavailable')

    def test_partial_notion_body_is_not_completion_fact(self):
        page = {'id': 'a' * 32, 'title': '검증 업무', 'url': 'https://www.notion.so/' + 'a' * 32,
                'status': '진행 중', 'last_edited_time_raw': '2026-11-08T00:00:00Z',
                'content': '개발 완료. 고객 검증 요청', '_verified_body': True, 'content_incomplete': True}
        candidate = sources.page_candidate(page, self.now, relation='owner')
        self.assertEqual(candidate.sources[0].grade, 'properties')
        self.assertNotIn('passages', candidate.facts)
        page['content_incomplete'] = False
        page['_owned_segments'] = [{'text': '고객 검증 요청', 'block_id': 'block'}]
        candidate = sources.page_candidate(page, self.now, relation='owner')
        self.assertEqual(candidate.sources[0].grade, 'body')
        self.assertEqual(candidate.facts['passages'][0]['text'], '고객 검증 요청')

    def test_missing_identity_does_not_search_all_personal_pages(self):
        batch = EvidenceBatch()
        with patch.object(sources, 'resolve_notion_settings', return_value={'token': 'test', 'database_ids': ['db']}), \
             patch.object(sources, 'resolve_notion_read_scope', return_value=({'db': 'db'}, True)), \
             patch.object(sources, 'fetch_notion_users', return_value=()), \
             patch.object(sources, 'execute_notion_search') as search:
            sources._collect_notion({'uid': 'u'}, self.now, batch)
        search.assert_not_called()
        self.assertEqual(batch.sourceStatus['notion'], 'unlinked')

    def test_notion_works_without_mantis_identity(self):
        page = {'id': 'a'*32, 'title': '내 문서', 'owner_user_ids': ['owner'], 'status': '진행', 'last_edited_time_raw': 'v1'}
        batch = EvidenceBatch()
        with patch.object(sources, 'resolve_notion_settings', return_value={'token': 'test', 'database_ids': ['db']}), \
             patch.object(sources, 'resolve_notion_read_scope', return_value=({'db': 'db'}, True)), \
             patch.object(sources, 'fetch_notion_users', return_value=(('김담당', 'owner'),)), \
             patch.object(sources, 'execute_notion_search', return_value={'status': 'ok', 'notion_pages': [page]}), \
             patch.object(sources, '_current_page', return_value=page):
            sources._collect_notion({'uid': 'u', 'displayName': '김담당'}, self.now, batch)
        self.assertEqual(batch.candidates[0].sources[0].type, 'notion')

    def test_ambiguous_owner_does_not_expand_scope(self):
        with patch.object(sources, 'fetch_notion_users', return_value=(('김담당', 'one'), ('김담당', 'two'))):
            self.assertEqual(sources.notion_owner(self.user), ('', ''))

    def test_explicit_links_ignore_non_notion_and_fragments(self):
        text = 'https://evil.test/' + 'b'*32 + ' https://www.notion.so/' + 'a'*32 + '#' + 'c'*32
        self.assertEqual(sources.notion_links(text), ['a'*32])

    def test_mantis_same_day_change_invalidates_revision(self):
        raw = {'id': 1, 'summary': '업무', 'updated_at': '2026-11-09T10:00:00Z'}
        before = sources._issue_revision(sources.normalize_issue(raw))
        after = sources._issue_revision(sources.normalize_issue({**raw, 'updated_at': '2026-11-09T10:05:00Z'}))
        self.assertNotEqual(before, after)

    def test_deleted_and_forbidden_mantis_are_revoked(self):
        from fastapi import HTTPException
        from test_suggestion_storage import sample
        for result in (None, HTTPException(403, 'denied')):
            with self.subTest(result=result), patch.object(sources, 'fetch_mantis_issue_by_id',
                **({'side_effect': result} if isinstance(result, Exception) else {'return_value': result})):
                with self.assertRaises(sources.SourceRevoked):
                    sources.revalidate_sources(self.user, sample(self.now).sources, budget=ExecutionBudget.create())

    def test_missing_notion_identity_keeps_verified_related_links(self):
        from test_suggestion_ranking import candidate
        c = candidate(); c.facts['notionIds'] = ['a'*32]
        page = {'id': 'a'*32, 'title': '연결 문서', 'owner_user_ids': ['other'], 'last_edited_time_raw': 'v1'}
        batch = EvidenceBatch(candidates=[c])
        with patch.object(sources, 'resolve_notion_settings', return_value={'token': 'test', 'database_ids': ['db']}), \
             patch.object(sources, 'resolve_notion_read_scope', return_value=({'db': 'db'}, True)), \
             patch.object(sources, 'fetch_notion_users', return_value=()), \
             patch.object(sources, 'execute_notion_search') as search, \
             patch.object(sources, '_current_page', return_value=page):
            sources._collect_notion({'uid': 'u'}, self.now, batch)
        search.assert_not_called()
        self.assertEqual(batch.candidates[-1].sources[0].relation, 'related')

    def test_scope_or_ownership_change_rejects_notion_page(self):
        page = {'id': 'a'*32, 'title': '담당 문서', 'owner_user_ids': ['owner'], 'last_edited_time_raw': 'v1'}
        candidate = sources.page_candidate(page, self.now, relation='owner')
        for changed in (None, {**page, 'owner_user_ids': ['other']}, {**page, 'last_edited_time_raw': 'v2'}):
            with self.subTest(changed=changed), patch.object(sources, 'resolve_notion_settings', return_value={'token': 'test', 'database_ids': ['db']}), \
                 patch.object(sources, 'resolve_notion_read_scope', return_value=({'db': 'db'}, True)), \
                 patch.object(sources, 'notion_owner', return_value=('담당', 'owner')), \
                 patch.object(sources, '_current_page', return_value=changed):
                with self.assertRaises(sources.SourceRevoked):
                    sources.revalidate_sources(self.user, candidate.sources, budget=ExecutionBudget.create())

    def test_same_page_timestamp_with_changed_owned_body_has_new_revision(self):
        page = {'id': 'a'*32, 'title': '검증', 'last_edited_time_raw': 'same', '_verified_body': True,
                '_owned_segments': [{'block_id': 'b', 'text': '검증 예정'}]}
        changed = {**page, '_owned_segments': [{'block_id': 'b', 'text': '검증 완료'}]}
        self.assertNotEqual(sources.page_candidate(page, self.now, relation='owner').revision,
                            sources.page_candidate(changed, self.now, relation='owner').revision)

    def test_source_url_never_contains_connection_credentials(self):
        issue = sources.normalize_issue({'id': 31105, 'summary': '업무', 'status': {'name': 'assigned'}})
        with patch.object(sources, 'get_mantis_settings', return_value={'url': 'https://user:secret@mantis.test/api/rest/issues?token=hidden'}):
            item = sources._issue_candidate(issue, self.now, {})
        self.assertEqual(item.sources[0].url, 'https://mantis.test/view.php?id=31105')

    def test_revoked_mantis_detail_is_removed_from_snapshot_candidates(self):
        from fastapi import HTTPException
        raw = {'id': 31105, 'summary': '업무', 'status': {'name': 'assigned'}, 'handler': {'real_name': '김담당'}}
        batch = EvidenceBatch()
        with patch.object(sources, 'load_complete_workspace', return_value=([sources.normalize_issue(raw)], {'search_complete': True})), \
             patch.object(sources, 'get_mantis_settings', return_value={'url': 'https://mantis.test/api/rest/issues'}), \
             patch('backend.workspace_notifications.collect_workspace_events', return_value=([], {})), \
             patch.object(sources, 'fetch_mantis_issue_by_id', side_effect=HTTPException(403, 'revoked')):
            sources._collect_mantis(self.user, self.now, batch)
        self.assertFalse(batch.candidates)

    def test_child_database_setting_invalidates_cached_scope(self):
        with patch.dict('os.environ', {'NOTION_INCLUDE_CHILD_DATABASES': 'true'}):
            before = sources.current_scope_key(self.user)
        with patch.dict('os.environ', {'NOTION_INCLUDE_CHILD_DATABASES': 'false'}):
            self.assertNotEqual(before, sources.current_scope_key(self.user))
