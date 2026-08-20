import unittest
from unittest.mock import patch

from backend.answers import build_unverified_person_note, format_deterministic_issue_answer
from backend.intent.constraints import (
    extract_query_constraints,
    merge_query_plan_constraints,
)
from backend.retrieval import run_retrieval_pipeline


class PeopleFilterGroundingTest(unittest.TestCase):
    def test_model_cannot_add_an_ungrounded_owner(self):
        question = "신한 이슈 보여줘"
        constraints = extract_query_constraints(question)
        merged = merge_query_plan_constraints(
            constraints,
            {
                "grounding_text": question,
                "filter_clauses": [
                    {"field": "owner", "operator": "equals", "value": "김환각"},
                ],
            },
        )
        self.assertEqual(merged["people"]["names"], [])
        self.assertEqual(
            [clause for clause in merged["filter_clauses"] if clause["field"] == "owner"],
            [],
        )

    def test_explicit_unverified_owner_is_filterable(self):
        question = "담당자 김신입 이슈 보여줘"
        constraints = extract_query_constraints(question)
        merged = merge_query_plan_constraints(
            constraints,
            {
                "grounding_text": question,
                "filter_clauses": [
                    {"field": "owner", "operator": "equals", "value": "김신입"},
                ],
            },
        )
        self.assertEqual(merged["people"]["names"], ["김신입"])
        self.assertEqual(merged["people"]["name_origins"], {"김신입": "explicit_unverified"})

    def test_model_cannot_promote_a_general_noun_after_person_context(self):
        question = "담당자 현황 이슈 보여줘"
        constraints = extract_query_constraints(question)
        merged = merge_query_plan_constraints(
            constraints,
            {
                "grounding_text": question,
                "filter_clauses": [
                    {"field": "owner", "operator": "equals", "value": "현황"},
                ],
            },
        )
        self.assertEqual(merged["people"]["names"], [])
        self.assertEqual(
            [clause for clause in merged["filter_clauses"] if clause["field"] == "owner"],
            [],
        )

    def test_inferred_person_is_not_applied_as_a_filter(self):
        constraints = extract_query_constraints("신입 선임 이슈 보여줘")
        self.assertEqual(constraints["people"]["names"], [])
        self.assertEqual(
            constraints["people"]["candidates"],
            [{"name": "신입", "origin": "inferred"}],
        )


class RetrievalSourceContractTest(unittest.TestCase):
    @patch("backend.retrieval.execute_chroma_search")
    @patch("backend.retrieval.execute_mantis_page_scan")
    @patch("backend.retrieval.execute_mantis_filter_search")
    @patch("backend.retrieval.execute_mantis_snapshot_search")
    @patch("backend.retrieval.execute_notion_search")
    def test_notion_plan_does_not_execute_mantis_for_platform_scope(
        self,
        notion_search,
        snapshot_search,
        filter_search,
        page_scan,
        chroma_search,
    ):
        notion_search.return_value = {
            "source": "notion",
            "status": "ok",
            "notion_pages": [],
            "meta": {"displayed_count": 0},
        }
        result = run_retrieval_pipeline(
            "MTS 운영 가이드 찾아줘",
            5,
            {"mantis": True, "notion": True, "vectorDb": True},
            {"source": "notion", "grounding_text": "MTS 운영 가이드 찾아줘"},
        )
        self.assertEqual([stage["source"] for stage in result["stages"]], ["notion"])
        snapshot_search.assert_not_called()
        filter_search.assert_not_called()
        page_scan.assert_not_called()
        chroma_search.assert_not_called()

    @patch("backend.retrieval.execute_mantis_page_scan")
    @patch("backend.retrieval.execute_mantis_filter_search")
    @patch("backend.retrieval.execute_notion_search")
    def test_mantis_plan_does_not_execute_notion_for_document_word(
        self,
        notion_search,
        filter_search,
        page_scan,
    ):
        filter_search.return_value = {
            "source": "mantis_filter",
            "status": "ok",
            "issues_list": [],
            "meta": {"displayed_count": 0},
        }
        page_scan.return_value = {
            "source": "mantis_page_scan",
            "status": "ok",
            "issues_list": [],
            "meta": {"displayed_count": 0},
        }
        result = run_retrieval_pipeline(
            "MTS 이슈 문서 보여줘",
            5,
            {"mantis": True, "notion": True, "vectorDb": False},
            {"source": "mantis", "grounding_text": "MTS 이슈 문서 보여줘"},
        )
        self.assertTrue(result["stages"])
        self.assertNotIn("notion", [stage["source"] for stage in result["stages"]])
        notion_search.assert_not_called()


class UnverifiedPersonAnswerTest(unittest.TestCase):
    def test_zero_results_explain_an_explicit_unverified_person(self):
        constraints = extract_query_constraints("담당자 김신입 이슈 보여줘")
        note = build_unverified_person_note(constraints, 0)
        self.assertIn("김신입", note or "")
        self.assertIn("담당자 명부", note or "")

    def test_known_or_inferred_people_do_not_show_the_warning(self):
        self.assertIsNone(
            build_unverified_person_note(
                {
                    "people": {
                        "names": ["석지원"],
                        "role": "handler",
                        "name_origins": {"석지원": "known"},
                        "candidates": [],
                    }
                },
                0,
            )
        )
        self.assertIsNone(build_unverified_person_note(extract_query_constraints("신입 선임 이슈"), 0))

    def test_zero_result_answer_contains_the_unverified_person_explanation(self):
        question = "담당자 김신입 이슈 보여줘"
        constraints = extract_query_constraints(question)
        answer = format_deterministic_issue_answer(
            question,
            constraints,
            {
                "intent_plan": {"source": "mantis"},
                "stages": [
                    {
                        "source": "mantis_filter",
                        "status": "ok",
                        "issues_list": [],
                        "meta": {"found_count": 0},
                    }
                ],
            },
            5,
        )
        self.assertIn("김신입", answer)
        self.assertIn("담당자 명부에서 확인하지 못했어요", answer)


if __name__ == "__main__":
    unittest.main()
