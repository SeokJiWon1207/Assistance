import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
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

    def test_low_memory_rejects_before_consuming_slot_or_rate_limit(self):
        with patch.dict(os.environ, {"CHAT_MIN_AVAILABLE_MEMORY_MB": "192", "CHAT_MEMORY_RESERVE_PER_REQUEST_MB": "128", "CHAT_RATE_LIMIT_PER_MINUTE": "1"}), \
             patch("backend.admission.available_memory_bytes", side_effect=[319 * 1024 ** 2, 512 * 1024 ** 2]):
            with self.assertRaises(HTTPException) as raised:
                self.controller.acquire("user-1")
            self.assertEqual(raised.exception.status_code, 429)
            self.assertEqual(raised.exception.headers["Retry-After"], "5")
            lease = self.controller.acquire("user-1")
            lease.release()
            with self.assertRaises(HTTPException) as rate_limited:
                self.controller.acquire("user-1")
            self.assertEqual(rate_limited.exception.status_code, 429)
            self.assertNotEqual(rate_limited.exception.headers["Retry-After"], "5")

    def test_concurrent_requests_reserve_memory_atomically(self):
        barrier = Barrier(4)
        def acquire(index):
            barrier.wait()
            try:
                return self.controller.acquire(f"user-{index}")
            except HTTPException as exc:
                return exc.status_code
        with patch.dict(os.environ, {"CHAT_MAX_CONCURRENT_TOTAL": "8", "CHAT_MIN_AVAILABLE_MEMORY_MB": "192", "CHAT_MEMORY_RESERVE_PER_REQUEST_MB": "128"}), \
             patch("backend.admission.available_memory_bytes", return_value=448 * 1024 ** 2, create=True):
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(acquire, range(4)))
            accepted = [item for item in results if item != 429]
            self.assertEqual(len(accepted), 2)
            self.assertEqual(results.count(429), 2)
            for lease in accepted:
                lease.release()
            with self.controller.admit("user-after-release"):
                pass

    def test_missing_memory_metrics_keeps_fixed_concurrency_limit(self):
        with patch.dict(os.environ, {"CHAT_MAX_CONCURRENT_TOTAL": "2"}), \
             patch("backend.admission.available_memory_bytes", return_value=None, create=True):
            first = self.controller.acquire("first")
            second = self.controller.acquire("second")
            with self.assertRaises(HTTPException) as raised:
                self.controller.acquire("third")
            self.assertEqual(raised.exception.status_code, 429)
            first.release()
            second.release()


if __name__ == "__main__":
    unittest.main()
