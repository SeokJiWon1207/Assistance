import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
