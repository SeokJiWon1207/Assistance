"""실제 검색에서 고객사 별칭으로 오인된 코드 식별자 회귀."""
import unittest

from backend.intent.constraints import extract_query_constraints
from backend.intent.search_terms import term_is_covered_by_constraints
from backend.retrieval import run_retrieval_pipeline
import test_notion_accuracy_regressions as fixtures


class NotionIdentifierSearchTests(unittest.TestCase):
    setUp = fixtures.NotionAccuracyRegressionTests.setUp
    search_fixture = fixtures.NotionAccuracyRegressionTests.search_fixture

    def test_identifier_is_searched_as_body_text_without_customer_scope(self):
        token = "MiraeAsset_NewChart_Ver"
        self.search_fixture([fixtures.page("target"), fixtures.page("unrelated")], {
            "target": [fixtures.NotionAccuracyRegressionTests.paragraph("block", token)],
            "unrelated": [fixtures.NotionAccuracyRegressionTests.paragraph("other", "unrelated")],
        })
        query = f"노션에서 {token} 찾아줘"
        self.assertIsNone(extract_query_constraints(query)["customer_key"])
        stage = run_retrieval_pipeline(query, 5, {"notion": True})["stages"][0]
        self.assertEqual(stage["meta"]["terms"], [token])
        self.assertEqual([p["id"] for p in stage["notion_pages"]], ["target"])

    def test_explicit_customer_does_not_consume_a_separate_identifier(self):
        token = "MiraeAsset_NewChart_Ver"
        query = f"노션 미래에셋 {token} 찾아줘"
        constraints = extract_query_constraints(query)
        self.assertTrue(constraints["customer_aliases"])
        self.assertFalse(term_is_covered_by_constraints(token, constraints))
        self.search_fixture([], {})
        stage = run_retrieval_pipeline(query, 5, {"notion": True})["stages"][0]
        self.assertEqual(stage["meta"]["terms"], [token])
        self.assertTrue(any(c["field"] == "customer" for c in stage["meta"]["constraints"]["common_filter_clauses"]))

    def test_standalone_customer_alias_and_exact_project_remain_filters(self):
        self.assertTrue(extract_query_constraints("노션 MiraeAsset 찾아줘")["customer_aliases"])
        constraints = extract_query_constraints("147MiraeAsset(미래에셋증권) 이슈 보여줘")
        self.assertEqual(constraints["customer_exact"], "147MiraeAsset(미래에셋증권)")


if __name__ == "__main__":
    unittest.main()
