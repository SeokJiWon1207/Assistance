"""본문 발췌가 뒤쪽 코드·표 근거와 원문 조회 상태를 보존한다."""

import unittest

from backend.notion.client import NOTION_CONTENT_INCOMPLETE
from backend.retrieval import build_model_retrieval_context


# 2026-10-12 연결 원문에서 확인한 C++ 11 코드와 C++ 20 표의 작은 발췌.
# 개인·담당자 정보나 전체 원문은 회귀 fixture에 저장하지 않는다.
MARKET_CODE = """enum MARKETTYPE
{
\t\tMARKETTYPE_STOCK,
\t\tMARKETTYPE_INDEX,
\t\tMARKETTYPE_FUTOPT
};

enum COLOR
{
\t\tRED,
\t\tGREEN,
\t\tBLUE
};

int main()
{
\tMARKETTYPE type = MARKETTYPE_STOCK;
\tCOLOR color = RED;

\t//서로 다른 이름으로 정의되었지만 서로 0으로 동작하기 때문에 TRUE가 됨
\tif (type == color)
\t\treturn 1;
\treturn 0;
}"""
AWAIT_TABLE = (
    "suspend_always | await_ready()에서 false 리턴, 대기\n"
    "suspend_never | await_ready()에서 true 리턴, 대기하지 않음"
)


class NotionBodyModelContextTests(unittest.TestCase):
    def context(self, content, query, *, page_fields=None, stage_meta=None):
        page = {"id": "page", "title": "C++ 11", "content": content,
                **(page_fields or {})}
        return build_model_retrieval_context({
            "query": query,
            "stages": [{"source": "notion", "status": "ok", "notion_pages": [page],
                        "meta": {"search_complete": True, **(stage_meta or {})}}],
        }, 5)

    def test_late_code_evidence_survives_with_its_line_breaks(self):
        body = "move와 auto 관련 설명\n" * 70 + MARKET_CODE + "\n다른 내용\n" * 80
        context = self.context(body, '노션 "C++ 11" 문서의 MARKETTYPE_FUTOPT 코드 설명해줘',
                               page_fields={"matched_terms": ["C++"]})
        result = context["notion_pages"][0]
        self.assertIn("\n\t\tMARKETTYPE_FUTOPT\n", result["content"])
        self.assertIn("MARKETTYPE type = MARKETTYPE_STOCK;", result["content"])
        self.assertTrue(result.get("content_excerpted"))
        self.assertLessEqual(len(result["content"]), 520)

    def test_late_table_rows_are_selected_from_query_without_a_planner(self):
        body = "concept과 requires 관련 설명\n" + "앞 단원 설명\n" * 100 + AWAIT_TABLE + "\n기타 설명\n" * 80
        result = self.context(body, "노션 await_ready의 동작을 설명해줘")["notion_pages"][0]
        self.assertIn(AWAIT_TABLE, result["content"])
        self.assertNotIn("concept과 requires", result["content"])

    def test_keyword_filter_selects_evidence_for_a_contextual_question(self):
        body = "일반 안내\n" * 100 + AWAIT_TABLE + "\n다른 내용\n" * 100
        result = self.context(body, "그 조건의 본문 내용을 설명해줘", stage_meta={
            "constraints": {"common_filter_clauses": [
                {"field": "keyword", "operator": "contains", "value": "await_ready"},
            ]},
        })["notion_pages"][0]
        self.assertIn(AWAIT_TABLE, result["content"])

    def test_matched_terms_select_evidence_when_followup_has_no_subject(self):
        body = "일반 안내\n" * 100 + MARKET_CODE + "\n다른 내용\n" * 100
        result = self.context(body, "그 부분 설명해줘", page_fields={
            "matched_terms": ["MARKETTYPE_FUTOPT"],
        })["notion_pages"][0]
        self.assertIn("MARKETTYPE_FUTOPT", result["content"])

    def test_specific_query_excludes_unrelated_matched_code_and_tables(self):
        body = ("UNRELATED_CODE();\nUNRELATED_TABLE | 다른 설명\n" + "일반 내용\n" * 100
                + AWAIT_TABLE + "\n기타 설명\n" * 100)
        result = self.context(body, "await_ready 동작 설명해줘", page_fields={
            "matched_terms": ["UNRELATED_CODE", "UNRELATED_TABLE"],
        })["notion_pages"][0]
        self.assertIn(AWAIT_TABLE, result["content"])
        self.assertNotIn("UNRELATED_CODE", result["content"])
        self.assertNotIn("UNRELATED_TABLE", result["content"])

    def test_distant_query_evidence_is_not_replaced_by_one_prefix(self):
        body = ("일반 안내\n" * 100 + "MARKETTYPE_FUTOPT: 선물옵션\n" + "중간 설명\n" * 100
                + AWAIT_TABLE + "\n뒤쪽 설명\n" * 100)
        result = self.context(body, "MARKETTYPE_FUTOPT와 await_ready 설명해줘")["notion_pages"][0]
        self.assertIn("MARKETTYPE_FUTOPT: 선물옵션", result["content"])
        self.assertIn("suspend_never | await_ready()에서 true 리턴", result["content"])
        self.assertLessEqual(len(result["content"]), 520)

    def test_long_single_line_keeps_the_actual_matching_text(self):
        body = "앞" * 900 + "MARKETTYPE_FUTOPT" + "뒤" * 900
        result = self.context(body, "MARKETTYPE_FUTOPT 설명해줘")["notion_pages"][0]
        self.assertIn("MARKETTYPE_FUTOPT", result["content"])
        self.assertLessEqual(len(result["content"]), 520)

    def test_incomplete_source_status_survives_an_excerpt_without_tail_marker(self):
        body = "앞쪽 안내\n" * 100 + AWAIT_TABLE + "\n" + "뒤쪽 안내\n" * 100 + NOTION_CONTENT_INCOMPLETE
        context = self.context(body, "await_ready 설명해줘", stage_meta={"content_complete": False})
        result = context["notion_pages"][0]
        self.assertTrue(result.get("content_incomplete"))
        self.assertTrue(result.get("content_excerpted"))
        self.assertIs(context["stages"][0]["meta"].get("content_complete"), False)
        self.assertTrue(context["stages"][0]["meta"]["search_complete"])

    def test_short_source_keeps_code_indentation_without_an_excerpt_claim(self):
        result = self.context(MARKET_CODE, "C++ 11 설명해줘")["notion_pages"][0]
        self.assertEqual(result["content"], MARKET_CODE)
        self.assertIs(result.get("content_excerpted"), False)
        self.assertIs(result.get("content_incomplete"), False)

    def test_typed_table_evidence_beats_an_earlier_paragraph_with_same_keyword(self):
        row = "return_value(ValueType val) | co_return val 시 호출"
        body = "메모: return_value는 본문에서 다른 맥락으로 언급됨\n" + "일반 안내\n" * 100 + row
        result = self.context(body, "노션 표에 return_value 포함된 부분 설명해줘", page_fields={
            "matched_body_blocks": [{"block_id": "row", "type": "table_row", "text": row,
                                     "page_id": "page", "owner_page_id": "page"}],
        }, stage_meta={"constraints": {
            "body_filter_clauses": [{"field": "keyword", "operator": "contains",
                                     "value": "return_value", "block_type": "table_row"}],
        }})["notion_pages"][0]
        self.assertEqual(result["content"], row)
        self.assertTrue(result["content_excerpted"])

    def test_typed_code_beats_an_earlier_table_even_when_full_content_is_short(self):
        body = "참조 | MARKETTYPE_FUTOPT | 코드가 아닌 표의 설명\n" + MARKET_CODE
        result = self.context(body, "노션 코드에 MARKETTYPE_FUTOPT 포함된 부분 설명해줘", page_fields={
            "matched_body_blocks": [{"block_id": "code", "type": "code", "text": MARKET_CODE,
                                     "page_id": "page", "owner_page_id": "page"}],
            "content_incomplete": True,
        }, stage_meta={"constraints": {
            "body_filter_clauses": [{"field": "keyword", "operator": "contains",
                                     "value": "MARKETTYPE_FUTOPT", "block_type": "code"}],
        }})["notion_pages"][0]
        self.assertEqual(result["content"], MARKET_CODE)
        self.assertTrue(result["content_excerpted"])
        self.assertTrue(result["content_incomplete"])

    def test_new_followup_keyword_does_not_reuse_stale_typed_blocks_or_filters(self):
        old = "OLD_TOKEN | 이전 질문의 표 설명"
        new = "NEW_TOKEN | 이번 질문에 필요한 표 설명"
        body = old + "\n일반 안내\n" * 120 + new + "\n마지막 안내\n" * 120
        for query in ("이번에는 표에 NEW_TOKEN 포함된 부분 설명해줘", "NEW_TOKEN 설명해줘"):
            with self.subTest(query=query):
                result = self.context(body, query, page_fields={
                    "matched_body_blocks": [{"block_id": "old", "type": "table_row", "text": old,
                                             "page_id": "page", "owner_page_id": "page"}],
                }, stage_meta={"constraints": {
                    "common_filter_clauses": [{"field": "keyword", "operator": "contains", "value": "OLD_TOKEN"}],
                    "body_filter_clauses": [{"field": "keyword", "operator": "contains",
                                             "value": "OLD_TOKEN", "block_type": "table_row"}],
                }})["notion_pages"][0]
                self.assertIn(new, result["content"])
                self.assertNotIn(old, result["content"])

    def test_typed_keyword_matching_title_still_survives_a_long_code_excerpt(self):
        code = "// 다른 초기화 설명\n" * 100 + "return_value(actual_value);"
        result = self.context("제목: return_value\n" + code,
            "노션 'return_value' 문서에서 코드에 return_value 포함된 부분 설명해줘", page_fields={
                "title": "return_value",
                "matched_body_blocks": [{"block_id": "long-code", "type": "code", "text": code,
                                         "page_id": "page", "owner_page_id": "page"}],
            }, stage_meta={"constraints": {
                "body_filter_clauses": [{"field": "keyword", "operator": "contains",
                                         "value": "return_value", "block_type": "code"}],
            }})["notion_pages"][0]
        self.assertIn("return_value(actual_value);", result["content"])
        self.assertLessEqual(len(result["content"]), 520)


if __name__ == "__main__":
    unittest.main()
