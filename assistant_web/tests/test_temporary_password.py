"""임시 비밀번호 생성, 첫 로그인 제한과 변경 완료 후 세션 폐기를 검증한다."""
import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from contextlib import ExitStack, closing

from fastapi.testclient import TestClient
from backend import auth
from backend.api import auth as api
from backend.main import app

TEMP = 'Temporary-Test-123!'
NEW = 'Changed-Test-456!'


class TemporaryPasswordTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = self.stack.enter_context(tempfile.TemporaryDirectory())
        self.path = Path(directory) / 'users.sqlite3'
        self.stack.enter_context(patch.dict(os.environ, {
            'AUTH_USERS_DB_PATH': str(self.path), 'FIREBASE_AUTH_ENABLED': 'true',
            'AUTH_COOKIE_SECURE': 'false', 'FIREBASE_WEB_API_KEY': 'fixture-public-key',
            'FIREBASE_BOOTSTRAP_ADMIN_EMAILS': '',
        }))
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        auth.upsert_invited_user('admin', 'admin@example.com', '관리자', 'admin', '', '', 'fixture')
        self.claims = {'uid': 'admin', 'email': 'admin@example.com', 'auth_time': int(time.time())-10}
        self.stack.enter_context(patch.object(auth, 'verify_firebase_session', side_effect=lambda _: self.claims))
        self.client.cookies.set('ai_session', 'session')
        self.client.cookies.set('ai_csrf', 'csrf')
        self.firebase = Mock()
        self.firebase.UserNotFoundError = type('UserNotFoundError', (Exception,), {})
        self.firebase.get_user_by_email.side_effect = self.firebase.UserNotFoundError()
        self.firebase.create_user.return_value = Mock(uid='new-user')
        self.firebase.generate_password_reset_link.return_value = 'https://example.test/setup'
        self.stack.enter_context(patch.object(auth, '_firebase_auth_module', return_value=self.firebase))

    def invite(self, **extra):
        return self.client.post('/api/auth/users/invite', headers={'X-CSRF-Token': 'csrf'},
            json={'email': 'new@example.com', 'displayName': '팀원', 'temporaryPassword': TEMP, **extra})

    def pending_user(self):
        response = self.invite()
        self.assertEqual(response.status_code, 200, response.text)
        self.claims = {'uid': 'new-user', 'email': 'new@example.com', 'auth_time': int(time.time())-5}

    def change(self, **extra):
        return self.client.post('/api/auth/change-password', headers={'X-CSRF-Token': 'csrf'},
            json={'currentPassword': TEMP, 'newPassword': NEW, **extra})

    def test_admin_creates_password_account_without_returning_or_storing_password(self):
        response = self.invite()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['user']['passwordChangeRequired'])
        self.assertNotIn(TEMP, response.text)
        self.assertEqual(self.firebase.create_user.call_args.kwargs['password'], TEMP)
        self.firebase.generate_password_reset_link.assert_not_called()
        self.assertNotIn(TEMP.encode(), self.path.read_bytes())
        self.assertTrue(auth.get_user_by_uid('new-user')['passwordChangeRequired'])

    def test_existing_account_is_not_overwritten(self):
        self.firebase.get_user_by_email.side_effect = None
        self.firebase.get_user_by_email.return_value = Mock(uid='existing')
        response = self.invite()
        self.assertEqual(response.status_code, 409)
        self.firebase.update_user.assert_not_called()
        self.firebase.create_user.assert_not_called()

    def test_pending_password_blocks_work_and_admin_apis_but_me_works(self):
        self.pending_user()
        for method, url in [('get','/api/models'), ('post','/api/chat'), ('get','/api/auth/users')]:
            response = getattr(self.client, method)(url, headers={'X-CSRF-Token': 'csrf'})
            self.assertEqual(response.status_code, 403, response.text)
            self.assertEqual(response.json()['code'], 'PASSWORD_CHANGE_REQUIRED')
        self.assertTrue(self.client.get('/api/auth/me').json()['user']['passwordChangeRequired'])
        with patch.object(api, 'create_firebase_session', return_value=('pending-cookie', self.claims)):
            response = self.client.post('/api/auth/session', headers={'X-CSRF-Token':'csrf'}, json={'idToken':'x'*40})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['user']['passwordChangeRequired'])

    def test_password_change_verifies_current_password_and_revokes_old_sessions(self):
        self.pending_user()
        provider = Mock(ok=True)
        provider.json.return_value = {'localId': 'new-user'}
        with patch('requests.post', return_value=provider):
            response = self.change()
        self.assertEqual(response.status_code, 200, response.text)
        self.firebase.update_user.assert_called_once_with('new-user', password=NEW)
        self.firebase.revoke_refresh_tokens.assert_called_once_with('new-user')
        self.assertFalse(auth.get_user_by_uid('new-user')['passwordChangeRequired'])
        self.client.cookies.set('ai_session','old-session')
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)
        with patch.object(api, 'create_firebase_session', return_value=('old-cookie', self.claims)):
            response = self.client.post('/api/auth/session', headers={'X-CSRF-Token':'csrf'}, json={'idToken':'x'*40})
        self.assertEqual(response.status_code, 401)
        self.claims['auth_time'] = int(time.time())+2
        self.client.cookies.set('ai_session','fresh-session')
        self.assertEqual(self.client.get('/api/auth/me').status_code, 200)

    def test_wrong_current_password_and_provider_failure_keep_user_restricted(self):
        self.pending_user()
        provider = Mock(ok=False, status_code=400)
        with patch('requests.post', return_value=provider):
            response = self.change()
        self.assertEqual(response.status_code, 400)
        self.firebase.update_user.assert_not_called()
        provider.ok = True
        provider.json.return_value = {'localId': 'different-user'}
        with patch('requests.post', return_value=provider):
            self.assertEqual(self.change().status_code, 400)
        self.assertTrue(auth.get_user_by_uid('new-user')['passwordChangeRequired'])

    def test_same_password_missing_csrf_and_revoke_failure_do_not_complete_change(self):
        self.pending_user()
        self.assertEqual(self.change(newPassword=TEMP).status_code, 422)
        response = self.client.post('/api/auth/change-password', json={'currentPassword': TEMP,'newPassword':NEW})
        self.assertEqual(response.status_code, 403)
        provider = Mock(ok=True)
        provider.json.return_value = {'localId':'new-user'}
        self.firebase.revoke_refresh_tokens.side_effect = RuntimeError('provider unavailable')
        with patch('requests.post', return_value=provider):
            self.assertEqual(self.change().status_code, 502)
        self.assertTrue(auth.get_user_by_uid('new-user')['passwordChangeRequired'])

    def test_validation_errors_do_not_echo_password_inputs(self):
        for payload in ({'temporaryPassword': 'short'}, {'temporaryPassword': {'secret':TEMP}}):
            response = self.invite(**payload)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(TEMP, response.text)
            self.assertNotIn('short', response.text)

    def test_only_admin_can_create_temporary_password_accounts(self):
        self.client.cookies.clear()
        self.assertEqual(self.invite().status_code, 401)
        auth.upsert_invited_user('member', 'member@example.com', '', 'member', '', '', 'fixture')
        self.claims = {'uid':'member', 'email':'member@example.com'}
        self.client.cookies.set('ai_session','session')
        self.client.cookies.set('ai_csrf','csrf')
        self.assertEqual(self.invite().status_code, 403)
        self.firebase.create_user.assert_not_called()

    def test_legacy_invite_and_access_updates_cannot_clear_required_change(self):
        self.pending_user()
        auth.upsert_invited_user('new-user', 'new@example.com', '다시 초대', 'member', '', '', 'admin')
        auth.update_user_access('new-user', approved=True, role='admin')
        self.assertTrue(auth.get_user_by_uid('new-user')['passwordChangeRequired'])
        response = self.client.get('/api/auth/users')
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'PASSWORD_CHANGE_REQUIRED')

    def test_change_password_validation_does_not_echo_inputs(self):
        self.pending_user()
        response = self.change(newPassword={'secret':NEW})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn(NEW, response.text)
        self.assertNotIn(TEMP, response.text)

    def test_provider_network_failure_keeps_change_required(self):
        self.pending_user()
        import requests
        with patch('requests.post', side_effect=requests.Timeout()):
            response = self.change()
        self.assertEqual(response.status_code, 502)
        self.firebase.update_user.assert_not_called()
        self.assertTrue(auth.get_user_by_uid('new-user')['passwordChangeRequired'])

    def test_local_save_failure_removes_new_firebase_account_for_retry(self):
        original_upsert = auth.upsert_invited_user

        def fail_after_save(*args, **kwargs):
            original_upsert(*args, **kwargs)
            raise sqlite3.OperationalError('fixture failure after commit')

        with patch.object(api, 'upsert_invited_user', side_effect=fail_after_save):
            response = self.invite()
        self.assertEqual(response.status_code, 503)
        self.firebase.delete_user.assert_called_once_with('new-user')
        self.assertIsNone(auth.get_user_by_uid('new-user'))
        self.assertEqual(self.invite().status_code, 200)

    def test_failed_provisioning_cleanup_reports_manual_recovery(self):
        self.firebase.delete_user.side_effect = RuntimeError('provider unavailable')
        with patch.object(api, 'upsert_invited_user', side_effect=sqlite3.OperationalError('disk full')):
            response = self.invite()
        self.assertEqual(response.status_code, 503)
        self.assertIn('인증 서비스', response.json()['detail'])
        self.assertNotIn(TEMP, response.text)

    def test_user_database_migration_preserves_existing_users_without_forcing_change(self):
        # 실제 구버전 DB를 새 코드가 열어도 기존 계정의 로그인 정책은 유지한다.
        legacy = self.path.parent / 'legacy.sqlite3'
        with closing(sqlite3.connect(legacy)) as connection, connection:
            auth._create_users_schema(connection)
            connection.execute("PRAGMA user_version=1")
            connection.execute("INSERT INTO users(firebase_uid,email,created_at,updated_at) VALUES ('old','old@example.com','2026','2026')")
        with patch.dict(os.environ, {'AUTH_USERS_DB_PATH':str(legacy)}):
            self.assertFalse(auth.get_user_by_uid('old')['passwordChangeRequired'])
