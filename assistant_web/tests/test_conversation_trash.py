"""삭제한 대화 보관함: 30일 보관·복원·영구 삭제와 사용자 경계를 확인한다."""
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend import workspace
from backend.main import app


class ConversationTrashTests(TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'workspace.sqlite3'
        self.enterContext(patch.dict(os.environ, {'WORKSPACE_DB_PATH': str(self.path), 'FIREBASE_AUTH_ENABLED': 'false'}))
        self.enterContext(patch('backend.api.workspace.current_request_user', return_value={'uid': 'a'}))
        self.client = self.enterContext(TestClient(app))

    def save(self, uid, cid, messages=1, favorite=False):
        workspace.save_conversation(uid, cid, f'{cid} 제목', [{'role': 'user', 'content': f'{cid}-{i}'} for i in range(messages)], favorite)

    def age_trash(self, cid, days):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE conversation_trash SET deleted_at=strftime('%Y-%m-%dT%H:%M:%f+00:00','now',?) WHERE conversation_id=?",
                       (f'-{days} days', cid))

    def test_deleted_conversation_is_listed_with_retention_and_restored_to_the_top(self):
        self.save('a', 'old', messages=3, favorite=True)
        self.save('a', 'newer')
        self.assertEqual(self.client.delete('/api/workspace/conversations/old').json(), {'deleted': True})

        trash = self.client.get('/api/workspace/conversations/trash')
        self.assertEqual(trash.status_code, 200, trash.text)
        body = trash.json()
        self.assertEqual(body['retentionDays'], 30)
        self.assertEqual([(item['id'], item['title'], item['messageCount']) for item in body['conversations']], [('old', 'old 제목', 3)])
        self.assertTrue(body['conversations'][0]['purgeAt'] > body['conversations'][0]['deletedAt'][:19])
        self.assertNotIn('old', self.client.get('/api/workspace/conversations').json()['conversationIds'])

        restored = self.client.post('/api/workspace/conversations/old/restore')
        self.assertEqual(restored.status_code, 200, restored.text)
        conversation = restored.json()['conversation']
        self.assertEqual((conversation['id'], conversation['favorite'], conversation['messageCount']), ('old', True, 3))
        self.assertEqual(self.client.get('/api/workspace/conversations/trash').json()['conversations'], [])
        self.assertEqual(self.client.get('/api/workspace/conversations').json()['conversationIds'][0], 'old')
        # 복원한 대화는 다시 평소처럼 저장된다.
        saved = workspace.save_conversation('a', 'old', 'old 제목', [{'role': 'user', 'content': '이어서'}], True,
                                            expected_updated_at=conversation['updatedAt'], start_index=3)
        self.assertEqual(saved['messageCount'], 4)

    def test_restore_and_purge_stay_inside_the_signed_in_user(self):
        self.save('b', 'private')
        workspace.delete_conversation('b', 'private')
        self.assertEqual(self.client.post('/api/workspace/conversations/private/restore').status_code, 404)
        self.assertEqual(self.client.post('/api/workspace/conversations/trash/purge', json={'all': True}).json()['purgedCount'], 0)
        self.assertEqual([item['id'] for item in workspace.list_conversation_trash('b')['conversations']], ['private'])
        self.assertEqual(self.client.post('/api/workspace/conversations/..%2Fx/restore').status_code, 404)

    def test_permanent_delete_removes_messages_for_selected_or_all(self):
        for cid in ('one', 'two', 'three'):
            self.save('a', cid, messages=2)
        workspace.delete_conversations('a', [], all_conversations=True)
        purged = self.client.post('/api/workspace/conversations/trash/purge', json={'ids': ['two', 'absent']})
        self.assertEqual(purged.json(), {'purgedIds': ['two'], 'purgedCount': 1})
        self.assertEqual(sorted(item['id'] for item in workspace.list_conversation_trash('a')['conversations']), ['one', 'three'])
        self.assertEqual(self.client.post('/api/workspace/conversations/trash/purge', json={'all': True}).json()['purgedCount'], 2)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_messages WHERE user_uid='a'").fetchone()[0], 0)
        for payload in ({}, {'ids': []}, {'all': True, 'ids': ['one']}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post('/api/workspace/conversations/trash/purge', json=payload).status_code, 422)

    def test_expired_and_overflowing_trash_is_purged_with_messages(self):
        for cid in ('expired', 'kept'):
            self.save('a', cid, messages=2)
            workspace.delete_conversation('a', cid)
        self.age_trash('expired', 31)
        self.age_trash('kept', 29)
        self.assertEqual([item['id'] for item in workspace.list_conversation_trash('a')['conversations']], ['kept'])
        self.assertEqual(self.client.post('/api/workspace/conversations/expired/restore').status_code, 404)
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_messages WHERE conversation_id='expired'").fetchone()[0], 0)

        with patch.dict(os.environ, {'CONVERSATION_TRASH_MAX_PER_USER': '2'}):
            for cid in ('c1', 'c2', 'c3'):
                self.save('a', cid)
                workspace.delete_conversation('a', cid)
            # 보관 개수를 넘으면 가장 먼저 지운 대화부터 영구 삭제한다.
            self.assertEqual([item['id'] for item in workspace.list_conversation_trash('a')['conversations']], ['c3', 'c2'])

    def test_restore_respects_the_conversation_count_limit(self):
        self.save('a', 'deleted')
        workspace.delete_conversation('a', 'deleted')
        with patch.dict(os.environ, {'CONVERSATION_MAX_PER_USER': '1'}):
            self.save('a', 'current')
            response = self.client.post('/api/workspace/conversations/deleted/restore')
        self.assertEqual(response.status_code, 413, response.text)
        self.assertEqual([item['id'] for item in workspace.list_conversation_trash('a')['conversations']], ['deleted'])

    def test_trashed_messages_do_not_count_toward_the_storage_quota(self):
        big = 'x' * 29000
        workspace.save_conversation('a', 'big', '큰 대화', [{'role': 'user', 'content': big} for _ in range(40)], False)
        with patch.dict(os.environ, {'CONVERSATION_MAX_TOTAL_BYTES_PER_USER': '1000000'}):
            # 보관 전에는 1MB 제한을 넘는다.
            with self.assertRaises(workspace.ConversationQuotaError):
                workspace.save_conversation('a', 'blocked', '새 대화', [{'role': 'user', 'content': '질문'}], False)
            workspace.delete_conversation('a', 'big')
            saved = workspace.save_conversation('a', 'new', '새 대화', [{'role': 'user', 'content': '질문'}], False)
        self.assertEqual(saved['messageCount'], 1)
