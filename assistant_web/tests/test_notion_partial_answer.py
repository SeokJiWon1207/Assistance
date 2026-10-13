"""Partial Notion evidence must remain visible in model answers and final streams."""
import json
import sqlite3
import unittest
from contextlib import closing
from unittest.mock import patch

import requests
from backend import llm
from backend.audit import CHAT_STREAM_CONTEXT
from backend.evaluation import comparison, core, finalization
from backend.notion.client import NOTION_CONTENT_INCOMPLETE
from backend.schemas import ChatRequest
from backend.services import chat_service as chat
from backend.services.contracts import ChatPipelineContext


MODEL = {"id": "partial-answer-test", "model": "partial-answer-test", "status": "active",
         "provider": "openai-compatible", "endpoint": "https://model.example/chat"}
SCOPE_NOTICE = "Notion 전체 검색이 완료되지 않아, 확인한 범위의 근거로 답변했습니다."
BODY_NOTICE = "일부 문서 본문을 끝까지 읽지 못해 누락된 내용이 있을 수 있습니다."
ANSWER = "return_value는 반환값을 설정합니다."
QUESTION = "노션 return_value 코드를 설명해줘"


def retrieval(*, search_complete=True, content_complete=True, status="ok"):
    return {"query": QUESTION, "constraints": {}, "intent_plan": {"source": "notion"},
            "stages": [{"source": "notion", "status": status,
                        "meta": {"search_complete": search_complete, "content_complete": content_complete,
                                 "candidate_count": 1, "displayed_count": 1},
                        "notion_pages": [{"id": "evidence-page", "title": "반환값 설명", "database_label": "기술 공유",
                                          "content": ANSWER, "url": "https://notion.so/evidence-page"}]}]}


def model_response(content, stream):
    response = requests.Response()
    response.status_code = 200
    response.encoding = "utf-8"
    if stream:
        response.headers["content-type"] = "text/event-stream"
        payload = {"choices": [{"delta": {"content": content}}]}
        response._content = ("data: " + json.dumps(payload, ensure_ascii=False) + "\n\ndata: [DONE]\n\n").encode()
    else:
        response.headers["content-type"] = "application/json"
        response._content = json.dumps({"choices": [{"message": {"content": content}}]}, ensure_ascii=False).encode()
    response._content_consumed = True
    return response


class NotionPartialAnswerTests(unittest.TestCase):
    def setUp(self):
        llm.reset_model_circuits()
        self.addCleanup(llm.reset_model_circuits)

    def generate(self, evidence, *, content=ANSWER, stream=False):
        request = ChatRequest(modelId=MODEL["id"], messages=[{"role": "user", "content": QUESTION}],
                              stream=stream, abTest=False)
        with patch.object(chat, "get_model_registry", return_value=[]), \
             patch.object(chat, "get_model_api_key", return_value="test-key"), \
             patch.object(llm.requests, "post", return_value=model_response(content, stream)):
            response = chat.generate_model_response(request, MODEL, [{"role": "user", "content": QUESTION}],
                                                    evidence, {}, 5)
        return request, response

    def test_partial_search_keeps_correct_answer_but_exposes_scope(self):
        _, response = self.generate(retrieval(search_complete=False, status="partial_error"))
        self.assertIn(ANSWER, response["content"])
        self.assertIn(SCOPE_NOTICE, response["content"])
        self.assertNotIn(BODY_NOTICE, response["content"])

    def test_partial_content_is_separate_from_search_scope(self):
        _, response = self.generate(retrieval(content_complete=False))
        self.assertIn(BODY_NOTICE, response["content"])
        self.assertNotIn(SCOPE_NOTICE, response["content"])

    def test_page_incomplete_marker_is_not_hidden_by_missing_stage_flag(self):
        evidence = retrieval()
        evidence["stages"][0]["meta"].pop("content_complete")
        evidence["stages"][0]["notion_pages"][0]["content"] += "\n" + NOTION_CONTENT_INCOMPLETE
        _, response = self.generate(evidence)
        self.assertIn(BODY_NOTICE, response["content"])

    def test_complete_or_non_notion_evidence_does_not_add_notice(self):
        for evidence in (retrieval(), None, {"stages": [{"source": "mantis_page_scan", "status": "partial_error",
                                                     "meta": {"search_complete": False}}]}):
            with self.subTest(evidence=evidence):
                _, response = self.generate(evidence)
                self.assertEqual(response["content"], ANSWER)

    def test_existing_model_notice_is_not_duplicated(self):
        _, response = self.generate(retrieval(search_complete=False), content=ANSWER + "\n\n" + SCOPE_NOTICE)
        self.assertEqual(response["content"].count(SCOPE_NOTICE), 1)

    def test_verified_stream_and_persisted_answer_include_identical_notices(self):
        events = []
        with patch.object(CHAT_STREAM_CONTEXT, "emitter", events.append, create=True), \
             patch.object(CHAT_STREAM_CONTEXT, "content_streamed", False, create=True):
            request, response = self.generate(retrieval(search_complete=False, content_complete=False), stream=True)
            self.assertFalse(any(event["type"] == "delta" for event in events))
            finalized = finalization.finalize_chat_response(request, QUESTION, response, user_uid="partial-answer-test")
        streamed = "".join(event["content"] for event in events if event["type"] == "delta")
        self.assertEqual(streamed, finalized["content"])
        self.assertIn(SCOPE_NOTICE, streamed)
        self.assertIn(BODY_NOTICE, streamed)
        self.assertEqual(finalized["verification"]["delivery"], "allowed")
        with closing(sqlite3.connect(finalization.AUDIT_DB_PATH)) as connection:
            saved = connection.execute("SELECT payload FROM validation_logs WHERE id = ?", (finalized["auditId"],)).fetchone()
        self.assertEqual(json.loads(saved[0])["answer"], streamed)

    def test_deterministic_list_keeps_its_existing_single_scope_notice(self):
        question = "노션 문서 목록 보여줘"
        evidence = retrieval(search_complete=False)
        evidence["query"] = question
        request = ChatRequest(modelId=MODEL["id"], messages=[{"role": "user", "content": question}], abTest=False)
        context = ChatPipelineContext(latest_user_text=question, model=MODEL, api_key="", messages=[],
            retrieval_text=question, enabled_tools={"notion": True}, system_notes=[], query_constraints={},
            active_limit=5, intent_plan={"source": "notion"}, retrieval_result=evidence)
        response = chat.deterministic_retrieval_response(request, context, 5, None)
        self.assertEqual(response["raw"]["mode"], "deterministic_notion_list")
        self.assertEqual(response["content"].count("일부 항목을 아직 확인하지 못해 전체 건수와 다를 수 있습니다."), 1)
        self.assertNotIn(SCOPE_NOTICE, response["content"])

    def test_notion_timeout_fallback_shows_known_documents_and_partial_body_notice(self):
        request = ChatRequest(modelId=MODEL["id"], messages=[{"role": "user", "content": QUESTION}], abTest=False)
        evidence = retrieval(search_complete=False, content_complete=False)
        with patch.object(chat, "get_model_registry", return_value=[]), \
             patch.object(chat, "get_model_api_key", return_value="test-key"), \
             patch.object(llm.requests, "post", side_effect=requests.Timeout("fixture timeout")):
            response = chat.generate_model_response(request, MODEL, [{"role": "user", "content": QUESTION}],
                                                    evidence, {}, 5)
        self.assertEqual(response["raw"]["mode"], "model_timeout_fallback")
        self.assertIn("반환값 설명", response["content"])
        self.assertNotIn("이슈를 찾지 못했습니다", response["content"])
        self.assertIn(BODY_NOTICE, response["content"])
        self.assertEqual(response["content"].count("일부 항목을 아직 확인하지 못해 전체 건수와 다를 수 있습니다."), 1)
        self.assertNotIn(SCOPE_NOTICE, response["content"])

    def test_ab_baseline_and_candidate_preserve_partial_notices_before_final_delivery(self):
        for candidate_id in ("", "candidate-model"):
            with self.subTest(candidate_id=candidate_id):
                evidence = retrieval(search_complete=False, content_complete=False)
                request, response = self.generate(evidence)
                request.abTest = True
                request.abModelId = candidate_id
                with patch.object(core, "get_private_model", side_effect=lambda identifier: {**MODEL, "id": identifier}), \
                     patch.object(comparison, "get_model_api_key", return_value="test-key"), \
                     patch.object(llm.requests, "post", return_value=model_response(ANSWER, False)):
                    finalized = finalization.finalize_chat_response(request, QUESTION, response,
                                                                   user_uid="partial-ab-test")
                variant_b = finalized["abTest"]["variantB"]
                self.assertEqual(variant_b["status"], "ok")
                self.assertIn(SCOPE_NOTICE, variant_b["content"])
                self.assertIn(BODY_NOTICE, variant_b["content"])
                self.assertEqual(variant_b["content"].count(SCOPE_NOTICE), 1)


if __name__ == "__main__":
    unittest.main()
