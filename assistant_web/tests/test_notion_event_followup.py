"""회식 공지의 검색 대상과 본문 질문을 구분하는 회귀 검증."""

import unittest
from unittest.mock import Mock, patch

from backend.intent.context import is_follow_up_question
from backend.intent.source_router import rule_based_search_intent
from backend.notion import search
from backend.notion.formatting import has_deterministic_notion_list_intent
from backend.notion.query_cache import clear_notion_query_cache
from backend.notion.client import NOTION_CONTENT_INCOMPLETE
from backend.retrieval import notion_question_needs_blocks
from backend.schemas import ChatMessage, ChatRequest
from backend.services import chat_service as chat


class NotionEventSearchTests(unittest.TestCase):
    def setUp(self):
        clear_notion_query_cache()
        self.addCleanup(clear_notion_query_cache)

    def lookup(self, question, terms, titles=None, constraints=None):
        pages = [
            {"id": str(index), "title": title, "search_text": title,
             "last_edited_time": "2026-10-07", "properties_text": ""}
            for index, title in enumerate(titles or [
                "2026년 3분기 회식관련 공지", "2026년 1분기 회식관련 공지",
            ])
        ]

        def matches(page, filters):
            if "and" in filters:
                return all(matches(page, value) for value in filters["and"])
            if "or" in filters:
                return any(matches(page, value) for value in filters["or"])
            value = filters.get("title", {})
            return (value.get("contains", "") in page["title"]
                    and ("equals" not in value or page["title"] == value["equals"]))

        def query(_url, payload, **_kwargs):
            response = Mock(status_code=200)
            response.json.return_value = {
                "results": [page for page in pages if matches(page, payload.get("filter", {}))],
                "has_more": False,
            }
            return response

        with (
            patch.object(search, "get_notion_settings", return_value={"token": "test", "database_ids": ["db-event"]}),
            patch.object(search, "resolve_notion_read_scope", return_value=({"db-event": "db-event"}, True)),
            patch.object(search, "fetch_notion_database_title", return_value="주요이슈현황"),
            patch.object(search, "notion_property_schema", return_value={"제목": {"type": "title"}}),
            patch.object(search, "normalize_notion_page", side_effect=lambda raw, **kwargs: {**raw, **kwargs}),
            patch.object(search, "notion_post_json", side_effect=query) as post,
            patch.object(search, "fetch_notion_page_blocks", return_value="일시 :\n장소 :") as blocks,
        ):
            result = search.execute_notion_search(question, planned_terms=terms, constraints=constraints)
        return result, post, blocks

    def test_place_question_finds_notice_without_place_in_its_title(self):
        for terms in (["3분기", "회식", "장소"], None):
            with self.subTest(terms=terms):
                clear_notion_query_cache()
                result, post, blocks = self.lookup("3분기 회식 어디서해", terms)
                self.assertEqual([page["title"] for page in result["notion_pages"]], ["2026년 3분기 회식관련 공지"])
                self.assertEqual(result["meta"]["terms"], ["3분기", "회식"])
                self.assertEqual(post.call_count, 1)
                self.assertEqual(blocks.call_count, 1)

    def test_omitted_quarter_is_restored_before_search(self):
        result, _, _ = self.lookup("3분기 회식일정 보여줘", ["회식"])
        self.assertEqual([page["title"] for page in result["notion_pages"]], ["2026년 3분기 회식관련 공지"])

    def test_place_topic_and_quoted_title_remain_search_conditions(self):
        for question, terms, title in (
            ("노션에서 장소 대관 규정 찾아줘", ["장소", "대관", "규정"], "장소 대관 규정"),
            ('노션에서 "3분기 회식 장소" 상세 내용 알려줘', ["3분기", "회식", "장소"], "3분기 회식 장소"),
            ("노션에서 장소 확인서 찾아줘", None, "장소 확인서"),
        ):
            with self.subTest(question=question):
                clear_notion_query_cache()
                result, _, _ = self.lookup(question, terms, [title, "3분기 회식 공지"])
                self.assertEqual([page["title"] for page in result["notion_pages"]], [title])

    def test_excluded_quarter_does_not_return_as_a_positive_condition(self):
        result, _, _ = self.lookup("3분기 말고 4분기 회식 보여줘", ["4분기", "회식"],
                                  ["2026년 4분기 회식 공지", "2026년 3분기 회식 공지"])
        self.assertEqual([page["title"] for page in result["notion_pages"]], ["2026년 4분기 회식 공지"])

    def test_registration_quarter_is_not_a_document_title_condition(self):
        constraints = {"filter_clauses": [{"field": "created_at", "operator": "between",
                                           "value": ["2026-07-01", "2026-09-30"]}]}
        result, _, _ = self.lookup("3분기에 등록된 회식 문서 찾아줘", ["회식"],
                                  ["회식 공지"], constraints)
        self.assertEqual(result["meta"]["terms"], ["회식"])

    def test_event_quarter_survives_a_separate_registration_period(self):
        constraints = {"filter_clauses": [{"field": "created_at", "operator": "between",
                                           "value": ["2026-10-01", "2026-10-31"]}]}
        result, _, _ = self.lookup("노션에서 이번 달에 등록된 3분기 회식 공지 보여줘",
                                  ["3분기", "회식"], constraints=constraints)
        self.assertEqual(result["meta"]["terms"], ["3분기", "회식"])


class NotionReadFollowUpTests(unittest.TestCase):
    def prepare(self, texts, *, prior_source="notion", grounding_text=""):
        messages = []
        for text in texts[:-1]:
            messages.extend([ChatMessage(role="user", content=text), ChatMessage(role="assistant", content="이전 답변")])
        messages.append(ChatMessage(role="user", content=texts[-1]))
        request = ChatRequest(modelId="test", contextAuditId="a" * 32, messages=messages)
        with (
            patch.object(chat, "get_private_model", return_value={"id": "test", "status": "active"}),
            patch.object(chat, "get_model_api_key", return_value="test"),
            patch.object(chat, "load_audit_query_plan", return_value={"source": prior_source, "grounding_text": grounding_text}),
            patch.object(chat, "resolve_reference_from_context", return_value=None),
            patch.object(chat, "rewrite_follow_up_query_with_model", return_value=None),
        ):
            return chat.prepare_chat_pipeline(request, texts[-1], {"uid": "test-user"})

    def test_affirmative_read_reuses_the_notion_topic_and_reads_body(self):
        for previous in ("노션에서 3분기 회식일정 보여줘", "3분기 회식일정 보여줘"):
            with self.subTest(previous=previous):
                context = self.prepare([previous, "응 보여줘"])
                self.assertTrue(is_follow_up_question("응 보여줘"))
                self.assertIn("3분기", context.retrieval_text)
                self.assertIn("회식", context.retrieval_text)
                self.assertEqual(rule_based_search_intent(context.retrieval_text, context.query_constraints)["source"], "notion")
                self.assertTrue(notion_question_needs_blocks(context.retrieval_text))
                self.assertFalse(has_deterministic_notion_list_intent(context.retrieval_text))

    def test_followup_chain_keeps_topic_without_crossing_an_independent_turn(self):
        context = self.prepare(["3분기 회식일정 보여줘", "응 보여줘", "네 자세히 알려줘"])
        self.assertIn("3분기", context.retrieval_text)
        self.assertEqual(search.inherit_notion_context([
            "응 보여줘", "파이썬 사용법 알려줘", "노션에서 3분기 회식 찾아줘",
        ]), "")

    def test_affirmative_read_keeps_the_last_confirmed_status_change(self):
        context = self.prepare([
            "노션에서 결재요청 상태 문서 보여줘", "그중 결재완료 상태만 보여줘", "응 보여줘",
        ], grounding_text="노션 결재완료 상태 문서 보여줘")
        self.assertEqual(search.extract_notion_constraints(context.retrieval_text)["status"], "결재완료")
        self.assertNotIn("결재요청", context.retrieval_text)
        self.assertEqual(search.inherit_notion_context([
            "응 보여줘", "그중 결재완료 상태만 보여줘", "노션에서 결재요청 상태 문서 보여줘",
        ]), "")

    def test_affirmation_alone_does_not_trigger_a_read_followup(self):
        for text in ("응", "네", "응 전송해줘", "네 수정해줘"):
            with self.subTest(text=text):
                self.assertFalse(is_follow_up_question(text))

    def test_empty_notion_search_gives_document_specific_guidance(self):
        question = "3분기 회식 어디서해"
        context = chat.ChatPipelineContext(question, {"id": "test"}, "test", [], question,
                                           {"notion": True}, [], {}, 5)
        context.intent_plan = {"source": "notion"}
        request = ChatRequest(modelId="test", messages=[ChatMessage(role="user", content=question)])
        with (
            patch.object(chat, "should_retrieve_chat_evidence", return_value=True),
            patch.object(chat, "retrieval_limit", return_value=(5, None)),
            patch.object(chat, "run_retrieval_pipeline", return_value={"stages": [{"source": "notion", "notion_pages": []}]}),
            patch.object(chat, "deterministic_retrieval_response", return_value=None),
        ):
            response = chat.retrieve_chat_evidence(request, context).response
        self.assertEqual(response["raw"]["mode"], "empty_evidence")
        self.assertIn("문서 제목", response["content"])
        self.assertNotIn("고객사", response["content"])

    def test_blank_place_is_reported_without_turning_goal_date_into_a_decision(self):
        question = "3분기 회식 어디서해"
        page = {"id": "page", "title": "2026년 3분기 회식관련 공지", "url": "https://www.notion.so/page",
                "due_date": "2026-10-16", "content": "일시 :\n장소 :\n회식장소: 1.참나무본가,3.이춘복참치\n회식장소: 4.신도림삼백가"}
        context = chat.ChatPipelineContext(question, {"id": "test"}, "test", [], question,
                                           {"notion": True}, [], {}, 5)
        context.intent_plan = {"source": "notion"}
        context.retrieval_result = {"intent_plan": {"source": "notion"},
                                    "stages": [{"source": "notion", "status": "ok", "notion_pages": [page]}]}
        request = ChatRequest(modelId="test", messages=[ChatMessage(role="user", content=question)])
        response = chat.deterministic_retrieval_response(request, context, 5, None)
        self.assertIsNotNone(response)
        self.assertIn("미기재", response["content"])
        self.assertIn("참나무본가", response["content"])
        self.assertNotIn("2026-10-16", response["content"])
        self.assertNotIn("아직 확정", response["content"])
        self.assertEqual(response["sources"][0]["page_id"], "page")

    def test_place_and_schedule_requests_read_bodies_even_with_a_lookup_verb(self):
        for question in ("3분기 회식 장소 알려줘", "3분기 회식 일정 확인해줘"):
            with self.subTest(question=question):
                self.assertTrue(notion_question_needs_blocks(question))

    def test_filled_place_is_not_hidden_by_an_empty_template_field(self):
        from backend.notion.formatting import format_notion_missing_information_answer
        self.assertEqual(format_notion_missing_information_answer("회식 어디서해", [{
            "title": "회식 공지", "content": "장소 :\n장소 : 참나무본가",
        }]), "")

    def test_notion_nonbreaking_spaces_do_not_hide_blank_fields(self):
        from backend.notion.formatting import format_notion_missing_information_answer
        self.assertIn("미기재", format_notion_missing_information_answer("회식 어디서해", [{
            "title": "회식 공지", "content": "일시\u00a0:\n장소\u00a0:",
        }]))

    def test_partial_body_reports_only_the_checked_field(self):
        from backend.notion.formatting import format_notion_missing_information_answer
        answer = format_notion_missing_information_answer("회식 어디서해", [{
            "title": "회식 공지", "content": "장소 :\n" + NOTION_CONTENT_INCOMPLETE,
        }])
        self.assertIn("확인한", answer)
        self.assertIn("일부", answer)
        self.assertNotIn("원문에 미기재", answer)

    def test_filled_place_property_is_not_hidden_by_a_blank_body_field(self):
        from backend.notion.formatting import format_notion_missing_information_answer
        self.assertEqual(format_notion_missing_information_answer("회식 어디서해", [{
            "title": "회식 공지", "content": "장소 :", "properties_text": "장소: 참나무본가",
        }]), "")

    def test_place_label_alone_does_not_imply_a_vote(self):
        from backend.notion.formatting import format_notion_missing_information_answer
        answer = format_notion_missing_information_answer("회식 어디서해", [{
            "title": "회식 공지", "content": "장소 :\n회식장소: 참나무본가",
        }])
        self.assertIn("참나무본가", answer)
        self.assertNotIn("투표", answer)


if __name__ == "__main__":
    unittest.main()
