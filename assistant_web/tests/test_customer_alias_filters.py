import unittest
from unittest.mock import patch

from backend.intent.matching import chroma_text_matches_constraints
from backend.query_filters import matches_source_filters
from backend.sources import mantis
from backend.understanding import extract_query_constraints


class CustomerAliasFilterTests(unittest.TestCase):
    def setUp(self):
        mantis.clear_mantis_recent_cache()
        self.addCleanup(mantis.clear_mantis_recent_cache)
        self.enterContext(patch.object(mantis, "get_mantis_settings", return_value={"url": "https://alias.test", "token": "test"}))

    def test_broker_name_precedes_product_alias_but_explicit_project_is_preserved(self):
        cases = [
            ("노션에서 한화 서버 자동주문 장애에 관한 건 알려줘", "한화"),
            ("KB증권 서버자동주문 이슈 보여줘", "KB증권"),
            ("서버자동주문 이슈 보여줘", "서버자동주문"),
            ("프로젝트 서버자동주문에서 한화 관련 이슈", "서버자동주문"),
        ]
        for question, expected in cases:
            with self.subTest(question=question):
                self.assertEqual(extract_query_constraints(question)["customer_key"], expected)

    def test_live_and_snapshot_filters_accept_current_and_legacy_names(self):
        issue = {"id": 42, "summary": "차트 오류", "project": {"name": "101PopcornRenewal(동부증권)"}}
        document = "이슈 ID: #42\n프로젝트(고객사): 101PopcornRenewal(동부증권)\n[제목]\n차트 오류\n\n"
        for customer in ("DB증권", "DB금융투자", "동부증권", "디비증권"):
            with self.subTest(customer=customer):
                constraints = extract_query_constraints(f"{customer} 최근 이슈 보여줘")
                self.assertTrue(mantis.issue_matches_constraints(issue, constraints))
                self.assertTrue(chroma_text_matches_constraints(document, constraints))
                with patch.object(mantis, "fetch_mantis_issues", return_value=[issue]):
                    stage = mantis.execute_mantis_page_scan("", constraints=constraints)
                self.assertEqual(stage["meta"]["matched_issue_ids"], [42])

    def test_alias_operators_and_unrelated_projects(self):
        for source in ("mantis", "chroma", "notion"):
            for operator, expected in (("equals", True), ("in", True), ("not_equals", False), ("not_in", False)):
                clause = {"field": "customer", "operator": operator, "value": "DB증권"}
                with self.subTest(source=source, operator=operator):
                    self.assertEqual(matches_source_filters(source, {"customer": "101PopcornRenewal(동부증권)"}, [clause]), expected)
                    self.assertEqual(matches_source_filters(source, {"customer": "112KBStar(KB증권)"}, [clause]), not expected)

    def test_short_alias_does_not_match_arbitrary_word(self):
        self.assertFalse(matches_source_filters("mantis", {"customer": "sandbox"}, [
            {"field": "customer", "operator": "equals", "value": "DB증권"},
        ]))
