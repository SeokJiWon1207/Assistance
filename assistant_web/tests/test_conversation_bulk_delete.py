"""대화 일괄 삭제의 사용자 경계·원자성·전체 범위를 확인한다."""
import os
import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend import workspace
from backend.main import app


class ConversationBulkDeleteTests(TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(folder) / 'workspace.sqlite3'
        self.enterContext(patch.dict(os.environ, {'WORKSPACE_DB_PATH': str(self.path), 'FIREBASE_AUTH_ENABLED': 'false'}))
        self.enterContext(patch('backend.api.workspace.current_request_user', return_value={'uid': 'a'}))
        self.client = self.enterContext(TestClient(app))

    def save(self, uid, cid, favorite=False):
        workspace.save_conversation(uid, cid, cid, [{'role': 'user', 'content': cid}], favorite)

    def test_selected_ids_remove_only_own_rows_and_messages(self):
        self.save('a', 'one', True)
        self.save('a', 'keep')
        self.save('b', 'one')
        response = self.client.post('/api/workspace/conversations/bulk-delete', json={'ids': ['one', 'one', 'absent']})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'deletedIds': ['one'], 'deletedCount': 1})
        self.assertIsNone(workspace.get_conversation('a', 'one'))
        self.assertIsNotNone(workspace.get_conversation('a', 'keep'))
        self.assertIsNotNone(workspace.get_conversation('b', 'one'))
        # 삭제한 대화는 보관함으로 옮겨 메시지를 남기고, 영구 삭제해야 메시지까지 지운다.
        trash = workspace.list_conversation_trash('a')['conversations']
        self.assertEqual([(item['id'], item['messageCount']) for item in trash], [('one', 1)])
        self.assertEqual(workspace.list_conversation_trash('b')['conversations'], [])
        self.assertEqual(workspace.purge_conversation_trash('a', ['one']), ['one'])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_messages WHERE user_uid='a' AND conversation_id='one'").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM conversation_messages WHERE user_uid='b' AND conversation_id='one'").fetchone()[0], 1)

    def test_all_includes_favorites_and_unloaded_history_beyond_one_hundred(self):
        for i in range(105):
            self.save('a', str(i), i % 2 == 0)
        self.save('b', 'keep')
        response = self.client.post('/api/workspace/conversations/bulk-delete', json={'all': True})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['deletedCount'], 105)
        self.assertEqual(workspace.list_conversations('a'), [])
        self.assertIsNotNone(workspace.get_conversation('b', 'keep'))
        self.assertEqual(self.client.post('/api/workspace/conversations/bulk-delete', json={'all': True}).json()['deletedCount'], 0)

    def test_failure_rolls_back_parent_and_message_deletions(self):
        self.save('a', 'one')
        self.save('a', 'two')
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON conversations WHEN old.conversation_id='two' "
                       "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            workspace.delete_conversations('a', ['one', 'two'])
        # 한 건이라도 실패하면 앞서 보관함으로 옮긴 대화까지 되돌린다.
        self.assertIsNotNone(workspace.get_conversation('a', 'one'))
        self.assertIsNotNone(workspace.get_conversation('a', 'two'))
        self.assertEqual(workspace.list_conversation_trash('a')['conversations'], [])
        with sqlite3.connect(self.path) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversation_messages').fetchone()[0], 2)

    def test_invalid_scope_never_deletes_anything(self):
        self.save('a', 'one')
        for payload in ({}, {'ids': []}, {'all': True, 'ids': ['one']}, {'ids': ['../one']}, {'ids': ['x'] * 5001}):
            with self.subTest(payload=str(payload)[:80]):
                self.assertEqual(self.client.post('/api/workspace/conversations/bulk-delete', json=payload).status_code, 422)
        self.assertIsNotNone(workspace.get_conversation('a', 'one'))

    def test_unauthenticated_request_is_rejected(self):
        with patch('backend.api.workspace.current_request_user', return_value=None):
            self.assertEqual(self.client.post('/api/workspace/conversations/bulk-delete', json={'all': True}).status_code, 401)

    def test_listing_identifies_all_own_ids_beyond_the_message_page(self):
        for cid in ('first', 'second', 'third'):
            self.save('a', cid)
        self.save('b', 'private')
        listing = self.client.get('/api/workspace/conversations?limit=1').json()
        self.assertEqual(len(listing['conversations']), 1)
        self.assertIn('conversationIds', listing)
        self.assertEqual(set(listing['conversationIds']), {'first', 'second', 'third'})
        self.client.post('/api/workspace/conversations/bulk-delete', json={'ids': ['second']})
        self.assertEqual(set(self.client.get('/api/workspace/conversations?limit=1').json()['conversationIds']), {'first', 'third'})

    def test_deleted_revision_cannot_be_saved_as_a_conflict_recovery(self):
        for bulk in (False, True):
            with self.subTest(bulk=bulk):
                cid = f'old-{int(bulk)}'
                saved = workspace.save_conversation('a', cid, '삭제할 대화', [{'role': 'user', 'content': '질문'}], False)
                if bulk:
                    self.client.post('/api/workspace/conversations/bulk-delete', json={'all': True})
                else:
                    self.client.delete(f'/api/workspace/conversations/{cid}')
                response = self.client.put(f'/api/workspace/conversations/{cid}', json={
                    'expectedUpdatedAt': saved['updatedAt'], 'messages': [{'role': 'user', 'content': '늦은 저장'}], 'startIndex': 0})
                self.assertEqual(response.status_code, 410, response.text)
                self.assertIsNone(workspace.get_conversation('a', cid))
                # 버전 정보 없이 처음 저장하는 것처럼 보내도 보관 중인 메시지를 덮지 않는다.
                fresh = self.client.put(f'/api/workspace/conversations/{cid}', json={'messages': [{'role': 'user', 'content': '덮어쓰기'}]})
                self.assertEqual(fresh.status_code, 410, fresh.text)
