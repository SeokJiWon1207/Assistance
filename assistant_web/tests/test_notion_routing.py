import json
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.notion.search import build_notion_terms, extract_notion_constraints
from backend.notion.formatting import has_deterministic_notion_list_intent
from backend.answers import should_return_deterministic_issue_list
from backend.intent.context import is_follow_up_question

from backend.understanding import (
    extract_query_constraints,
    build_keyword_terms,
    rule_based_search_intent,
    has_notion_retrieval_signal,
    should_run_notion_search,
)


class CompoundNounNameTest(unittest.TestCase):
    """'사원'은 직위 접미사이면서 복합명사의 일부다."""

    def test_compound_nouns_are_not_person_names(self):
        for text in ("신입사원 관련 문서 찾아줘", "경력사원 교육", "정규사원 명단", "계약사원 현황"):
            self.assertEqual(
                extract_query_constraints(text)["people"]["names"], [], msg=text
            )

    def test_real_names_with_titles_still_extracted(self):
        with patch(
            "backend.intent.entities.load_known_people",
            return_value=(("홍길동", "우동헌"), ()),
        ):
            self.assertEqual(
                extract_query_constraints("홍길동 사원 이슈")["people"]["names"], ["홍길동"]
            )
            self.assertEqual(
                extract_query_constraints("우동헌 과장 담당 건")["people"]["names"], ["우동헌"]
            )

    def test_unverified_title_name_is_only_a_candidate(self):
        with patch("backend.intent.entities.load_known_people", return_value=((), ())):
            people = extract_query_constraints("홍길동 사원 이슈")["people"]
        self.assertEqual(people["names"], [])
        self.assertEqual(people["candidates"], [{"name": "홍길동", "origin": "inferred"}])

    def test_fake_person_no_longer_routes_to_mantis(self):
        # 가짜 인물 조건이 생기면 문서 질문이 Mantis 이슈 검색으로 빠진다.
        question = "신입사원 관련 문서 찾아줘"
        constraints = extract_query_constraints(question)
        self.assertNotEqual(rule_based_search_intent(question, constraints).get("source"), "mantis")


class NotionIntentKeywordTest(unittest.TestCase):
    def test_document_wordings_trigger_notion(self):
        for text in (
            "신입사원 관련 문서 찾아줘",
            "신입사원 교육 자료",
            "회의록 찾아줘",
            "인수인계 문서",
            "온보딩 가이드",
            "휴가 규정",
        ):
            self.assertTrue(has_notion_retrieval_signal(text), msg=text)

    def test_mantis_questions_do_not_trigger_notion(self):
        for text in (
            "MTS 미종료 이슈 보여줘",
            "화면번호 9413 이슈",
            "석지원 공수 알려줘",
            "월별 등록건수 추이",
        ):
            self.assertFalse(has_notion_retrieval_signal(text), msg=text)

    def test_legacy_notion_search_gate_name_remains_compatible(self):
        self.assertIs(should_run_notion_search, has_notion_retrieval_signal)


class RoutingFixtureTest(unittest.TestCase):
    def test_routing_and_people_filter_contract(self):
        fixture = Path(__file__).parent / "fixtures" / "routing_cases.jsonl"
        with patch(
            "backend.intent.entities.load_known_people",
            return_value=(("김보람", "svn"), ()),
        ):
            for line in fixture.read_text(encoding="utf-8").splitlines():
                case = json.loads(line)
                with self.subTest(question=case["q"]):
                    constraints = extract_query_constraints(case["q"])
                    plan = rule_based_search_intent(case["q"], constraints)
                    self.assertEqual(plan["source"], case["source"])
                    self.assertEqual(plan["authority"], case["authority"])
                    self.assertNotIn("needs_model", plan)
                    if "follow_up" in case:
                        self.assertEqual(is_follow_up_question(case["q"]), case["follow_up"])
                    if "notion_terms" in case:
                        self.assertEqual(build_notion_terms(case["q"]), case["notion_terms"])
                    if "notion_deterministic" in case:
                        self.assertEqual(has_deterministic_notion_list_intent(case["q"]), case["notion_deterministic"])
                    if "statuses" in case:
                        self.assertEqual(constraints["statuses"], case["statuses"])
                    if "mantis_terms" in case:
                        self.assertEqual(build_keyword_terms(case["q"], constraints), case["mantis_terms"])
                    if "mantis_deterministic" in case:
                        self.assertEqual(should_return_deterministic_issue_list(case["q"], constraints), case["mantis_deterministic"])
                    if "created_at" in case:
                        self.assertEqual(constraints["created_at"], case["created_at"])
                    if "customer_key" in case:
                        self.assertEqual(constraints["customer_key"], case["customer_key"])
                    if "customer_exact" in case:
                        self.assertEqual(constraints["customer_exact"], case["customer_exact"])
                    if "screen_numbers" in case:
                        self.assertEqual(constraints["screen_numbers"], case["screen_numbers"])
                    for alias in case.get("customer_forbidden_aliases", []):
                        self.assertNotIn(alias, constraints["customer_aliases"])
                    with patch("backend.notion.search.load_known_people", return_value=(
                        tuple(case.get("notion_known_people", ["김보람", "svn"])), (),
                    )):
                        for key, value in case.get("notion_constraints", {}).items():
                            self.assertEqual(extract_notion_constraints(case["q"])[key], value)
                    self.assertEqual(constraints["people"]["names"], case["people"])
                    self.assertEqual(
                        [item["name"] for item in constraints["people"]["candidates"]],
                        case["candidate_people"],
                    )


if __name__ == "__main__":
    unittest.main()
