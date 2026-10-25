"""외부 토큰 검증만 대체하고 실제 세션·CSRF·사용자 저장소·권한 경계를 검증한다."""
import os
import tempfile
import time
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend import auth
from backend.api import auth as auth_api
from backend.main import app


class AuthRoleLifecycleTests(TestCase):
    def setUp(self):
        folder = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.dict(os.environ, {"FIREBASE_AUTH_ENABLED": "true",
            "AUTH_COOKIE_SECURE": "true", "AUTH_USERS_DB_PATH": str(Path(folder) / "users.sqlite3"),
            "FIREBASE_BOOTSTRAP_ADMIN_EMAILS": ""}))
        self.claims = {"uid": "test-user", "email": "test@example.test", "auth_time": int(time.time())}
        self.enterContext(patch.object(auth_api, "create_firebase_session", return_value=("test-signed-cookie", self.claims)))
        self.enterContext(patch.object(auth, "verify_firebase_session", return_value=self.claims))
        self.client = TestClient(app, base_url="https://testserver")
        self.addCleanup(self.client.close)

    def login(self, role):
        auth.upsert_invited_user("test-user", "test@example.test", "검증 사용자", role, "검증 담당자", "검증 팀", "admin@example.test")
        config = self.client.get("/api/auth/config")
        result = self.client.post("/api/auth/session", json={"idToken": "x" * 40},
                                  headers={"X-CSRF-Token": config.cookies.get("ai_csrf")})
        self.assertEqual(result.status_code, 200, result.text)
        self.assertIn("HttpOnly", result.headers["set-cookie"])
        self.assertIn("Secure", result.headers["set-cookie"])
        return result

    def test_role_access_is_enforced_and_changes_apply_to_existing_session(self):
        self.login("member")
        self.assertEqual(self.client.get("/api/auth/me").status_code, 200)
        self.assertEqual(self.client.get("/api/auth/users").status_code, 403)
        auth.update_user_access("test-user", role="manager")
        self.assertEqual(self.client.get("/api/auth/users").status_code, 403)
        auth.update_user_access("test-user", role="admin")
        self.assertEqual(self.client.get("/api/auth/users").status_code, 200)
        auth.update_user_access("test-user", approved=False)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 403)

    def test_logout_requires_csrf_and_removes_authenticated_access(self):
        self.login("member")
        self.assertEqual(self.client.post("/api/auth/logout").status_code, 403)
        result = self.client.post("/api/auth/logout", headers={"X-CSRF-Token": self.client.cookies.get("ai_csrf")})
        self.assertEqual(result.status_code, 200)
        self.assertNotIn("__Host-ai_session", self.client.cookies)
        self.assertEqual(self.client.get("/api/auth/me").status_code, 401)
