"""고정 플래너 응답, 장애 경로, 실제 보고서 및 상태 상속 계약."""

import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from backend import audit, workspace
from backend.intent import status_semantics as semantic
from backend.intent.constraints import extract_query_constraints
from backend.intent.matching import text_matches_status
from backend.evaluation.verification import ungrounded_constraint_labels, build_verification_report
from backend.mantis_status import STATUS_NAMES, OPEN_STATUSES, mantis_status_bucket
from backend.query_filters import matches_source_filters
from backend.query_planning import build_query_plan, validate_query_plan
from backend.reports import sla
from backend.schemas import ChatRequest, ChatMessage
from backend.services import chat_service as chat
from backend.sources.mantis_collection import IssueBatch
from backend.sources.mantis import issue_matches_constraints
from backend.sources.mantis_store import render_document
from backend.intent.matching import chroma_text_matches_constraints


CASES = json.loads((Path(__file__).parent / "fixtures/status_semantic_cases.json").read_text())


class StatusDefinitionTests(unittest.TestCase):
    def test_count_document_and_raw_list_keep_same_missing_media_fallback(self):
        for media, expected in (("", True), ("MTS", True), ("HTS", False)):
            issue = {"id": 21371, "project": {"name": "120NH(NH증권)"},
                     "summary": "MTS 차트 문의", "status": {"name": "assigned"},
                     "custom_fields": [{"field": {"name": "매체구분"}, "value": media}]}
            constraints = extract_query_constraints("NH MTS 미종료 이슈")
            self.assertEqual(issue_matches_constraints(issue, constraints), expected)
            self.assertEqual(chroma_text_matches_constraints(render_document(issue), constraints), expected)

    def test_all_languages_and_paths_share_the_same_bucket(self):
        for canonical, aliases in STATUS_NAMES.items():
            for alias in aliases:
                with self.subTest(alias=alias):
                    opened = canonical in OPEN_STATUSES
                    self.assertEqual(mantis_status_bucket(alias), "open" if opened else "done")
                    self.assertEqual(workspace._is_open(alias), opened)
                    self.assertEqual(sla._is_closed(alias), not opened)
                    self.assertEqual(text_matches_status(alias, ["open"]), opened)
                    for source in ("mantis", "chroma"):
                        self.assertEqual(matches_source_filters(source, {"status": alias}, [
                            {"field": "status", "operator": "in", "value": ["open"]},
                        ]), opened)
        for unknown in ("", "알 수 없음", "future_status"):
            self.assertEqual(mantis_status_bucket(unknown), "unknown")
            self.assertFalse(workspace._is_open(unknown))


class StatusSemanticTests(unittest.TestCase):
    def setUp(self):
        for target, value in (("get_model_registry", [{"id": "fixed-planner", "status": "active"}]),
                              ("get_model_api_key", "synthetic")):
            p = patch.object(semantic, target, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {"STATUS_PLANNER_MODEL_ID": "fixed-planner"})
        p.start()
        self.addCleanup(p.stop)

    def resolve(self, question, payload):
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]}
        with patch.object(semantic, "post_model_chat", return_value=response) as post:
            result = semantic.resolve_status_intent(question, extract_query_constraints(question))
        return result, post

    def test_fixture_success_and_timeout_paths(self):
        for case in CASES:
            q = case["question"]
            with self.subTest(question=q):
                result, post = self.resolve(q, {"status_intent": case["intent"], "evidence": case["evidence"],
                                               "needs_clarification": case["clarify"]})
                self.assertEqual(result["status_intent"], case["intent"])
                self.assertEqual(result["needs_clarification"], case["clarify"])
                neutral = semantic.is_structured_status_neutral_query(q, extract_query_constraints(q))
                if neutral:
                    post.assert_not_called()
                else:
                    self.assertEqual(result["model_id"], "fixed-planner")
                    self.assertEqual(post.call_args.args[0]["id"], "fixed-planner")
                with patch.object(semantic, "post_model_chat", side_effect=requests.Timeout):
                    failed = semantic.resolve_status_intent(q, extract_query_constraints(q))
                self.assertEqual(failed["needs_clarification"], not neutral)
                self.assertIsNone(failed["status_intent"])

    def test_structured_lookups_do_not_depend_on_status_model_availability(self):
        for q in (
            "동부증권 최근 이슈 3개 보여줘",
            "하나금융 HTS 최근 이슈 2개만 최신순으로 보여줘",
            "유안타증권 이슈에서 HTS는 빼고 최근 3개만 보여줘",
            "담당자 cs이슈의 최근 이슈 2개 보여줘",
            "화면번호 6110 관련 이슈 3개 보여줘",
            "최신 800건 기준으로 2025년 7월에 교보증권에서 들어온 이슈는 몇 개야?",
        ):
            with self.subTest(question=q), patch.object(semantic, "post_model_chat", side_effect=requests.Timeout) as post:
                constraints = extract_query_constraints(q)
                result = semantic.resolve_status_intent(q, constraints)
                self.assertFalse(result["needs_clarification"])
                self.assertEqual(result["reason"], "structured_status_neutral")
                post.assert_not_called()
                self.assertTrue(chat.can_skip_general_planner(q, constraints, {"source": "mantis"}))

    def test_unknown_modifiers_cannot_disappear_into_neutral_fast_path(self):
        for q in (
            "NH 최근 아직 손봐야 할 이슈 3개 보여줘",
            "NH 최근 이슈 3개 보여줘 단 마무리 못 한 것만",
            "NH MTS 남은 공수 알려줘", "NH 이슈 없어지지 않은 것만 보여줘",
            "NH 최근 이슈 3개 보여줘 상태는 신경 쓰이는 것만",
            "NH 최근 이슈 3개 보여줘 가 나 다 라 수습해야 할 것",
        ):
            with self.subTest(question=q):
                self.assertFalse(semantic.is_structured_status_neutral_query(q, extract_query_constraints(q)))

    def test_bad_evidence_raw_status_and_nonwork_objects_are_rejected(self):
        for q, intent, evidence in (("NH 남은 기간", "open", "남은"), ("NH 남은 공수", "open", "남은 공수"),
                                    ("NH 살펴볼 이슈", "open", "없는 문구"), ("NH 마무리 못 한 것", "resolved", "마무리 못 한 것")):
            result, _ = self.resolve(q, {"status_intent": intent, "evidence": evidence, "needs_clarification": False})
            self.assertTrue(result["needs_clarification"], result)

    def test_explicit_rule_wins_without_model_and_survives_outage(self):
        for q, expected in (("NH 개발완료 이슈", ["dev_done"]), ("NH 남은거 몇개야", ["open"]),
                             ("NH 종료 이슈", ["closed"])):
            result, post = self.resolve(q, {"status_intent": "open", "evidence": "NH", "needs_clarification": False})
            post.assert_not_called()
            self.assertEqual(result["statuses"], expected)
            self.assertFalse(result["needs_clarification"])

    def test_grounded_model_conditions_are_verified_without_regex(self):
        q = CASES[0]["question"]
        self.assertEqual(extract_query_constraints(q)["statuses"], [])
        result, _ = self.resolve(q, {"status_intent": "open", "evidence": CASES[0]["evidence"], "needs_clarification": False})
        with semantic.status_resolution_scope(result):
            c = extract_query_constraints(q)
            self.assertEqual(extract_query_constraints("KB 이슈")["statuses"], [])
        self.assertEqual(extract_query_constraints(q)["statuses"], [])
        plan = build_query_plan(q, {"source": "mantis"}, c, {"terms": ["손봐야", "차트"]})
        self.assertEqual(plan["terms"], ["차트"])
        self.assertTrue(validate_query_plan(q, plan)["valid"])
        self.assertEqual(ungrounded_constraint_labels(q, c, "mantis"), [])
        plan["filter_clauses"] = [x for x in plan["filter_clauses"] if x["field"] != "status"]
        self.assertFalse(validate_query_plan(q, plan)["valid"])

    def test_semantic_planning_precedes_actual_sla_report(self):
        q = "NH 마무리 못 한 이슈 오래된 순으로 2건 보여줘"
        resolution, _ = self.resolve(q, {"status_intent": "open", "evidence": "마무리 못 한 이슈", "needs_clarification": False})
        context = chat.ChatPipelineContext(q, {"id": "chosen-answer-model"}, "key", [], q,
            {"mantis": True}, [], extract_query_constraints(q), 5)
        rows = [{"id": i, "summary": "점검", "project": {"name": "120NH(NH증권)"},
                 "status": {"name": status}, "created_at": "2025-01-01"}
                for i, status in enumerate(("assigned", "resolved", "closed", "new"), 1)]
        with patch.object(chat, "resolve_status_intent", return_value=resolution), \
             patch.object(sla, "read_complete_store", return_value=IssueBatch(rows, {"search_complete": True})):
            response = chat.plan_chat_query(ChatRequest(modelId="chosen-answer-model", messages=[ChatMessage(role="user", content=q)]), context, None)
        self.assertEqual({s["issue_id"] for s in response["sources"]}, {1, 4})
        response = chat.attach_status_resolution(response, context)
        self.assertIn("근거: ‘마무리 못 한 이슈’", response["content"])
        self.assertEqual(response["retrieval"]["query_plan"]["status_resolution"]["origin"], "llm")
        verification = build_verification_report(q, response["content"], response["sources"], response["retrieval"], "deterministic_mantis_sla")
        self.assertEqual(verification["status"], "pass", verification)

    def test_ambiguous_or_failed_plan_never_runs_reports(self):
        q = "NH MTS 챙겨야 하는 것은?"
        for reason in ("ambiguous", "planner_unavailable", "invalid_evidence"):
            context = chat.ChatPipelineContext(q, {}, "", [], q, {"mantis": True}, [], extract_query_constraints(q), 5)
            with patch.object(chat, "resolve_status_intent", return_value={"needs_clarification": True, "reason": reason}), \
                 patch.object(chat, "build_deterministic_report") as report:
                response = chat.plan_chat_query(ChatRequest(modelId="test", messages=[ChatMessage(role="user", content=q)]), context, None)
            report.assert_not_called()
            self.assertEqual(response["raw"]["mode"], "status_clarification")
            self.assertIn("집계하지 않았습니다", response["content"])
            response = chat.attach_status_resolution(response, context)
            verification = build_verification_report(q, response["content"], [], response["retrieval"], "status_clarification")
            self.assertEqual(verification["status"], "warning")
            self.assertEqual(verification["completedDimensions"], 0)

    def test_explicit_all_statuses_does_not_keep_closed_filter(self):
        for question in ("NH 종료된 이슈도 포함해서 보여줘", "NH 상태 무관하게 보여줘", "그중 전체 상태 보여줘"):
            c = extract_query_constraints(question)
            self.assertEqual(c["statuses"], [], question)
            resolution, post = self.resolve(question, {})
            post.assert_not_called()
            self.assertEqual(resolution["reason"], "explicit_scope")

    def test_notion_is_not_sent_to_mantis_status_planner(self):
        q = "노션에서 문서 찾아줘"
        context = chat.ChatPipelineContext(q, {}, "", [], q, {"notion": True, "mantis": True}, [], extract_query_constraints(q), 5)
        with patch.object(chat, "resolve_status_intent") as planner, patch.object(chat, "_plan_chat_query", return_value=None):
            chat.plan_chat_query(ChatRequest(modelId="test", messages=[ChatMessage(role="user", content=q)]), context, None)
        planner.assert_not_called()

    def test_saved_status_is_inherited_but_explicit_change_and_independent_query_are_not(self):
        prior = CASES[0]["question"]
        resolution, _ = self.resolve(prior, {"status_intent": "open", "evidence": CASES[0]["evidence"], "needs_clarification": False})
        for latest, expected in (("그중 KB만 보여줘", True), ("그중 개발완료만", False),
                                 ("그중 전체 상태 보여줘", False), ("신한 이슈 보여줘", False)):
            with self.subTest(latest=latest), \
                 patch.object(chat, "get_private_model", return_value={"id": "answer", "status": "active"}), \
                 patch.object(chat, "get_model_api_key", return_value="synthetic"), \
                 patch.object(chat, "load_audit_query_plan", return_value={"source": "mantis", "status_resolution": resolution}) as load, \
                 patch.object(chat, "rewrite_follow_up_query_with_model", return_value=None):
                request = ChatRequest(modelId="answer", contextAuditId="a" * 32,
                    messages=[ChatMessage(role="user", content=prior), ChatMessage(role="user", content=latest)])
                context = chat.prepare_chat_pipeline(request, latest, {"uid": "u"})
            self.assertEqual(bool(context.status_resolution), expected)
            if expected:
                self.assertEqual(load.call_args.args, ("a" * 32, "u", prior))
                self.assertTrue(semantic.resolution_is_grounded(context.status_resolution))

    def test_short_clarification_reply_restores_only_authenticated_previous_scope(self):
        prior = "NH MTS 챙겨야 하는 것은?"
        for latest, trusted, inherits in (("미종료로 보여줘", True, True),
                                          ("전체 상태로 보여줘", True, True),
                                          ("미종료로 보여줘", False, False),
                                          ("KB 미종료 보여줘", True, False)):
            with self.subTest(latest=latest, trusted=trusted), \
                 patch.object(chat, "get_private_model", return_value={"id": "answer", "status": "active"}), \
                 patch.object(chat, "get_model_api_key", return_value="synthetic"), \
                 patch.object(chat, "load_audit_query_plan", return_value={
                     "status_resolution": {"needs_clarification": True}} if trusted else {}), \
                 patch.object(chat, "rewrite_follow_up_query_with_model", return_value=None):
                request = ChatRequest(modelId="answer", contextAuditId="a" * 32,
                    messages=[ChatMessage(role="user", content=prior), ChatMessage(role="user", content=latest)])
                context = chat.prepare_chat_pipeline(request, latest, {"uid": "u"})
            self.assertEqual(context.query_constraints.get("platform_key") == "mts", inherits)
            if inherits:
                self.assertEqual(context.query_constraints.get("customer_key"), "NH")
                self.assertEqual(context.query_constraints.get("statuses"), ["open"] if "미종료" in latest else [])


class AuditStatusOwnershipTests(unittest.TestCase):
    def test_plan_load_requires_owner_and_matching_previous_question(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "audit.sqlite3"
            with sqlite3.connect(db) as c:
                c.execute("CREATE TABLE validation_logs (id TEXT, user_uid TEXT, question TEXT, payload TEXT)")
                c.execute("INSERT INTO validation_logs VALUES (?, ?, ?, ?)",
                          ("a" * 32, "owner", "직전 질문", json.dumps({"queryPlan": {"source": "mantis"}})))
            with patch.object(audit, "AUDIT_DB_PATH", db), patch.object(audit, "init_audit_db"):
                self.assertEqual(audit.load_audit_query_plan("a" * 32, "owner", "직전 질문"), {"source": "mantis"})
                self.assertEqual(audit.load_audit_query_plan("a" * 32, "other", "직전 질문"), {})
                self.assertEqual(audit.load_audit_query_plan("a" * 32, "owner", "다른 질문"), {})
