"""실제 채팅 경계에서 재현한 전송·근거·폴백 회귀. 입력과 응답은 모두 가상이다."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

import requests

from agent_harness_cases import (
    ADX, BACKUP, COMPANY, FakeModel, FakeSources, agent_environment, backup_record,
    chat_request, explain_context, final, notion_page, tool_call, turn, verification_record,
)
from backend import llm
from backend.evaluation.finalization import finalize_chat_response
from backend.services.agent import orchestrator, policy
from backend.services.chat_service import attach_status_resolution
from backend.tools.conditions import project_conditions


QUESTION = "ADX 원인과 해결 방법"
SEARCH = "search_mantis_issues"
BACKUP_ENV = {"AGENT_ALLOWED_MODEL_IDS": "company-test,backup-test",
              "AGENT_ALLOWED_ENDPOINT_ORIGINS": "https://company.test,https://backup.test"}


def investigate(question=QUESTION, source="mantis"):
    request = chat_request(question)
    context = explain_context(question, source=source)
    with policy.request_transfer_scope(request.modelId) as active:
        assert active is not None
        result = orchestrator.run_agent_investigation(request, context, active)
        return finalize_chat_response(request, question, attach_status_resolution(result, context))


class AgentReviewRegressions(unittest.TestCase):
    def test_expired_record_preserves_planner_and_transport_policy(self):
        with agent_environment(company={**COMPANY, "fallbackModelIds": ["external-test"]},
                               record=verification_record(verifiedAt="2000-01-01")) as env:
            with policy.request_transfer_scope("company-test") as active:
                self.assertIsNone(active)
                primary = llm.get_private_model("company-test")
                self.assertTrue(llm.model_transfer_allowed(primary))
                self.assertEqual(llm.get_fallback_models(primary, llm.get_model_registry()), [])
                with self.assertRaises(llm.ModelTransferBlocked):
                    llm.post_model_chat(llm.get_private_model("external-test"), "test", {}, timeout=1)
            self.assertEqual(env["model"].calls, [])

    def test_unlisted_fallback_is_not_in_either_candidate_pool(self):
        with agent_environment(extra_models=(BACKUP,), extra_env=BACKUP_ENV,
                               extra_records={"backup-test": backup_record()}):
            with policy.request_transfer_scope("company-test") as active:
                primary = llm.get_private_model("company-test")
                pools = orchestrator.agent_model_candidates(primary, active)
                self.assertEqual([[m["id"] for m in pool] for pool in pools], [["company-test"], ["company-test"]])

    def test_text_only_failover_is_attributed_to_backup_in_response_and_audit(self):
        model = FakeModel([turn(tool_call(SEARCH, {"query": "ADX"})), final("x")],
                          failing_after={"company-test": (2, 503)})
        with agent_environment(model=model, sources=FakeSources(mantis={"ADX": [ADX]}),
                               company={**COMPANY, "fallbackModelIds": ["backup-test"]}, extra_models=(BACKUP,),
                               extra_records={"backup-test": backup_record()}, extra_env=BACKUP_ENV):
            result = investigate()
        self.assertTrue(result["groundingReview"]["completed"])
        self.assertEqual(result["modelId"], "backup-test")
        self.assertEqual(result["usedFallbackModel"], "backup-test")
        from backend.config import AUDIT_DB_PATH
        import sqlite3
        from contextlib import closing
        with closing(sqlite3.connect(AUDIT_DB_PATH)) as connection:
            row = connection.execute("SELECT model_id, payload FROM validation_logs WHERE id=?", (result["auditId"],)).fetchone()
        self.assertEqual((row[0], json.loads(row[1])["modelId"]), ("backup-test", "backup-test"))

    def test_rejected_source_stays_out_of_response_events_and_followup_audit(self):
        rejected = {"content": "접근 제한", "sources": [], "sourcesForFollowUps": [], "retrieval": None,
                    "raw": {"mode": "explanation_evidence_incomplete"},
                    "groundingReview": {"completed": False, "errors": ["source_refresh_invalid"]}}
        model = FakeModel([turn(tool_call(SEARCH, {"query": "ADX"})), final("x")])
        with agent_environment(model=model, sources=FakeSources(mantis={"ADX": [ADX]})), patch(
            "backend.services.grounded_explanation.generate_grounded_explanation", return_value=rejected
        ), patch("backend.evaluation.finalization.emit_chat_event") as events:
            result = investigate()
        self.assertFalse(result["sources"])
        self.assertIsNone(result["retrieval"])
        self.assertNotIn(ADX["summary"], json.dumps(result, ensure_ascii=False))
        self.assertFalse(any(c.args[0] == "evidence" and c.kwargs["sources"] for c in events.call_args_list))
        from backend.audit import load_audit_sources
        self.assertFalse(load_audit_sources(result["auditId"]))

    def test_redirect_never_replays_protected_post(self):
        urls = []
        def redirect(adapter, request, **kwargs):
            urls.append(request.url)
            response = requests.Response()
            response.status_code = 307 if len(urls) == 1 else 200
            response.url, response.request = request.url, request
            response._content, response._content_consumed = b"{}", True
            response.headers["Location"] = "https://unlisted.invalid/chat"
            return response
        with llm.model_transfer_scope(lambda m: m["id"] == COMPANY["id"]), patch(
            "requests.adapters.HTTPAdapter.send", new=redirect
        ), self.assertRaises(llm.ModelTransferBlocked):
            llm.post_model_chat(COMPANY, "test", {"messages": [{"role": "user", "content": "marker"}]}, timeout=2)
        self.assertEqual(urls, [COMPANY["endpoint"]])

    def test_quote_selection_and_review_mask_private_text(self):
        issue = deepcopy(ADX)
        email = "fixture@example.invalid"
        issue["notes"][0]["text"] += " 담당 연락 " + email
        model = FakeModel([turn(tool_call(SEARCH, {"query": "ADX"})), final("x")])
        with agent_environment(model=model, sources=FakeSources(mantis={"ADX": [issue]})):
            result = investigate()
        self.assertTrue(result["groundingReview"]["completed"])
        grounded = [c["payload"]["messages"] for c in model.calls if not c["tools"]]
        self.assertGreaterEqual(len(grounded), 2)
        self.assertNotIn(email, json.dumps(grounded))
        self.assertIn("[이메일 가림]", json.dumps(grounded, ensure_ascii=False))

    def test_redaction_preserves_embedded_evidence_json_and_passage_ids(self):
        from backend.tools.redaction import redact_prompt_text
        content = '검토 자료\n{"text":"토큰=example-secret","passage_id":"p17"}'
        masked = redact_prompt_text(content)
        self.assertEqual(json.loads(masked.splitlines()[1]), {"text": "토큰=[가림]", "passage_id": "p17"})
        self.assertIn("example-secret", content)

    def test_uncited_completion_and_model_link_are_excluded_at_delivery(self):
        q = "배포 가이드의 롤백 절차를 설명해줘"
        page = notion_page("p1", "배포 가이드", "롤백 절차는 검토 중입니다.")
        draft = ("- 롤백 절차는 검토 중입니다. [N1] 운영 반영까지 완료했습니다.\n"
                 "상세 절차는 [여기](https://fake.invalid)에서 확인하세요. [N1]")
        model = FakeModel([turn(tool_call("search_notion_pages", {"query": "롤백"})), final(draft)], finals=[final(draft)])
        with agent_environment(model=model, sources=FakeSources(notion={"롤백": [page]})):
            result = investigate(q, "notion")
        self.assertEqual(result["verification"]["delivery"], "allowed")
        self.assertIn("검토 중입니다", result["content"])
        self.assertNotIn("완료했습니다", result["content"])
        self.assertNotIn("fake.invalid", result["content"])

    def test_inline_citation_and_verification_notes_survive_delivery(self):
        question = "보고서 생성 조건과 운영 배포 여부를 설명해줘"
        page = notion_page("guide", "보고서 가이드", "입력값이 없으면 보고서가 생성되지 않습니다.")
        draft = ("### 확인된 사실\n- 가이드 [N1]에 따르면 입력값이 없으면 보고서가 생성되지 않습니다.\n"
                 "### 근거 기반 분석·추정\n- 운영 문제는 해결됐습니다.\n"
                 "### 추가 확인 필요\n- 배포 여부는 해당 문서에서 확인되지 않습니다.\n"
                 "- 운영 로그와 대상 버전 대조 필요\n- 실제 배포까지 완료했습니다.")
        model = FakeModel([turn(tool_call("search_notion_pages", {"query": "보고서"})), final(draft)])
        with agent_environment(model=model, sources=FakeSources(notion={"보고서": [page]})):
            result = investigate(question, "notion")
        self.assertEqual(result["verification"]["delivery"], "allowed")
        self.assertIn("가이드 (Notion: 보고서 가이드)에 따르면 입력값이 없으면 보고서가 생성되지 않습니다.", result["content"])
        self.assertIn("### 추가 확인 필요", result["content"])
        self.assertIn("배포 여부는 해당 문서에서 확인되지 않습니다.", result["content"])
        self.assertIn("운영 로그와 대상 버전 대조 필요", result["content"])
        self.assertNotIn("### 근거 기반 분석·추정", result["content"])
        self.assertNotIn("해결됐습니다", result["content"])
        self.assertNotIn("완료했습니다", result["content"])

    def test_evidence_gap_alone_is_reported_when_search_found_a_document(self):
        question = "보고서 배포 일자를 확인해줘"
        page = notion_page("guide", "보고서 가이드", "보고서는 영업일마다 생성합니다.")
        draft = ("### 확인된 사실\n- 배포 일자는 제공된 자료에서 확인할 수 없습니다.\n"
                 "### 근거 기반 분석·추정\n\n### 추가 확인 필요")
        model = FakeModel([turn(tool_call("search_notion_pages", {"query": "보고서"})), final(draft)])
        with agent_environment(model=model, sources=FakeSources(notion={"보고서": [page]})):
            result = investigate(question, "notion")
        self.assertIn("배포 일자는 제공된 자료에서 확인할 수 없습니다.", result["content"])
        self.assertNotIn("### 추가 확인 필요", result["content"])
        self.assertNotIn("### 근거 기반 분석·추정", result["content"])

    def test_changed_serving_after_search_stops_all_model_calls(self):
        class Changed(FakeModel):
            def __call__(self, *args):
                if self.calls:
                    self.fingerprint = "new-deployment"
                return super().__call__(*args)
        model = Changed([turn(tool_call(SEARCH, {"query": "ADX"})), final("x")])
        with agent_environment(model=model, sources=FakeSources(mantis={"ADX": [ADX]})):
            result = investigate()
            self.assertTrue(policy.load_verification("company-test")["reverificationRequired"])
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(result["agentReview"]["finalization"], "evidence_only")

    def test_fingerprint_is_checked_on_grounded_calls_too(self):
        class ChangedOnText(FakeModel):
            def grounded(self, payload):
                self.fingerprint = "new-text-deployment"
                return super().grounded(payload)
        model = ChangedOnText([turn(tool_call(SEARCH, {"query": "ADX"})), final("x")])
        with agent_environment(model=model, sources=FakeSources(mantis={"ADX": [ADX]})):
            result = investigate()
            self.assertTrue(policy.load_verification("company-test")["reverificationRequired"])
        self.assertEqual([c["tools"] for c in model.calls], [True, True, False])
        self.assertEqual(result["agentReview"]["stopReason"], "serving_changed")

    def test_mantis_owner_does_not_promote_notion_reference_in_both_plan(self):
        constraints = {"filter_clauses": [{"field": "owner", "operator": "equals", "value": "담당자"}]}
        for source in ("mantis", "both"):
            with self.subTest(source=source):
                projected = project_conditions({"source": source}, constraints, "notion")
                self.assertEqual(projected.grade, "reference")
                self.assertIn("담당자", projected.unapplied)
                self.assertNotIn("owner", [c["field"] for c in projected.constraints["filter_clauses"]])

    def test_cross_source_page_with_wrong_customer_stays_out_of_ledger(self):
        pages = [{**notion_page(code, code, "지표 가이드"), "customer": customer}
                 for code, customer in (("good", "신한투자증권"), ("wrong", "대신증권"))]
        model = FakeModel([turn(tool_call("search_notion_pages", {"query": "ADX"})), final("x")])
        constraints = {"filter_clauses": [{"field": "customer", "operator": "equals", "value": "신한"},
                                          {"field": "owner", "operator": "equals", "value": "담당자"}]}
        with agent_environment(model=model, sources=FakeSources(notion={"ADX": pages})), policy.request_transfer_scope(
            "company-test"
        ) as active:
            context = explain_context(QUESTION, source="both", constraints=constraints)
            result = orchestrator.run_agent_investigation(chat_request(QUESTION), context, active)
        self.assertEqual(result["agentReview"]["evidence"], {"scoped": [], "reference": ["N1"]})
        self.assertEqual([(s["title"], s["evidence_grade"]) for s in result["sources"]], [("good", "reference")])
