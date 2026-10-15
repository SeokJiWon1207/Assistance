"""관리자 초기화가 기존 세션을 폐기하고 다시 변경을 강제하는지 검증한다."""
import time
import unittest
from unittest.mock import Mock, patch

from backend import auth
from backend.api import auth as api
import test_temporary_password as fixtures

TEMP = fixtures.TEMP
NEW = fixtures.NEW


class AdminPasswordResetTests(unittest.TestCase):
    setUp = fixtures.TemporaryPasswordTests.setUp

    def existing(self, approved=True):
        auth.upsert_invited_user('existing', 'existing@example.com', '기존 팀원', 'manager', '담당자', '팀', 'admin')
        if not approved:
            auth.update_user_access('existing', approved=False)

    def reset(self, uid='existing', password=TEMP):
        return self.client.post(f'/api/auth/users/{uid}/reset-password',
                                json={'temporaryPassword': password}, headers={'X-CSRF-Token': 'csrf'})

    def as_member(self, seconds=2):
        self.claims = {'uid': 'existing', 'email': 'existing@example.com', 'auth_time': int(time.time()) + seconds}

    def test_admin_reset_keeps_profile_and_requires_change_after_new_login(self):
        self.existing()
        old_time = int(time.time()) - 10
        response = self.reset()
        self.assertEqual(response.status_code, 200, response.text)
        user = response.json()['user']
        self.assertTrue(user['passwordChangeRequired'])
        self.assertEqual((user['role'], user['team'], user['mantisUsername']), ('manager', '팀', '담당자'))
        self.assertNotIn(TEMP, response.text)
        self.assertNotIn(TEMP.encode(), self.path.read_bytes())
        self.firebase.update_user.assert_called_once_with('existing', password=TEMP)
        self.firebase.revoke_refresh_tokens.assert_called_once_with('existing')
        self.as_member()
        self.claims['auth_time'] = old_time
        self.assertEqual(self.client.get('/api/auth/me').status_code, 401)
        with patch.object(api, 'create_firebase_session', return_value=('old', self.claims)):
            response = self.client.post('/api/auth/session', json={'idToken':'x'*40}, headers={'X-CSRF-Token':'csrf'})
        self.assertEqual(response.status_code, 401)
        self.as_member()
        self.assertTrue(self.client.get('/api/auth/me').json()['user']['passwordChangeRequired'])
        self.assertEqual(self.client.get('/api/models').json()['code'], 'PASSWORD_CHANGE_REQUIRED')
        provider = Mock(ok=True)
        provider.json.return_value = {'localId': 'existing'}
        with patch('requests.post', return_value=provider):
            response = self.client.post('/api/auth/change-password', json={'currentPassword':TEMP,'newPassword':NEW}, headers={'X-CSRF-Token':'csrf'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(auth.get_user_by_uid('existing')['passwordChangeRequired'])

    def test_reset_requires_admin_csrf_and_different_approved_target(self):
        self.existing()
        self.assertEqual(self.reset('missing').status_code, 404)
        self.assertEqual(self.reset('admin').status_code, 400)
        response = self.client.post('/api/auth/users/existing/reset-password', json={'temporaryPassword':TEMP})
        self.assertEqual(response.status_code, 403)
        self.as_member()
        self.assertEqual(self.reset().status_code, 403)
        self.claims = {'uid':'admin', 'email':'admin@example.com'}
        auth.update_user_access('existing', approved=False)
        self.assertEqual(self.reset().status_code, 409)
        self.assertFalse(auth.get_user_by_uid('existing')['approved'])
        self.firebase.update_user.assert_not_called()

    def test_validation_does_not_echo_password(self):
        self.existing()
        for password in ['short', {'secret':TEMP}]:
            response = self.reset(password=password)
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(TEMP, response.text)
        self.firebase.update_user.assert_not_called()
        self.assertFalse(auth.get_user_by_uid('existing')['passwordChangeRequired'])

    def test_partial_provider_failure_blocks_login_until_admin_retries(self):
        self.existing()
        self.firebase.revoke_refresh_tokens.side_effect = RuntimeError('provider unavailable')
        response = self.reset()
        self.assertEqual(response.status_code, 502)
        self.assertTrue(auth.get_user_by_uid('existing')['passwordResetPending'])
        self.as_member()
        with patch.object(api, 'create_firebase_session', return_value=('new', self.claims)):
            response = self.client.post('/api/auth/session', json={'idToken':'x'*40}, headers={'X-CSRF-Token':'csrf'})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.get('/api/auth/me').status_code, 403)
        self.claims = {'uid':'admin', 'email':'admin@example.com'}
        self.firebase.revoke_refresh_tokens.side_effect = None
        self.assertEqual(self.reset().status_code, 200)
        self.as_member()
        self.assertEqual(self.client.get('/api/models').json()['code'], 'PASSWORD_CHANGE_REQUIRED')

    def test_database_failure_before_reset_does_not_touch_firebase(self):
        self.existing()
        with patch.object(auth, 'begin_password_reset', side_effect=RuntimeError('database unavailable'), create=True):
            response = self.reset()
        self.assertEqual(response.status_code, 502)
        self.firebase.update_user.assert_not_called()

    def test_old_database_migrates_without_marking_existing_users_pending(self):
        self.existing()
        user = auth.get_user_by_uid('existing')
        self.assertFalse(user['passwordResetPending'])
        self.assertEqual(user['passwordResetVersion'], 0)

    def test_concurrent_reset_cannot_override_password_change(self):
        from backend.auth_passwords import password_operation
        self.existing()
        with password_operation('existing'):
            response = self.reset()
        self.assertEqual(response.status_code, 409)
        self.firebase.update_user.assert_not_called()

    def test_stale_password_change_request_cannot_undo_admin_reset(self):
        from backend.auth_passwords import change_temporary_password
        from fastapi import HTTPException
        self.existing()
        previous = auth.get_user_by_uid('existing')
        self.assertEqual(self.reset().status_code, 200)
        self.firebase.update_user.reset_mock()
        with self.assertRaises(HTTPException) as error:
            change_temporary_password(previous, TEMP, NEW)
        self.assertEqual(error.exception.status_code, 409)
        self.firebase.update_user.assert_not_called()
        self.assertTrue(auth.get_user_by_uid('existing')['passwordChangeRequired'])

    def test_provider_update_failure_keeps_existing_session_blocked(self):
        self.existing()
        self.firebase.update_user.side_effect = RuntimeError('provider timeout')
        self.assertEqual(self.reset().status_code, 502)
        self.as_member()
        self.assertEqual(self.client.get('/api/models').status_code, 403)
        self.assertTrue(auth.get_user_by_uid('existing')['passwordResetPending'])

    def test_final_database_failure_keeps_account_pending(self):
        self.existing()
        with patch.object(auth, 'finish_password_reset', side_effect=RuntimeError('disk full')):
            self.assertEqual(self.reset().status_code, 502)
        self.assertTrue(auth.get_user_by_uid('existing')['passwordResetPending'])

    def test_version_two_database_preserves_profile_and_pending_change(self):
        import os
        import sqlite3
        from contextlib import closing
        old = self.path.parent / 'version-two.sqlite3'
        with closing(sqlite3.connect(old)) as con, con:
            auth._create_users_schema(con)
            auth._add_password_change_fields(con)
            con.execute('PRAGMA user_version=2')
            con.execute("INSERT INTO users(firebase_uid,email,created_at,updated_at,password_change_required) VALUES ('old','old@example.com','2026','2026',1)")
        with patch.dict(os.environ, {'AUTH_USERS_DB_PATH':str(old)}):
            user = auth.get_user_by_uid('old')
            self.assertTrue(user['passwordChangeRequired'])
            self.assertFalse(user['passwordResetPending'])
            self.assertEqual(user['passwordResetVersion'], 0)
        with closing(sqlite3.connect(old)) as con:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], 3)
