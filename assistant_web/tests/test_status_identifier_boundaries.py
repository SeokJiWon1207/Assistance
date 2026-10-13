"""실연결 코드 검색에서 상태 별칭이 식별자 내부와 충돌한 회귀."""

import unittest

from backend.intent.constraints import extract_query_constraints


class StatusIdentifierBoundaryTests(unittest.TestCase):
    def test_code_identifiers_do_not_create_status_filters(self):
        for token in ("GetMarketCloseTime", "OpenFile", "renew_token", "reassigned", "undeveloped"):
            with self.subTest(token=token):
                constraints = extract_query_constraints(f"노션에서 {token} 찾아줘")
                self.assertEqual(constraints["statuses"], [])
                self.assertFalse(any(c["field"] == "status" for c in constraints["filter_clauses"]))

    def test_standalone_english_status_and_korean_suffix_still_work(self):
        for phrase, expected in (("closed", "closed"), ("OPEN인", "open"),
                                 ("assigned", "assigned"), ("developed 상태", "dev_done")):
            with self.subTest(phrase=phrase):
                self.assertIn(expected, extract_query_constraints(f"노션 {phrase} 이슈 찾아줘")["statuses"])
