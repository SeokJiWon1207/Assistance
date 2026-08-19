import unittest

from backend.understanding import (
    extract_query_constraints,
    rule_based_search_intent,
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
        self.assertEqual(
            extract_query_constraints("홍길동 사원 이슈")["people"]["names"], ["홍길동"]
        )
        self.assertEqual(
            extract_query_constraints("우동헌 과장 담당 건")["people"]["names"], ["우동헌"]
        )

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
            self.assertTrue(should_run_notion_search(text), msg=text)

    def test_mantis_questions_do_not_trigger_notion(self):
        for text in (
            "MTS 미종료 이슈 보여줘",
            "화면번호 9413 이슈",
            "석지원 공수 알려줘",
            "월별 등록건수 추이",
        ):
            self.assertFalse(should_run_notion_search(text), msg=text)


if __name__ == "__main__":
    unittest.main()
