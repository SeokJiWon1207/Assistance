"""고객사 하나와 수량 하나가 함께 있는 후속 검색의 회귀 검사."""
from contextlib import closing

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend import audit
from backend.intent.constraints import extract_query_constraints
from backend.intent.confirmed_context import confirmed_scope_text
from backend.intent.context import build_contextual_retrieval_text
from backend.intent.status_semantics import resolve_status_intent
from backend.schemas import ChatMessage, ChatRequest
from backend.services import chat_service as chat
from backend.sources.mantis import select_mantis_rows
from scripts.accuracy.dynamic_accuracy_check import ACCURACY_USER_UID, execute_chat


class FollowUpCustomerScopeTests(unittest.TestCase):
    def test_quantity_does_not_remove_a_separate_hana_customer(self):
        for question in (
            "하나 상태 종료 가장 최근 이슈 하나만 보여줘",
            "하나에서 최근 이슈 하나를 알려줘",
            "고객사 하나의 결과 하나씩 보여줘",
        ):
            with self.subTest(question=question):
                constraints = extract_query_constraints(question)
                self.assertEqual(constraints["customer_key"], "하나")
                self.assertTrue(any(c["field"] == "customer" for c in constraints["filter_clauses"]))

    def test_quantity_alone_does_not_add_hana_customer_or_exclusion(self):
        for question in (
            "가장 최근 이슈 하나만 보여줘",
            "항목 하나를 알려줘",
            "NH 이슈 하나만 보여줘",
            "NH 이슈 하나만 제외하고 보여줘",
        ):
            with self.subTest(question=question):
                constraints = extract_query_constraints(question)
                self.assertEqual(constraints["customer_key"], "NH" if question.startswith("NH") else None)
                self.assertEqual(constraints["excluded_customer_keys"], [])

    def test_explicit_hana_exclusion_survives_a_quantity(self):
        constraints = extract_query_constraints("하나 제외 NH 최근 이슈 하나만 보여줘")
        self.assertEqual(constraints["customer_key"], "NH")
        self.assertEqual(constraints["excluded_customer_keys"], ["하나"])

    def test_followup_preserves_the_matched_customer_and_exact_project(self):
        for customer in ("하나금융", "하나", "134Hanaw(하나금융)"):
            previous = f"{customer}에서 상태가 closed인 최근 이슈 2개 보여줘"
            with self.subTest(customer=customer):
                query, rewrite = build_contextual_retrieval_text([
                    ChatMessage(role="user", content=previous),
                    ChatMessage(role="user", content="방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘"),
                ])
                expected = extract_query_constraints(previous)
                actual = extract_query_constraints(query)
                self.assertFalse(rewrite)
                self.assertEqual(actual["customer_key"], expected["customer_key"])
                self.assertEqual(actual["customer_exact"], expected["customer_exact"])
                self.assertEqual(actual["statuses"], ["closed"])

    def test_anonymous_followup_filters_out_newer_other_customer_issue(self):
        question = "방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘"
        request = ChatRequest(modelId="fixture", messages=[
            ChatMessage(role="user", content="하나금융에서 상태가 closed인 최근 이슈 2개 보여줘"),
            ChatMessage(role="assistant", content="#31571, #31554"),
            ChatMessage(role="user", content=question),
        ])
        with (
            patch.object(chat, "get_private_model", return_value={"id": "fixture", "status": "active"}),
            patch.object(chat, "get_model_api_key", return_value="fixture-key"),
            patch.object(chat, "rewrite_follow_up_query_with_model", side_effect=AssertionError("모델 호출 금지")),
        ):
            context = chat.prepare_chat_pipeline(request, question, None)
        rows = [
            {"id": 31583, "summary": "차트 문의", "handler": {"real_name": "석지원"}, "project": {"name": "112KBStar(KB증권)"}, "status": {"name": "closed"}},
            {"id": 31571, "summary": "서버 점검", "handler": {"real_name": "석지원"}, "project": {"name": "134Hanaw(하나금융)"}, "status": {"name": "closed"}},
        ]
        self.assertEqual(context.query_constraints["customer_key"], "하나금융")
        self.assertEqual([row["id"] for row in select_mantis_rows(rows, context.query_constraints)], [31571])

    def test_accuracy_owner_restores_confirmed_constraints_from_audit(self):
        previous = "하나금융에서 상태가 closed인 최근 이슈 2개 보여줘"
        question = "방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘"
        constraints = extract_query_constraints(previous)
        plan = {"source": "mantis", "resolved_constraints": constraints,
                "status_resolution": resolve_status_intent(previous, constraints), "terms": []}
        request = ChatRequest(modelId="fixture", contextAuditId="a" * 32, messages=[
            ChatMessage(role="user", content=previous),
            ChatMessage(role="assistant", content="#31571, #31554"),
            ChatMessage(role="user", content=question),
        ])
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "audit.sqlite3"
            with closing(sqlite3.connect(db)) as connection, connection:
                connection.execute("CREATE TABLE validation_logs (id TEXT, user_uid TEXT, question TEXT, payload TEXT)")
                connection.execute("INSERT INTO validation_logs VALUES (?, ?, ?, ?)",
                                   ("a" * 32, ACCURACY_USER_UID, previous, json.dumps({"queryPlan": plan})))
            with (
                patch.object(audit, "AUDIT_DB_PATH", db),
                patch.object(audit, "init_audit_db"),
                patch.object(chat, "get_private_model", return_value={"id": "fixture", "status": "active"}),
                patch.object(chat, "get_model_api_key", return_value="fixture-key"),
                patch.object(chat, "rewrite_follow_up_query_with_model", side_effect=AssertionError("모델 호출 금지")),
                patch.object(chat.chat_service, "build_response",
                             side_effect=lambda req, user: chat.prepare_chat_pipeline(req, question, user)),
            ):
                context = execute_chat(request)
            self.assertEqual(context.query_constraints["customer_key"], "하나금융")
            self.assertEqual(context.query_constraints["inherited_from_audit_id"], "a" * 32)
            self.assertEqual(context.query_constraints["statuses"], ["closed"])

    def test_saved_status_stays_explicit_in_followup_grounding(self):
        for status in ("confirmed", "resolved", "acknowledged", "closed"):
            previous = f"115다올투자증권(구KTB) 상태 {status} 최근 이슈 2개 보여줘"
            with self.subTest(status=status):
                constraints = extract_query_constraints(previous)
                scope = confirmed_scope_text(constraints)
                query, rewrite = build_contextual_retrieval_text([
                    ChatMessage(role="user", content=scope),
                    ChatMessage(role="user", content="방금 조건에서 가장 최근 이슈 하나만 자세히 알려줘"),
                ])
                actual = extract_query_constraints(query)
                self.assertFalse(rewrite)
                self.assertEqual(actual["statuses"], constraints["statuses"])
                self.assertEqual(actual["customer_exact"], constraints["customer_exact"])
