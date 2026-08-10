import os
import unittest
from datetime import date, timedelta
from unittest.mock import patch

from backend import startup_validation


class StartupValidationTests(unittest.TestCase):
    def production_env(self, **overrides):
        values = {
            "APP_ENV": "production",
            "FIREBASE_AUTH_ENABLED": "true",
            "AUTH_COOKIE_SECURE": "true",
            "PUBLIC_BASE_URL": "https://assistant.example.com",
            "MODEL_ENDPOINT_INSECURE_ALLOWLIST": "",
            "MODEL_ENDPOINT_INSECURE_ALLOWLIST_EXPIRES_AT": "",
        }
        values.update(overrides)
        return patch.dict(os.environ, values, clear=True)

    def test_production_rejects_disabled_auth(self):
        with self.production_env(FIREBASE_AUTH_ENABLED="false"):
            with self.assertRaisesRegex(RuntimeError, "FIREBASE_AUTH_ENABLED"):
                startup_validation.validate_auth_transport_security()

    def test_production_rejects_insecure_cookie(self):
        with self.production_env(AUTH_COOKIE_SECURE="false"):
            with self.assertRaisesRegex(RuntimeError, "AUTH_COOKIE_SECURE"):
                startup_validation.validate_auth_transport_security()

    def test_production_rejects_http_public_url(self):
        with self.production_env(PUBLIC_BASE_URL="http://assistant.example.com"):
            with self.assertRaisesRegex(RuntimeError, "PUBLIC_BASE_URL"):
                startup_validation.validate_auth_transport_security()

    @patch.object(startup_validation, "get_model_registry")
    def test_remote_http_model_requires_exact_allowlist(self, registry):
        registry.return_value = [
            {"id": "company", "endpoint": "http://model.example.com:13025/v1/chat", "enabled": True}
        ]
        with self.production_env():
            with self.assertRaisesRegex(RuntimeError, "허용되지 않습니다"):
                startup_validation.validate_model_endpoint_security()

    @patch.object(startup_validation, "get_model_registry")
    def test_remote_http_model_accepts_unexpired_exact_origin(self, registry):
        registry.return_value = [
            {"id": "company", "endpoint": "http://model.example.com:13025/v1/chat", "enabled": True}
        ]
        with self.production_env(
            MODEL_ENDPOINT_INSECURE_ALLOWLIST="http://model.example.com:13025",
            MODEL_ENDPOINT_INSECURE_ALLOWLIST_EXPIRES_AT="2099-12-31",
        ):
            startup_validation.validate_model_endpoint_security()

    def test_remote_http_allowlist_rejects_wildcard(self):
        with self.production_env(
            MODEL_ENDPOINT_INSECURE_ALLOWLIST="http://*",
            MODEL_ENDPOINT_INSECURE_ALLOWLIST_EXPIRES_AT="2099-12-31",
        ):
            with self.assertRaisesRegex(RuntimeError, "wildcard"):
                startup_validation.validate_model_endpoint_security()

    @patch.object(startup_validation, "get_model_registry")
    def test_remote_http_allowlist_rejects_expired_exception(self, registry):
        registry.return_value = [
            {"id": "company", "endpoint": "http://model.example.com:13025/v1/chat", "enabled": True}
        ]
        with self.production_env(
            MODEL_ENDPOINT_INSECURE_ALLOWLIST="http://model.example.com:13025",
            MODEL_ENDPOINT_INSECURE_ALLOWLIST_EXPIRES_AT=(date.today() - timedelta(days=1)).isoformat(),
        ):
            with self.assertRaisesRegex(RuntimeError, "만료"):
                startup_validation.validate_model_endpoint_security()

    @patch.object(startup_validation, "get_model_registry")
    def test_transport_status_exposes_d30_warning_without_endpoint(self, registry):
        registry.return_value = [
            {"id": "company", "endpoint": "http://model.example.com:13025/v1/chat", "enabled": True}
        ]
        with self.production_env(
            MODEL_ENDPOINT_INSECURE_ALLOWLIST="http://model.example.com:13025",
            MODEL_ENDPOINT_INSECURE_ALLOWLIST_EXPIRES_AT="2026-09-01",
            MODEL_ENDPOINT_INSECURE_ALLOWLIST_WARNING_DAYS="30",
        ):
            status = startup_validation.get_model_transport_security_status(
                today=date(2026, 8, 11)
            )

        self.assertEqual(status["status"], "warning")
        self.assertEqual(status["daysRemaining"], 21)
        self.assertEqual(status["exceptionModelIds"], ["company"])
        self.assertNotIn("model.example.com", str(status))


if __name__ == "__main__":
    unittest.main()
