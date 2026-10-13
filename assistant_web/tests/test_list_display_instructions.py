"""전체 목록의 표시 열 요청을 검색 주제와 구분한다."""
import unittest

from backend.intent.constraints import extract_query_constraints
from backend.intent.search_terms import build_keyword_terms, wants_all_results


class ListDisplayInstructionTests(unittest.TestCase):
    def test_requested_columns_do_not_become_search_terms(self):
        for text in (
            "2026년 10월에 등록된 MTS 이슈 전체 목록을 보여줘. 이슈 번호, 제목, 담당자, 등록일을 모두 표시해줘.",
            "2026년 10월에 등록된 MTS 이슈 전체 목록 보여줘. 제목과 담당자를 표시해줘.",
            "2026년 10월에 등록된 MTS 로그인 이슈 전체 목록 보여줘. 이슈 번호, 제목, 담당자, 등록일을 모두 표시해줘.",
        ):
            with self.subTest(text=text):
                constraints = extract_query_constraints(text)
                self.assertEqual(constraints["platform_key"], "mts")
                self.assertEqual(constraints["created_at"]["from"], "2026-10-01")
                self.assertTrue(wants_all_results(text))
                self.assertEqual(build_keyword_terms(text, constraints), ["로그인"] if "로그인" in text else [])

    def test_field_words_in_issue_topics_are_kept(self):
        for text, expected in (
            ("10월 MTS 등록일 표시 오류 이슈 보여줘", ["표시", "오류"]),
            ("10월 MTS 로그인 제목 오류 이슈 보여줘", ["로그인", "오류"]),
        ):
            with self.subTest(text=text):
                self.assertEqual(build_keyword_terms(text, extract_query_constraints(text)), expected)
