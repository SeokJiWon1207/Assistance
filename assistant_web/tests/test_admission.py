import os
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from backend.admission import ChatAdmissionController
from backend.schemas import ChatRequest


class ChatAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.controller = ChatAdmissionController()

    def test_per_user_concurrency_is_rejected_and_released(self):
        with patch.dict(
            os.environ,
            {
                "CHAT_MAX_CONCURRENT_PER_USER": "1",
                "CHAT_MAX_CONCURRENT_TOTAL": "4",
                "CHAT_RATE_LIMIT_PER_MINUTE": "20",
            },
        ):
            with self.controller.admit("user-1"):
                with self.assertRaises(HTTPException) as raised:
                    with self.controller.admit("user-1"):
                        pass
                self.assertEqual(raised.exception.status_code, 429)
            with self.controller.admit("user-1"):
                pass

    def test_rate_limit_is_enforced_per_user(self):
        with patch.dict(
            os.environ,
            {
                "CHAT_MAX_CONCURRENT_PER_USER": "2",
                "CHAT_MAX_CONCURRENT_TOTAL": "4",
                "CHAT_RATE_LIMIT_PER_MINUTE": "2",
            },
        ):
            with self.controller.admit("user-1"):
                pass
            with self.controller.admit("user-1"):
                pass
            with self.assertRaises(HTTPException) as raised:
                with self.controller.admit("user-1"):
                    pass
            self.assertEqual(raised.exception.status_code, 429)
            with self.controller.admit("user-2"):
                pass

    def test_acquired_lease_releases_once(self):
        with patch.dict(
            os.environ,
            {
                "CHAT_MAX_CONCURRENT_PER_USER": "1",
                "CHAT_MAX_CONCURRENT_TOTAL": "1",
                "CHAT_RATE_LIMIT_PER_MINUTE": "20",
            },
        ):
            lease = self.controller.acquire("user-1")
            with self.assertRaises(HTTPException):
                self.controller.acquire("user-1")
            lease.release()
            lease.release()
            second = self.controller.acquire("user-1")
            second.release()

    def test_chat_request_rejects_oversized_history(self):
        with self.assertRaisesRegex(ValueError, "120,000"):
            ChatRequest(
                modelId="model",
                messages=[
                    {"role": "user", "content": "x" * 30000}
                    for _ in range(5)
                ],
            )


if __name__ == "__main__":
    unittest.main()
