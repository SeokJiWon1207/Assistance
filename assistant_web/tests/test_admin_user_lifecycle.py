"""사용자 삭제의 데이터 격리와 성공한 로그인 시각을 검증한다."""
import os
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

from backend import audit, auth
from backend.api import auth as api
from backend.storage.workspace_schema import init_workspace_db
import test_temporary_password as fixtures


class AdminUserLifecycleTests(unittest.TestCase):
    def setUp(self):
        fixtures.TemporaryPasswordTests.setUp(self)
        self.workspace = self.path.parent / 'workspace.sqlite3'
        self.audit = self.path.parent / 'audit.sqlite3'
        self.stack.enter_context(patch.dict(os.environ, {'WORKSPACE_DB_PATH': str(self.workspace)}))
        self.stack.enter_context(patch.object(audit, 'AUDIT_DB_PATH', self.audit))
        init_workspace_db()
        audit.init_audit_db()
        for uid in ('target', 'other'):
            auth.upsert_invited_user(uid, f'{uid}@example.com', uid, 'member', '', '', 'admin')
        with closing(sqlite3.connect(self.workspace)) as db, db:
            # 휴지통 도입 전·후 스키마 모두에서 삭제 기능을 독립 검증한다.
            db.execute("CREATE TABLE IF NOT EXISTS conversation_trash ("
                       "user_uid TEXT NOT NULL, conversation_id TEXT NOT NULL, title TEXT NOT NULL, "
                       "favorite INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, "
                       "updated_at TEXT NOT NULL, deleted_at TEXT NOT NULL, "
                       "PRIMARY KEY (user_uid, conversation_id))")
            for uid in ('target', 'other'):
                db.execute("INSERT INTO conversations VALUES (?, 'same', '대화', '[]', 0, 'now', 'now')", (uid,))
                db.execute("INSERT INTO conversation_messages VALUES (?, 'same', 0, '{}', 2)", (uid,))
                db.execute("INSERT INTO conversation_trash VALUES (?, 'trash', '휴지통', 0, 'now', 'now', 'now')", (uid,))
                db.execute("INSERT INTO conversation_messages VALUES (?, 'trash', 0, '{}', 2)", (uid,))
                db.execute("INSERT INTO suggestion_cache VALUES (?, 'scope', '{}', 9999999999)", (uid,))
                db.execute("INSERT INTO suggestion_dismissals VALUES (?, 'scope', 'key', 'rev', 1)", (uid,))
                db.execute("INSERT INTO suggestion_jobs VALUES (?, 'scope', 'token', 9999999999, 1)", (uid,))
                db.execute("INSERT INTO notification_reads VALUES (?, 'notice', 'now')", (uid,))
                db.execute("INSERT INTO weekly_team_brief_reads VALUES (?, 'week', 'now')", (uid,))
                db.execute("INSERT INTO workspace_observations VALUES (?, 'scope', 'now', '{}')", (uid,))
                db.execute("INSERT INTO workspace_events VALUES (?, ?, 'scope', 1, 'now', '{}')", (uid, uid))
                db.execute("INSERT INTO resolution_playbooks(id,title,symptom,resolution,created_by_uid,created_at,updated_at) "
                           "VALUES (?, '제목', '증상', '해결', ?, 'now', 'now')", (uid, uid))
            db.execute("INSERT INTO weekly_team_briefs VALUES ('week', '{}', 'now')")
        with closing(sqlite3.connect(self.audit)) as db, db:
            for uid in ('target', 'other'):
                db.execute("INSERT INTO validation_logs(id,created_at,question,model_id,mode,score,status,payload,user_uid) "
                           "VALUES (?, 'now', '질문', 'model', 'chat', 100, 'pass', '{}', ?)", (uid, uid))
                db.execute("INSERT INTO answer_feedback(id,audit_id,created_at,updated_at,rating) "
                           "VALUES (?, ?, 'now', 'now', 'good')", (uid, uid))

    def delete(self, uid='target', email=None):
        return self.client.request('DELETE', f'/api/auth/users/{uid}',
                                   json={'email': email or f'{uid}@example.com'},
                                   headers={'X-CSRF-Token': 'csrf'})

    def test_deletion_removes_only_target_owned_data_and_blocks_old_session(self):
        response = self.delete()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(auth.get_user_by_uid('target'))
        self.assertIsNotNone(auth.get_user_by_uid('other'))
        self.firebase.delete_user.assert_called_once_with('target')
        with closing(sqlite3.connect(self.workspace)) as db:
            for table in ('conversations', 'conversation_messages', 'conversation_trash', 'notification_reads',
                          'weekly_team_brief_reads', 'workspace_observations', 'workspace_events',
                          'suggestion_cache', 'suggestion_dismissals', 'suggestion_jobs'):
                self.assertEqual(db.execute(f'SELECT DISTINCT user_uid FROM {table}').fetchall(), [('other',)], table)
            self.assertEqual(db.execute('SELECT created_by_uid FROM resolution_playbooks').fetchall(), [('other',)])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM weekly_team_briefs').fetchone()[0], 1)
        with closing(sqlite3.connect(self.audit)) as db:
            self.assertEqual(db.execute('SELECT user_uid FROM validation_logs').fetchall(), [('other',)])
            self.assertEqual(db.execute('SELECT audit_id FROM answer_feedback').fetchall(), [('other',)])
        self.claims = {'uid': 'target', 'email': 'target@example.com'}
        self.assertEqual(self.client.get('/api/auth/me').status_code, 403)

    def test_delete_requires_admin_csrf_matching_email_and_other_target(self):
        self.assertEqual(self.delete('admin').status_code, 400)
        self.assertEqual(self.delete('missing').status_code, 404)
        self.assertEqual(self.delete(email='wrong@example.com').status_code, 400)
        self.assertEqual(self.client.request('DELETE', '/api/auth/users/target',
                         json={'email': 'target@example.com'}).status_code, 403)
        self.claims = {'uid': 'other', 'email': 'other@example.com'}
        self.assertEqual(self.delete().status_code, 403)
        self.firebase.delete_user.assert_not_called()
        self.assertTrue(auth.get_user_by_uid('target')['approved'])

    def test_provider_failure_keeps_data_and_blocks_account_until_retry(self):
        self.firebase.delete_user.side_effect = RuntimeError('provider down')
        self.assertEqual(self.delete().status_code, 502)
        self.assertFalse(auth.get_user_by_uid('target')['approved'])
        with closing(sqlite3.connect(self.workspace)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0], 2)
        self.firebase.delete_user.side_effect = None
        self.assertEqual(self.delete().status_code, 200)

    def test_missing_firebase_account_can_finish_deletion(self):
        self.firebase.delete_user.side_effect = self.firebase.UserNotFoundError()
        self.assertEqual(self.delete().status_code, 200)
        self.assertIsNone(auth.get_user_by_uid('target'))

    def test_deletion_also_works_before_conversation_trash_is_installed(self):
        with closing(sqlite3.connect(self.workspace)) as db, db:
            db.execute('DROP TABLE conversation_trash')
        self.assertEqual(self.delete().status_code, 200)
        self.assertIsNone(auth.get_user_by_uid('target'))
        with closing(sqlite3.connect(self.workspace)) as db:
            self.assertEqual(db.execute('SELECT DISTINCT user_uid FROM conversation_messages').fetchall(), [('other',)])

    def test_local_failure_rolls_back_all_data_and_allows_retry(self):
        with closing(sqlite3.connect(self.audit)) as db, db:
            db.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON validation_logs BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        self.assertEqual(self.delete().status_code, 503)
        self.assertFalse(auth.get_user_by_uid('target')['approved'])
        with closing(sqlite3.connect(self.workspace)) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM conversations').fetchone()[0], 2)
        with closing(sqlite3.connect(self.audit)) as db, db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM answer_feedback').fetchone()[0], 2)
            db.execute('DROP TRIGGER fail_delete')
        self.firebase.delete_user.side_effect = self.firebase.UserNotFoundError()
        self.assertEqual(self.delete().status_code, 200)

    def test_password_operation_conflict_does_not_delete_user(self):
        from backend.auth_passwords import password_operation
        with password_operation('target'):
            self.assertEqual(self.delete().status_code, 409)
        self.firebase.delete_user.assert_not_called()

    def test_active_user_request_prevents_deleting_data_still_being_written(self):
        from backend.user_data_lock import user_data_operation
        with user_data_operation('target', self.path):
            self.assertEqual(self.delete().status_code, 409)
        self.assertTrue(auth.get_user_by_uid('target')['approved'])
        self.firebase.delete_user.assert_not_called()
        self.assertEqual(self.delete().status_code, 200)

    def test_authenticated_request_holds_data_lock_until_response_finishes(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from fastapi.responses import StreamingResponse
        from backend.user_data_lock import user_data_operation
        app = FastAPI()
        app.middleware('http')(auth.authentication_middleware)

        @app.get('/api/probe')
        def probe():
            def stream():
                with self.assertRaises(BlockingIOError):
                    with user_data_operation('admin', self.path, exclusive=True):
                        pass
                yield '{"protected":true}'
            return StreamingResponse(stream(), media_type='application/json')

        with TestClient(app) as client:
            client.cookies.set('ai_session', 'session')
            self.assertEqual(client.get('/api/probe').json(), {'protected': True})
        with user_data_operation('admin', self.path, exclusive=True):
            pass

    def login(self, claims):
        with patch.object(api, 'create_firebase_session', return_value=('new-session', claims)):
            return self.client.post('/api/auth/session', json={'idToken': 'x' * 40},
                                    headers={'X-CSRF-Token': 'csrf'})

    def test_successful_login_records_time_and_me_does_not_change_it(self):
        claims = {'uid': 'target', 'email': 'target@example.com', 'auth_time': 100}
        with patch.object(auth, '_now', return_value='2026-11-05T12:34:56+09:00'):
            response = self.login(claims)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['user']['lastLoginAt'], '2026-11-05T12:34:56+09:00')
        self.claims = claims
        self.assertEqual(self.client.get('/api/auth/me').json()['user']['lastLoginAt'], '2026-11-05T12:34:56+09:00')

    def test_rejected_login_does_not_record_success_time(self):
        auth.begin_password_reset('target')
        claims = {'uid': 'target', 'email': 'target@example.com', 'auth_time': 1}
        self.assertEqual(self.login(claims).status_code, 403)
        self.assertIsNone(auth.get_user_by_uid('target')['lastLoginAt'])
        auth.finish_password_reset('target')
        self.assertEqual(self.login(claims).status_code, 401)
        self.assertIsNone(auth.get_user_by_uid('target')['lastLoginAt'])
