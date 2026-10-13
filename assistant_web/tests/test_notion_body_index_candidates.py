"""색인 후보 검증의 한도·실패·중복 처리 계약."""

import unittest
from unittest.mock import Mock, patch

import requests

from backend.notion import body_index_search


class BodyIndexCandidateTests(unittest.TestCase):
    def test_missing_index_does_not_call_live_validator(self):
        with patch.object(body_index_search, "open_body_index", return_value=None), \
                patch.object(body_index_search, "validate_index_candidate") as validate:
            self.assertIsNone(body_index_search.find_indexed_body_candidates(["db"], ["needle"]))
        validate.assert_not_called()

    def test_validation_failure_does_not_hide_later_valid_candidate(self):
        index = Mock()
        index.search.return_value = [{"page_id": "a"}, {"page_id": "b"}]
        index.status.return_value = {"complete": True}
        valid = {"raw_page": {"id": "b"}, "segments": [], "complete": True}
        with patch.object(body_index_search, "open_body_index", return_value=index), \
                patch.object(body_index_search, "validate_index_candidate",
                             side_effect=[requests.ConnectionError("private endpoint"), valid]):
            result = body_index_search.find_indexed_body_candidates(["db"], ["needle"])
        self.assertEqual(result["candidates"], [valid])
        self.assertFalse(result["validation_complete"])
        self.assertIn("body_index_validation_error", result["incomplete_reasons"])
        self.assertNotIn("private endpoint", str(result))

    def test_validation_limit_and_duplicate_candidates_are_explicit(self):
        index = Mock()
        index.search.return_value = [{"page_id": str(i)} for i in range(32)] * 2
        index.status.return_value = {"complete": True}
        with patch.object(body_index_search, "open_body_index", return_value=index), \
                patch.object(body_index_search, "validate_index_candidate", return_value=None) as validate:
            result = body_index_search.find_indexed_body_candidates(["db"], ["needle"])
        self.assertEqual(validate.call_count, 30)
        self.assertEqual(result["candidate_count"], 32)
        self.assertFalse(result["validation_complete"])
        self.assertIn("body_index_validation_limit", result["incomplete_reasons"])

    def test_time_budget_keeps_already_verified_results(self):
        index = Mock()
        index.search.return_value = [{"page_id": str(i)} for i in range(3)]
        index.status.return_value = {"complete": True}
        valid = {"raw_page": {"id": "0"}, "complete": True}
        with patch.object(body_index_search, "open_body_index", return_value=index), \
                patch.object(body_index_search.time, "monotonic", side_effect=[0, 0, 60]), \
                patch.object(body_index_search, "validate_index_candidate", return_value=valid):
            result = body_index_search.find_indexed_body_candidates(["db"], ["needle"])
        self.assertEqual(result["candidates"], [valid])
        self.assertFalse(result["validation_complete"])

    def test_typed_old_match_is_prioritized_before_thirty_recent_paragraphs(self):
        def record(identifier, kind, edited, owner=None):
            return {"page_id": identifier, "raw_page": {"id": identifier, "last_edited_time": edited},
                    "segments": [{"block_id": f"{identifier}-block", "type": kind, "text": "needle",
                                  "page_id": identifier, "owner_page_id": owner or identifier}]}

        for kind in ("code", "table_row"):
            with self.subTest(kind=kind):
                old = record("old-typed", kind, "2025-01-01T00:00:00Z")
                index = Mock()
                index.search.return_value = [
                    record(f"recent-{i}", "paragraph", "2026-10-01T00:00:00Z") for i in range(30)
                ] + [record("foreign-typed", kind, "2026-10-02T00:00:00Z", owner="different-page"), old]
                index.status.return_value = {"complete": True}
                with patch.object(body_index_search, "open_body_index", return_value=index), \
                        patch.object(body_index_search, "validate_index_candidate", side_effect=lambda row, *_a, **_kw: {
                            "raw_page": row["raw_page"], "segments": row["segments"], "complete": True,
                        }) as validate:
                    result = body_index_search.find_indexed_body_candidates(["db"], ["needle"], body_clauses=[{
                        "field": "keyword", "operator": "contains", "value": "needle", "block_type": kind,
                    }])
                self.assertEqual(validate.call_args_list[0].args[0]["page_id"], "old-typed")
                self.assertIn("old-typed", [row["raw_page"]["id"] for row in result["candidates"]])
                self.assertEqual(validate.call_count, 30)
                self.assertFalse(result["validation_complete"])
