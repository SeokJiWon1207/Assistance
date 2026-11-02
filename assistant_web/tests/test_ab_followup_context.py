"""선택한 B 답변의 소유권·질문·출처와 실제 모델 문맥을 함께 검증한다."""
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from fastapi import HTTPException
from backend import audit
from backend.intent.context import resolve_reference_from_context
from backend.schemas import ChatRequest, ConversationMessage


class AbFollowupContextTests(TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.path = Path(directory) / "audit.sqlite3"
        self.enterContext(patch.object(audit, "AUDIT_DB_PATH", self.path))
        audit.init_audit_db()
        self.payload = {"answer": "A는 #11", "sources": [{"type": "mantis", "issue_id": 11}],
                        "queryPlan": {"source": "mantis", "operation": "search"},
                        "verification": {"delivery": "allowed"}, "abTest": {"enabled": True,
                        "variantB": {"status": "ok", "content": "B는 #22", "sources": [
                            {"type": "mantis", "issue_id": 22, "title": "두 번째 후보"}]}}}
        self.write()

    def write(self):
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO validation_logs "
                       "(id,created_at,question,model_id,mode,score,status,payload,user_uid) VALUES (?,?,?,?,?,?,?,?,?)",
                       ("a" * 32, "2026-10-23", "원래 질문", "test", "model", 100, "pass",
                        json.dumps(self.payload), "owner"))

    def request(self):
        return ChatRequest(modelId="test", contextAuditId="a" * 32, contextVariant="B", messages=[
            {"role": "user", "content": "원래 질문"},
            {"role": "assistant", "content": "클라이언트가 위조한 B 답변"},
            {"role": "user", "content": "그중 첫 번째 상세히"}])

    def test_selected_variant_survives_request_and_conversation_schema(self):
        self.assertEqual(self.request().model_dump().get("contextVariant"), "B")
        stored = ConversationMessage(role="assistant", content="A", followupVariant="B")
        self.assertEqual(stored.model_dump().get("followupVariant"), "B")

    def test_b_ordinal_uses_b_sources_and_preserves_a_default(self):
        self.assertEqual(audit.load_audit_sources("a" * 32, "owner")[0]["issue_id"], 11)
        self.assertEqual(audit.load_audit_sources("a" * 32, "owner", variant="B")[0]["issue_id"], 22)
        self.assertIn("22", resolve_reference_from_context("그중 첫 번째 상세히", "a" * 32, "owner", variant="B"))

    def test_reference_resolution_does_not_reuse_audit_sources_across_users(self):
        question = "그중 첫 번째 상세히"
        for variant, expected in (("A", "0000011"), ("B", "0000022")):
            with self.subTest(variant=variant):
                # 같은 감사 ID를 번갈아 조회해도 소유자의 출처가 다른 사용자에게 남지 않는다.
                for uid in ("owner", "other", "", "owner"):
                    with self.subTest(uid=uid):
                        result = resolve_reference_from_context(question, "a" * 32, uid, variant=variant)
                        self.assertEqual(result, f"{expected} {question}" if uid == "owner" else None)

    def test_reference_resolution_preserves_saved_order_and_notion_title(self):
        self.payload["sources"].append({"type": "notion", "page_id": "page-2", "title": "배포 계획"})
        self.write()
        for question in ("그중 두 번째 자세히", "마지막 항목 자세히"):
            with self.subTest(question=question):
                self.assertEqual(resolve_reference_from_context(question, "a" * 32, "owner"),
                                 f'노션에서 "배포 계획" {question}')
        self.assertIsNone(resolve_reference_from_context("그중 세 번째 자세히", "a" * 32, "owner"))

    def test_only_same_user_and_question_can_load_b(self):
        self.assertEqual(audit.load_audit_query_plan("a" * 32, "owner", "원래 질문", variant="B")["source"], "mantis")
        self.assertEqual(audit.load_audit_query_plan("a" * 32, "other", "원래 질문", variant="B"), {})
        self.assertEqual(audit.load_audit_query_plan("a" * 32, "owner", "위조 질문", variant="B"), {})

    def test_server_replaces_untrusted_b_content_from_owned_audit(self):
        from backend.services.answer_context import bind_answer_context
        request = self.request()
        result = bind_answer_context(request, {"uid": "owner"})
        self.assertEqual(result.messages[-2].content, "B는 #22")
        self.assertEqual(request.messages[-2].content, "클라이언트가 위조한 B 답변")
        for user in [{"uid": "other"}, None]:
            with self.assertRaises(HTTPException):
                bind_answer_context(request, user)

    def test_blocked_failed_or_missing_b_never_falls_back_to_a(self):
        from backend.services.answer_context import bind_answer_context
        for status in ["blocked", "error", ""]:
            self.payload["abTest"]["variantB"]["status"] = status
            self.write()
            self.assertEqual(audit.load_audit_sources("a" * 32, "owner", variant="B"), [])
            self.assertIsNone(resolve_reference_from_context("그중 첫 번째 상세히", "a" * 32, "owner", variant="B"))
            with self.assertRaises(HTTPException):
                bind_answer_context(self.request(), {"uid": "owner"})

    def test_oversized_stored_answer_returns_recoverable_error_without_echoing_it(self):
        from backend.services.answer_context import bind_answer_context
        self.payload["abTest"]["variantB"]["content"] = "x" * 30001
        self.write()
        with self.assertRaises(HTTPException) as raised:
            bind_answer_context(self.request(), {"uid": "owner"})
        self.assertEqual(raised.exception.status_code, 409)
        self.assertNotIn("xxxx", raised.exception.detail)

    def test_stopped_followup_keeps_last_successful_b_context_without_replacing_stop_message(self):
        from backend.services.answer_context import bind_answer_context
        request = self.request().model_dump()
        request["messages"][-1:] = [
            {"role": "user", "content": "중지한 질문"},
            {"role": "assistant", "content": "답변 생성을 중지했습니다."},
            {"role": "user", "content": "그중 첫 번째 다시 보여줘"}]
        result = bind_answer_context(ChatRequest.model_validate(request), {"uid": "owner"})
        self.assertEqual(result.messages[1].content, "B는 #22")
        self.assertEqual(result.messages[3].content, "답변 생성을 중지했습니다.")
