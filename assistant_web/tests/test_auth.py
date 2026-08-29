import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend import auth as auth_module
from backend.api import auth as auth_api
from backend.main import app


class AuthUserStoreTests(unittest.TestCase):
    def test_invited_user_can_be_updated_and_listed(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"AUTH_USERS_DB_PATH": str(Path(directory) / "users.sqlite3")},
        ):
            invited = auth_module.upsert_invited_user(
                "firebase-1",
                "USER@EXAMPLE.COM",
                "테스트 사용자",
                "member",
                "mantis-user",
                "컨텐츠서비스",
                "admin@example.com",
            )
            self.assertEqual(invited["email"], "user@example.com")
            self.assertEqual(invited["role"], "member")
            self.assertTrue(invited["approved"])

            updated = auth_module.update_user_access(
                "firebase-1", role="manager", approved=False
            )
            self.assertEqual(updated["role"], "manager")
            self.assertFalse(updated["approved"])
            self.assertEqual(len(auth_module.list_users()), 1)

    def test_bootstrap_admin_is_approved_on_first_login(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "AUTH_USERS_DB_PATH": str(Path(directory) / "users.sqlite3"),
                "FIREBASE_BOOTSTRAP_ADMIN_EMAILS": "admin@example.com",
            },
        ):
            user = auth_module.approve_login_claims(
                {"uid": "admin-uid", "email": "ADMIN@example.com", "name": "관리자"}
            )
            self.assertIsNotNone(user)
            self.assertEqual(user["role"], "admin")
            self.assertEqual(user["invitedBy"], "bootstrap")

    def test_token_verification_allows_small_clock_skew(self):
        firebase_auth = Mock()
        firebase_auth.verify_id_token.return_value = {
            "uid": "user-1",
            "auth_time": int(time.time()),
        }
        firebase_auth.create_session_cookie.return_value = "session-cookie"
        firebase_auth.verify_session_cookie.return_value = {"uid": "user-1"}

        with patch.object(
            auth_module, "_firebase_auth_module", return_value=firebase_auth
        ), patch.dict(os.environ, {"FIREBASE_CLOCK_SKEW_SECONDS": "10"}):
            cookie, claims = auth_module.create_firebase_session("id-token")
            verified = auth_module.verify_firebase_session(cookie)

        self.assertEqual(cookie, "session-cookie")
        self.assertEqual(claims["uid"], "user-1")
        self.assertEqual(verified["uid"], "user-1")
        firebase_auth.verify_id_token.assert_called_once_with(
            "id-token", check_revoked=True, clock_skew_seconds=10
        )
        firebase_auth.verify_session_cookie.assert_called_once_with(
            "session-cookie", check_revoked=True, clock_skew_seconds=10
        )

    def test_clock_skew_is_bounded_to_firebase_limit(self):
        with patch.dict(os.environ, {"FIREBASE_CLOCK_SKEW_SECONDS": "120"}):
            self.assertEqual(auth_module.firebase_clock_skew_seconds(), 60)


class AuthApiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()

    def test_auth_config_is_public_when_disabled(self):
        with patch.dict(os.environ, {"FIREBASE_AUTH_ENABLED": "false"}):
            response = self.client.get("/api/auth/config")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["enabled"])
        self.assertIn("ai_csrf", response.cookies)

    def test_bootstrap_admin_can_exchange_token_for_session(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "FIREBASE_AUTH_ENABLED": "true",
                "FIREBASE_WEB_API_KEY": "public-test-key",
                "FIREBASE_BOOTSTRAP_ADMIN_EMAILS": "admin@example.com",
                "AUTH_USERS_DB_PATH": str(Path(directory) / "users.sqlite3"),
                "AUTH_COOKIE_SECURE": "false",
            },
        ), patch.object(
            auth_api,
            "create_firebase_session",
            return_value=("signed-session-cookie", {"uid": "admin-uid", "email": "admin@example.com"}),
        ), patch.object(
            auth_module,
            "verify_firebase_session",
            return_value={"uid": "admin-uid", "email": "admin@example.com"},
        ):
            config = self.client.get("/api/auth/config")
            csrf = config.cookies.get("ai_csrf")
            login = self.client.post(
                "/api/auth/session",
                headers={"X-CSRF-Token": csrf},
                json={"idToken": "x" * 40},
            )
            self.assertEqual(login.status_code, 200, login.text)
            self.assertEqual(login.json()["user"]["role"], "admin")
            self.assertIn("ai_session", login.cookies)

            me = self.client.get("/api/auth/me")
            self.assertEqual(me.status_code, 200, me.text)
            self.assertEqual(me.json()["user"]["email"], "admin@example.com")

    def test_uninvited_firebase_user_cannot_create_session(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "FIREBASE_AUTH_ENABLED": "true",
                "FIREBASE_WEB_API_KEY": "public-test-key",
                "FIREBASE_BOOTSTRAP_ADMIN_EMAILS": "admin@example.com",
                "AUTH_USERS_DB_PATH": str(Path(directory) / "users.sqlite3"),
                "AUTH_COOKIE_SECURE": "false",
            },
        ), patch.object(
            auth_api,
            "create_firebase_session",
            return_value=("signed-session-cookie", {"uid": "unknown-uid", "email": "unknown@example.com"}),
        ):
            config = self.client.get("/api/auth/config")
            response = self.client.post(
                "/api/auth/session",
                headers={"X-CSRF-Token": config.cookies.get("ai_csrf")},
                json={"idToken": "x" * 40},
            )
        self.assertEqual(response.status_code, 403)
        self.assertIn("초대한 계정", response.json()["detail"])

    def test_session_exchange_failure_is_not_reported_as_bad_password(self):
        with patch.dict(
            os.environ,
            {"FIREBASE_AUTH_ENABLED": "true", "AUTH_COOKIE_SECURE": "false"},
        ), patch.object(
            auth_api,
            "create_firebase_session",
            side_effect=ValueError("invalid token audience"),
        ), self.assertLogs(auth_api.LOGGER, level="ERROR") as logs:
            config = self.client.get("/api/auth/config")
            response = self.client.post(
                "/api/auth/session",
                headers={"X-CSRF-Token": config.cookies.get("ai_csrf")},
                json={"idToken": "x" * 40},
            )

        self.assertEqual(response.status_code, 401)
        self.assertIn("서버 세션", response.json()["detail"])
        self.assertNotIn("비밀번호", response.json()["detail"])
        self.assertIn("ValueError", "\n".join(logs.output))

    def test_protected_api_rejects_missing_session(self):
        with patch.dict(
            os.environ,
            {"FIREBASE_AUTH_ENABLED": "true", "AUTH_COOKIE_SECURE": "false"},
        ):
            response = self.client.get("/api/auth/me")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "로그인이 필요합니다.")

    def test_protected_api_rejects_expired_or_forged_session(self):
        with patch.dict(
            os.environ,
            {"FIREBASE_AUTH_ENABLED": "true", "AUTH_COOKIE_SECURE": "false"},
        ), patch.object(
            auth_module,
            "verify_firebase_session",
            side_effect=ValueError("invalid session"),
        ):
            response = self.client.get(
                "/api/auth/me", cookies={"ai_session": "forged"}
            )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "로그인 세션이 만료되었습니다.")

    def test_protected_api_rejects_unapproved_user(self):
        with patch.dict(
            os.environ,
            {"FIREBASE_AUTH_ENABLED": "true", "AUTH_COOKIE_SECURE": "false"},
        ), patch.object(
            auth_module,
            "verify_firebase_session",
            return_value={"uid": "blocked", "email": "blocked@example.com"},
        ), patch.object(
            auth_module,
            "get_user_by_uid",
            return_value={
                "uid": "blocked",
                "email": "blocked@example.com",
                "approved": False,
                "role": "member",
            },
        ):
            response = self.client.get(
                "/api/auth/me", cookies={"ai_session": "valid"}
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "승인되지 않은 사용자입니다.")

    def test_member_cannot_access_admin_api(self):
        user = {
            "uid": "member-1",
            "email": "member@example.com",
            "approved": True,
            "role": "member",
        }
        with patch.dict(
            os.environ,
            {"FIREBASE_AUTH_ENABLED": "true", "AUTH_COOKIE_SECURE": "false"},
        ), patch.object(
            auth_module,
            "verify_firebase_session",
            return_value={"uid": "member-1", "email": "member@example.com"},
        ), patch.object(auth_module, "get_user_by_uid", return_value=user):
            response = self.client.get(
                "/api/validation/logs", cookies={"ai_session": "valid"}
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "관리자 권한이 필요합니다.")

    def test_state_changing_api_rejects_missing_csrf_token(self):
        user = {
            "uid": "member-1",
            "email": "member@example.com",
            "approved": True,
            "role": "member",
        }
        with patch.dict(
            os.environ,
            {"FIREBASE_AUTH_ENABLED": "true", "AUTH_COOKIE_SECURE": "false"},
        ), patch.object(
            auth_module,
            "verify_firebase_session",
            return_value={"uid": "member-1", "email": "member@example.com"},
        ), patch.object(auth_module, "get_user_by_uid", return_value=user):
            response = self.client.post(
                "/api/chat",
                cookies={"ai_session": "valid", "ai_csrf": "cookie-token"},
                json={
                    "modelId": "test-model",
                    "messages": [{"role": "user", "content": "테스트"}],
                },
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "요청 보안 토큰이 올바르지 않습니다.")

    def test_admin_can_update_existing_user_without_creating_an_invite(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {
                "FIREBASE_AUTH_ENABLED": "true",
                "AUTH_USERS_DB_PATH": str(Path(directory) / "users.sqlite3"),
                "AUTH_COOKIE_SECURE": "false",
            },
        ):
            auth_module.upsert_invited_user(
                "admin-uid",
                "admin@example.com",
                "관리자",
                "admin",
                "",
                "컨텐츠서비스",
                "bootstrap",
            )
            auth_module.upsert_invited_user(
                "member-uid",
                "member@example.com",
                "팀원",
                "member",
                "",
                "컨텐츠서비스",
                "admin@example.com",
            )
            with patch.object(
                auth_module,
                "verify_firebase_session",
                return_value={"uid": "admin-uid", "email": "admin@example.com"},
            ):
                config = self.client.get("/api/auth/config")
                csrf_token = config.cookies.get("ai_csrf")
                self.client.cookies.set("ai_session", "valid-session")
                self.client.cookies.set("ai_csrf", csrf_token)
                users = self.client.get("/api/auth/users")
                response = self.client.patch(
                    "/api/auth/users/member-uid",
                    headers={"X-CSRF-Token": csrf_token},
                    json={"mantisUsername": "석지원"},
                )

        self.assertEqual(users.status_code, 200, users.text)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"]["mantisUsername"], "석지원")
        self.assertEqual(response.json()["user"]["role"], "member")

    def test_admin_cannot_downgrade_their_own_account(self):
        admin = {"uid": "admin-uid", "role": "admin"}
        with patch.object(auth_api, "current_request_user", return_value=admin), patch.object(
            auth_api, "get_user_by_uid", return_value={**admin, "approved": True}
        ):
            with self.assertRaises(HTTPException) as raised:
                auth_api.patch_user(
                    "admin-uid",
                    auth_api.UserAccessUpdateRequest(role="member"),
                    None,
                )
        self.assertEqual(raised.exception.status_code, 400)
        self.assertIn("권한이나 승인을 변경할 수 없습니다", raised.exception.detail)


if __name__ == "__main__":
    unittest.main()
