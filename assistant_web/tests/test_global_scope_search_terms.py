"""전사 범위 지정이 원문 검색어로 섞이는 회귀를 막는다."""
import unittest
from backend.intent.search_terms import build_keyword_terms

class GlobalScopeSearchTermsTest(unittest.TestCase):
    def test_global_scope_words_do_not_filter_issue_contents(self):
        constraints={"mantis_team_scope":"all","platform_key":"mts","platform_aliases":["MTS"],"created_at":{"from":"2026-10-01","to":"2026-10-31","precision":"month"}}
        for text in ("2026년 10월 전사 MTS 이슈 전체 목록 보여줘","팀 제한 없이 전사 전체 이슈로 보여줘","그중 전사 기준으로 바꿔줘","전체 팀 이슈 보여줘"):
            with self.subTest(text=text):
                self.assertEqual(build_keyword_terms(text,constraints),[])
    def test_global_scope_removal_preserves_the_actual_topic(self):
        constraints={"mantis_team_scope":"all"}
        self.assertEqual(build_keyword_terms("전사 VBScript 이슈 보여줘",constraints),["VBScript"])
    def test_without_global_scope_the_word_remains_a_topic(self):
        self.assertEqual(build_keyword_terms("전사 VBScript 이슈 보여줘",{}),["전사","VBScript"])
    def test_quoted_topic_is_preserved_with_global_scope(self):
        self.assertEqual(build_keyword_terms('전사 "전사 오류" 이슈 보여줘',{"mantis_team_scope":"all"}),["전사","오류"])
