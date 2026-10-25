"""긴 기록·부분 저장·마이그레이션·사용자 경계의 보존 계약."""
import json
import os
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from backend import workspace


class ConversationHistoryTests(TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(self.directory) / 'workspace.sqlite3'
        self.enterContext(patch.dict(os.environ, {'WORKSPACE_DB_PATH': str(self.database)}))

    def messages(self, count, start=0):
        return [{'role': 'user' if i % 2 == 0 else 'assistant', 'content': f'원문 메시지 {i}',
                 'auditId': f'{i:032x}'} for i in range(start, start + count)]

    def test_history_over_eighty_messages_survives_reopen_and_paging(self):
        messages = self.messages(164)
        workspace.save_conversation('a', 'long', '첫 질문', messages, False)
        page = workspace.get_conversation('a', 'long')
        self.assertEqual(page['messageCount'], 164)
        self.assertEqual(page['messageOffset'], 104)
        earlier = workspace.get_conversation('a', 'long', before=page['messageOffset'])
        first = workspace.get_conversation('a', 'long', before=earlier['messageOffset'])
        restored = first['messages'] + earlier['messages'] + page['messages']
        self.assertEqual([m['content'] for m in restored], [m['content'] for m in messages])
        self.assertIsNone(workspace.get_conversation('b', 'long'))

    def test_incremental_tail_edit_keeps_prefix_and_rejects_stale_revision(self):
        saved = workspace.save_conversation('a', 'long', '첫 질문', self.messages(100), False)
        updated = workspace.save_conversation('a', 'long', '첫 질문', self.messages(2, 100), False,
            expected_updated_at=saved['updatedAt'], start_index=100)
        self.assertEqual(updated['messageCount'], 102)
        with self.assertRaises(workspace.ConversationConflictError):
            workspace.save_conversation('a', 'long', '첫 질문', [], False,
                expected_updated_at=saved['updatedAt'], start_index=0)
        self.assertEqual(workspace.get_conversation('a', 'long', before=60)['messages'][0]['content'], '원문 메시지 0')

    def test_legacy_migration_preserves_every_message_once(self):
        messages = self.messages(120)
        with closing(sqlite3.connect(self.database)) as db, db:
            workspace._create_workspace_schema(db)
            workspace._create_team_brief_schema(db)
            workspace._create_playbook_schema(db)
            db.execute('PRAGMA user_version=3')
            db.execute('INSERT INTO conversations VALUES (?,?,?,?,?,?,?)',
                       ('a','old','기존 기록',json.dumps(messages),0,'2026-01-01','2026-01-01'))
        workspace.init_workspace_db()
        workspace.init_workspace_db()
        page = workspace.get_conversation('a', 'old', before=60)
        self.assertEqual(page['messageCount'], 120)
        self.assertEqual(page['messages'][0]['content'], '원문 메시지 0')
        self.assertEqual(page['messages'][-1]['auditId'], f'{59:032x}')

    def test_quota_rejection_and_invalid_gap_do_not_destroy_existing_records(self):
        saved = workspace.save_conversation('a', 'long', '첫 질문', self.messages(100), False)
        with self.assertRaises(workspace.ConversationConflictError):
            workspace.save_conversation('a', 'long', '첫 질문', self.messages(1), False,
                expected_updated_at=saved['updatedAt'], start_index=200)
        with patch.dict(os.environ, {'CONVERSATION_MAX_TOTAL_BYTES_PER_USER':'1000000'}):
            with self.assertRaises(workspace.ConversationQuotaError):
                workspace.save_conversation('a', 'long', '첫 질문', [
                    {'role':'assistant','content':'가'*25000} for _ in range(20)], False,
                    expected_updated_at=saved['updatedAt'], start_index=100)
        self.assertEqual(workspace.get_conversation('a', 'long')['messageCount'], 100)

    def test_legacy_client_cannot_replace_a_long_history_with_a_short_window(self):
        workspace.save_conversation('a', 'long', '첫 질문', self.messages(140), False)
        with self.assertRaises(workspace.ConversationConflictError):
            workspace.save_conversation('a', 'long', '첫 질문', self.messages(60, 80), False)
        self.assertEqual(workspace.get_conversation('a', 'long')['messageCount'], 140)

    def test_authenticated_api_pages_and_chunks_keep_original_audit_ids(self):
        from fastapi.testclient import TestClient
        from backend.main import app
        with patch.dict(os.environ, {'FIREBASE_AUTH_ENABLED':'false'}), patch(
            'backend.api.workspace.current_request_user', return_value={'uid':'api-a'}), TestClient(app) as client:
            revision = ''
            for start in [0, 80]:
                result = client.put('/api/workspace/conversations/paged', json={
                    'title':'첫 질문', 'messages':self.messages(80, start), 'startIndex':start,
                    'truncate':start == 80, 'expectedUpdatedAt':revision})
                self.assertEqual(result.status_code, 200, result.text)
                revision = result.json()['conversation']['updatedAt']
            recent = client.get('/api/workspace/conversations/paged').json()['conversation']
            self.assertEqual(recent['messageCount'], 160)
            first = client.get('/api/workspace/conversations/paged?before=60').json()['conversation']
            self.assertEqual(first['messages'][0]['content'], '원문 메시지 0')
            self.assertEqual(first['messages'][0]['auditId'], '0'*32)
            with patch('backend.api.workspace.current_request_user', return_value={'uid':'other'}):
                self.assertEqual(client.get('/api/workspace/conversations/paged').status_code, 404)
