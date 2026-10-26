"""저장본에 없는 최신 이슈의 원문 접근과 출처 표시 회귀."""
import os
import unittest
from unittest.mock import patch

import requests
from fastapi import HTTPException
from fastapi.testclient import TestClient

from backend.main import app


class SourceDocumentApiTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"FIREBASE_AUTH_ENABLED": "false"})
        self.env.start()
        self.client = TestClient(app)
        self.addCleanup(self.env.stop)
        self.addCleanup(self.client.close)

    def test_missing_snapshot_returns_live_full_text_and_provenance(self):
        raw = {"id": 31631, "summary": "정기점검", "description": "본문" * 2000,
               "notes": [{"text": "노트 마지막까지"}], "additional_information": "추가 정보",
               "steps_to_reproduce": "재현 단계", "status": {"name": "confirmed"}}
        with patch("backend.api.search.get_chroma_document", return_value=None), patch(
            "backend.sources.mantis.fetch_mantis_issue_by_id", return_value=raw,
        ) as fetch:
            response = self.client.get("/api/chroma/documents/31631")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["issue_id"], 31631)
        self.assertEqual(data["source"], "mantis")
        self.assertTrue(data["fetchedAt"].endswith("+00:00"))
        self.assertIn(raw["description"], data["content"])
        self.assertIn("노트 마지막까지", data["content"])
        self.assertIn("추가 정보", data["content"])
        self.assertIn("재현 단계", data["content"])
        fetch.assert_called_once_with(31631)

    def test_existing_snapshot_preserves_historical_content_without_live_request(self):
        with patch("backend.api.search.get_chroma_document", return_value={"issue_id": 7, "content": "과거 원문"}), patch(
            "backend.sources.mantis.fetch_mantis_issue_by_id", side_effect=AssertionError("snapshot must stay historical"),
        ):
            response = self.client.get("/api/chroma/documents/7")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "과거 원문")
        self.assertEqual(response.json()["source"], "chroma")
        self.assertNotIn("fetchedAt", response.json())

    def test_missing_denied_and_unreachable_sources_remain_distinct(self):
        for result, error, expected in [
            (None, None, 404), (None, HTTPException(403, "Mantis 이슈 접근이 거절되었습니다."), 403),
            (None, requests.Timeout("private internal address"), 502),
        ]:
            with self.subTest(expected=expected), patch("backend.api.search.get_chroma_document", return_value=None), patch(
                "backend.sources.mantis.fetch_mantis_issue_by_id", return_value=result, side_effect=error,
            ):
                response = self.client.get("/api/chroma/documents/31631")
            self.assertEqual(response.status_code, expected)
            self.assertNotIn("private internal address", response.text)

    def test_invalid_id_does_not_request_source(self):
        with patch("backend.sources.mantis.fetch_mantis_issue_by_id", side_effect=AssertionError("invalid id")):
            self.assertEqual(self.client.get("/api/chroma/documents/0").status_code, 422)
