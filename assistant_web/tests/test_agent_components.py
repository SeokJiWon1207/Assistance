"""에이전트 구성요소: 전송 정책·검증 기록·응답 파서·조건 대응·인용·마스킹·마감 통합·기능 플래그 OFF."""

import json
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import requests

from agent_harness_cases import (
    ADX, COMPANY, EXTERNAL, FakeModel, FakeSources, agent_environment, chat_request, explain_context, final,
    notion_page, registry, tool_call, turn, verification_record,
)
from backend import llm
from backend.services.agent import policy


class TransferPolicyTests(unittest.TestCase):
    def test_flag_off_creates_no_policy_and_keeps_existing_fallbacks(self):
        with agent_environment(enabled=False):
            with policy.request_transfer_scope("company-test") as active:
                self.assertIsNone(active)
                self.assertIsNone(policy.active_agent_policy())
                company = llm.get_private_model("company-test")
                fallbacks = [model["id"] for model in llm.get_fallback_models(company, llm.get_model_registry())]
                self.assertEqual(fallbacks, ["external-test", "local-test"])
                self.assertTrue(llm.model_transfer_allowed(llm.get_private_model("external-test")))

    def test_candidate_scope_limits_fallbacks_and_blocks_other_models_before_sending(self):
        model = FakeModel([])
        with agent_environment(model=model):
            with policy.request_transfer_scope("company-test") as active:
                self.assertIsNotNone(active)
                company = llm.get_private_model("company-test")
                self.assertEqual(llm.get_fallback_models(company, llm.get_model_registry()), [])
                self.assertEqual(llm.get_fallback_models(company, llm.get_model_registry(), default_local_only=True), [])
                with self.assertRaises(llm.ModelTransferBlocked):
                    llm.post_model_chat(llm.get_private_model("external-test"), "key", {"messages": []}, timeout=5)
            self.assertEqual(model.calls, [])
            self.assertTrue(llm.model_transfer_allowed(llm.get_private_model("external-test")))

    def test_candidate_requires_allowlists_declaration_and_matching_verification(self):
        today = date.today()
        cases = {
            "verified": ({}, {}, True),
            "other_origin": ({"AGENT_ALLOWED_ENDPOINT_ORIGINS": "https://other.test"}, {}, False),
            "id_not_allowed": ({"AGENT_ALLOWED_MODEL_IDS": "external-test"}, {}, False),
            "failed_check": ({}, {"checks": {"tools_request_accepted": True}}, False),
            "unsupported": ({}, {"result": "unsupported"}, False),
            "config_changed": ({}, {"configFingerprint": "changed"}, False),
            "expired": ({}, {"verifiedAt": (today - timedelta(days=31)).isoformat()}, False),
            "reverification": ({}, {"reverificationRequired": True}, False),
        }
        for name, (env, overrides, expected) in cases.items():
            with self.subTest(case=name), agent_environment(extra_env=env, record=verification_record(**overrides)):
                self.assertEqual(policy.agent_candidate("company-test") is not None, expected)
        with self.subTest(case="missing_record"), agent_environment(save_record=False):
            self.assertEqual(policy.tool_support(llm.get_private_model("company-test"), policy.AgentSettings())[1],
                             "not_verified")
        with self.subTest(case="not_declared"), agent_environment(company={**COMPANY, "supportsTools": False}):
            self.assertIsNone(policy.agent_candidate("company-test"))
        with self.subTest(case="external_selected"), agent_environment():
            self.assertIsNone(policy.agent_candidate("external-test"))

    def test_unified_planner_under_candidate_policy_never_falls_back_to_other_models(self):
        from backend.intent import unified
        model = FakeModel([])
        with agent_environment(model=model), patch.object(model, "grounded", return_value=_status(503)):
            with policy.request_transfer_scope("company-test"):
                plan = unified.plan_question("ADX 이슈 원인 알려줘", llm.get_private_model("company-test"),
                                             "company-key", {"uid": "u"}, {})
        self.assertEqual(plan["operation"], "clarify")
        self.assertEqual({call["model"] for call in model.calls}, {"company-test"})

    def test_generation_under_candidate_policy_returns_evidence_instead_of_external_answer(self):
        from backend.services.chat.generate import generate_model_response
        model = FakeModel([])
        with agent_environment(model=model), patch.object(model, "grounded", side_effect=requests.ConnectionError("down")):
            with policy.request_transfer_scope("company-test"):
                result = generate_model_response(chat_request("질문"), llm.get_private_model("company-test"),
                                                 [{"role": "user", "content": "질문"}], {"stages": []}, {}, 5)
        self.assertEqual(result["raw"]["mode"], "model_timeout_fallback")
        self.assertEqual({call["model"] for call in model.calls}, {"company-test"})


def _status(code):
    from agent_harness_cases import FakeResponse
    return FakeResponse(code, {})


class ProtocolTests(unittest.TestCase):
    def parse(self, message, finish="stop"):
        from backend.services.agent.protocol import parse_assistant_turn
        return parse_assistant_turn({"choices": [{"message": message, "finish_reason": finish}], "usage": {"prompt_tokens": 3}})

    def test_tool_calls_without_content_are_a_normal_intermediate_turn(self):
        turn_ = self.parse({"content": None, "tool_calls": [tool_call("search_mantis_issues", {"query": "ADX"}, "x1")]},
                           "tool_calls")
        self.assertEqual(turn_.kind, "tool_calls")
        self.assertEqual(turn_.history_message()["tool_calls"][0]["id"], "x1")
        self.assertEqual(turn_.history_message()["content"], "")
        self.assertEqual(turn_.usage, {"prompt_tokens": 3})

    def test_provider_fields_on_tool_calls_are_returned_unchanged(self):
        call = {**tool_call("search_mantis_issues", {"query": "ADX"}, "g1"),
                "extra_content": {"google": {"thought_signature": "sig"}}, "index": 0}
        history = self.parse({"content": None, "tool_calls": [call]}, "tool_calls").history_message()
        self.assertEqual(history["tool_calls"][0]["extra_content"], {"google": {"thought_signature": "sig"}})
        self.assertNotIn("index", history["tool_calls"][0])

    def test_content_with_tool_calls_stays_intermediate(self):
        turn_ = self.parse({"content": "먼저 찾아보겠습니다.", "tool_calls": [tool_call("search_notion_pages", {"query": "a"})]},
                           "tool_calls")
        self.assertEqual(turn_.kind, "tool_calls")
        self.assertEqual(turn_.content, "먼저 찾아보겠습니다.")

    def test_empty_truncated_unparsed_and_invalid_responses_are_distinct(self):
        from backend.services.agent.protocol import ToolCall, parse_arguments, parse_assistant_turn
        self.assertEqual(self.parse({"content": ""}).kind, "empty")
        self.assertEqual(self.parse({"content": "부분 답", "tool_calls": []}, "length").kind, "truncated_final")
        self.assertEqual(self.parse({"content": None, "tool_calls": [tool_call("a", '{"q":')]}, "length").kind,
                         "truncated_tool_calls")
        self.assertEqual(self.parse({"content": '<tool_call>{"name": "a"}</tool_call>'}).kind, "unparsed_tool_call")
        self.assertEqual(self.parse({"content": None, "tool_calls": [{"function": {"name": "a"}}]}).kind, "invalid")
        self.assertEqual(parse_assistant_turn({"choices": []}).kind, "invalid")
        self.assertEqual(parse_arguments(ToolCall("i", "a", "{bad"))[1], "argument_parse_error")
        self.assertEqual(parse_arguments(ToolCall("i", "a", "[1]"))[1], "invalid_arguments")
        self.assertEqual(parse_arguments(ToolCall("i", "a", ""))[0], {})


class ConditionAndCitationTests(unittest.TestCase):
    def test_unplanned_source_keeps_mappable_conditions_and_names_the_rest(self):
        from backend.tools.conditions import project_conditions
        constraints = {"customer_key": "신한", "customer_aliases": ["신한투자증권"], "statuses": ["open"],
                       "people": {"names": ["석지원"], "role": "handler"},
                       "created_at": {"from": "2026-05-01", "to": "2026-05-31"},
                       "filter_clauses": [{"field": "customer", "operator": "equals", "value": "신한"},
                                          {"field": "keyword", "operator": "contains", "value": "ADX"},
                                          {"field": "status", "operator": "in", "value": ["open"]},
                                          {"field": "created_at", "operator": "between", "value": ["2026-05-01", "2026-05-31"]}]}
        notion = project_conditions({"source": "mantis"}, constraints, "notion")
        self.assertEqual([clause["field"] for clause in notion.constraints["filter_clauses"]], ["customer", "keyword"])
        self.assertEqual(notion.constraints["customer_aliases"], ["신한투자증권"])
        self.assertEqual(notion.unapplied, ("상태", "등록 기간", "담당자"))
        self.assertEqual(notion.grade, "reference")
        mantis = project_conditions({"source": "mantis"}, constraints, "mantis")
        self.assertTrue(mantis.planned)
        self.assertEqual(mantis.constraints, constraints)
        both = project_conditions({"source": "both"}, constraints, "notion")
        self.assertEqual(both.grade, "reference")
        self.assertEqual(both.constraints, notion.constraints)
        clean = project_conditions({"source": "notion"}, {"filter_clauses": []}, "mantis")
        self.assertEqual(clean.grade, "scoped")

    def test_citations_remove_whole_claims_not_just_markers(self):
        from backend.services.agent import citations
        catalog = {"M#31350": {"type": "mantis", "id": 31350, "title": "ADX", "grade": "scoped"},
                   "N1": {"type": "notion", "id": "p", "title": "가이드", "grade": "reference"}}
        text = "원인은 기간 오류입니다. [M#31350] 가이드도 같습니다. [N1]\n- 담당자가 승인했습니다. [N9]\n이슈 #40000에서 끝났습니다."
        report = citations.validate_citations(text, catalog, quoted_ids=set())
        self.assertEqual(sorted(issue.reason for issue in report.issues), ["reference_grade", "unknown_issue", "unknown_ref"])
        cleaned, removed = citations.remove_claims(text, report)
        self.assertEqual(removed, 3)
        self.assertEqual(cleaned, "원인은 기간 오류입니다. [M#31350]")
        self.assertEqual(citations.render_citations(cleaned, catalog), "원인은 기간 오류입니다. (#31350)")
        self.assertTrue(citations.validate_citations("원문 노트의 #40000 참조 [M#31350]", catalog, {40000}).ok)

    def test_citation_scope_and_unverified_section_do_not_hide_assertions_or_invalid_sources(self):
        from backend.services.agent import citations
        catalog = {"N1": {"grade": "scoped"}, "N2": {"grade": "reference"}}
        content = ("- 사양은 검토 중입니다 [N1] 운영 반영까지 완료했습니다.\n"
                   "### 추가 확인 필요\n"
                   "- 운영 반영은 완료됐지만 배포 시간은 확인되지 않습니다.\n"
                   "- 운영 반영은 완료되지 않았습니다.\n"
                   "- [N7]의 반영 여부 확인 필요\n- [N2]의 반영 여부 확인 필요\n"
                   "- [배포 기록](https://fake.invalid) 확인 필요")
        report = citations.validate_citations(content, catalog, set())
        self.assertEqual({issue.reason for issue in report.issues},
                         {"missing_citation", "unknown_ref", "reference_grade", "model_link"})
        cleaned, _ = citations.remove_claims(content, report)
        self.assertIn("사양은 검토 중입니다 [N1]", cleaned)
        self.assertNotIn("운영 반영", cleaned)
        self.assertNotIn("fake.invalid", cleaned)
        self.assertNotIn("[N7]", cleaned)
        self.assertNotIn("[N2]", cleaned)

    def test_caveats_keep_their_own_section_and_do_not_lend_a_source_to_another_claim(self):
        from backend.services.agent import citations
        catalog = {"N1": {"grade": "scoped"}}
        draft = ("### 확인된 사실\n"
                 "운영 로그 대조 필요\n"
                 "별도 검토 중 [N1], 운영 반영도 완료했습니다.\n"
                 "운영 전환을 확정했습니다. [N1]에 따르면 절차를 검토하고 있습니다.\n"
                 "정확한 시점은 자료만으로 판단하기 어렵습니다.\n"
                 "### 추가 확인 필요\n"
                 "운영 로그 대조 필요\n[N1234]에서 확인 필요")
        report = citations.validate_citations(draft, catalog, set())
        cleaned, _ = citations.remove_claims(draft, report)
        self.assertIn("별도 검토 중 [N1]", cleaned)
        self.assertNotIn("운영 반영도 완료했습니다", cleaned)
        self.assertNotIn("운영 전환을 확정했습니다", cleaned)
        self.assertIn("[N1]에 따르면 절차를 검토하고 있습니다.", cleaned)
        self.assertIn("자료만으로 판단하기 어렵습니다.", cleaned)
        self.assertEqual(cleaned.count("운영 로그 대조 필요"), 1)
        self.assertNotIn("[N1234]", cleaned)

    def test_redaction_masks_credentials_and_personal_patterns(self):
        from backend.tools.redaction import redact
        masked = redact({"text": "password=abc123 연락처 010-1234-5678 a.b@example.com Bearer abcdefghijklmnop12",
                         "list": ["900101-1234567"]})
        self.assertNotIn("abc123", masked["text"])
        self.assertNotIn("010-1234-5678", masked["text"])
        self.assertNotIn("a.b@example.com", masked["text"])
        self.assertNotIn("abcdefghijklmnop12", masked["text"])
        self.assertEqual(masked["list"], ["[주민번호 가림]"])


class ExecutionAndReuseTests(unittest.TestCase):
    def test_child_deadline_times_out_without_stopping_the_request(self):
        from backend.execution import ExecutionBudget, StepTimeout, checkpoint, execution_scope, run_with_deadline
        parent = ExecutionBudget(deadline=time.monotonic() + 60)
        with execution_scope(parent):
            with self.assertRaises(StepTimeout):
                run_with_deadline(0.01, lambda: (time.sleep(0.05), checkpoint()))
            parent.remaining()
            self.assertEqual(parent.child(999).deadline, parent.deadline)

    def test_issue_reuse_scope_fetches_each_issue_once(self):
        from backend.sources.mantis_refresh import fetch_with_refresh_budget, issue_reuse_scope
        calls = []

        def fetch(timeout, attempts):
            calls.append(timeout)
            return {"id": 1}
        with issue_reuse_scope():
            self.assertEqual(fetch_with_refresh_budget(1, fetch), {"id": 1})
            self.assertEqual(fetch_with_refresh_budget(1, fetch), {"id": 1})
        fetch_with_refresh_budget(1, fetch)
        self.assertEqual(len(calls), 2)

    def test_notion_search_can_ignore_conditions_written_in_the_query(self):
        from backend.notion import search
        captured = {}

        def snapshot(database_ids, **kwargs):
            captured.update(kwargs)
            return {"source": "notion", "status": "ok", "notion_pages": [], "meta": {}}
        with patch.object(search, "resolve_notion_settings", return_value={"token": "t", "database_ids": ["db"]}), \
                patch.object(search, "resolve_notion_read_scope", return_value=({"db": ""}, True)), \
                patch("backend.notion.complete_snapshot.search_complete_snapshot", side_effect=snapshot):
            search.execute_notion_search("2025년 완료된 회의록 ADX", 3, {"filter_clauses": []}, planned_terms=["ADX"],
                                         include_blocks=False, derive_text_constraints=False)
        constraints = captured["constraints"]
        self.assertFalse(constraints.get("status"))
        self.assertEqual(constraints["common_filter_clauses"], [])
        self.assertEqual(constraints["body_filter_clauses"], [])
        self.assertEqual(captured["terms"], ["ADX"])


class FinalizationIntegrationTests(unittest.TestCase):
    def agent_response(self, model, sources, question, **context_args):
        from backend.services.agent.orchestrator import run_agent_investigation
        with agent_environment(model=model, sources=sources):
            with policy.request_transfer_scope("company-test") as active:
                return run_agent_investigation(chat_request(question), explain_context(question, **context_args), active)

    def test_finalized_agent_answer_has_agent_checks_audit_summary_and_no_ab_comparison(self):
        from backend.evaluation.finalization import finalize_chat_response
        question = "배포 가이드에서 롤백 절차가 왜 필요한지 설명해줘"
        page = notion_page("page-roll", "배포 가이드", "롤백 절차는 장애 시 이전 버전으로 되돌리기 위해 필요합니다.")
        model = FakeModel([turn(tool_call("search_notion_pages", {"query": "롤백"})),
                           final("롤백은 장애 시 이전 버전으로 되돌리기 위해 필요합니다. [N1]")])
        response = self.agent_response(model, FakeSources(notion={"롤백": [page]}), question, source="notion")
        request = chat_request(question, abTest=True, abModelId="external-test")
        with patch("backend.evaluation.finalization.generate_candidate_model_answer") as candidate, \
                patch("backend.evaluation.finalization.generate_basic_grounded_baseline") as baseline:
            result = finalize_chat_response(request, question, response, user_uid="u1")
        candidate.assert_not_called()
        baseline.assert_not_called()
        self.assertNotIn("abTest", result)
        self.assertEqual(result["verification"]["delivery"], "allowed")
        agent = next(item for item in result["verification"]["dimensions"] if item["id"] == "agent")
        self.assertEqual([check["id"] for check in agent["checks"]],
                         ["agent_step_conditions", "agent_transfer_policy", "agent_citations", "agent_reference_grade"])
        from backend.config import AUDIT_DB_PATH
        from backend.storage.profiles import AUDIT
        from backend.storage.sqlite import open_sqlite
        from contextlib import closing
        with closing(open_sqlite(AUDIT_DB_PATH, profile=AUDIT)) as connection:
            payload = json.loads(connection.execute("SELECT payload FROM validation_logs WHERE id = ?",
                                                    (result["auditId"],)).fetchone()[0])
        self.assertEqual(payload["agent"]["finalization"], "free_form")
        self.assertNotIn("롤백 절차는 장애 시", json.dumps(payload["agent"], ensure_ascii=False))
        self.assertEqual(payload["queryPlan"]["source"], "notion")

    def test_failed_agent_check_blocks_delivery(self):
        from backend.evaluation.finalization import finalize_chat_response
        question = "배포 가이드에서 롤백 절차가 왜 필요한지 설명해줘"
        response = {"modelId": "company-test", "content": "답", "sources": [], "raw": {"mode": "agent_model"},
                    "retrieval": {"stages": [], "constraints": {}}, "agentReview": {"finalization": "free_form"},
                    "agentChecks": [{"id": "agent_citations", "label": "근거 원장 인용", "status": "fail", "detail": "x"}]}
        result = finalize_chat_response(chat_request(question), question, response, user_uid="u1")
        self.assertEqual(result["verification"]["delivery"], "blocked")
        self.assertEqual(result["raw"]["blockedChecks"], ["agent_citations"])

    def test_reference_material_is_kept_out_of_follow_up_basis(self):
        question = "내 담당 미종료 ADX 이슈의 원인과 해결 방법 알려줘"
        page = notion_page("page-adx", "지표 가이드", "기간 파라미터 설명")
        model = FakeModel([turn(tool_call("search_mantis_issues", {"query": "ADX"})),
                           turn(tool_call("search_notion_pages", {"query": "기간 파라미터"})), final("x")])
        constraints = {"statuses": ["open"], "filter_clauses": [{"field": "status", "operator": "in", "value": ["open"]}]}
        response = self.agent_response(model, FakeSources(mantis={"ADX": [ADX]}, notion={"기간 파라미터": [page]}),
                                       question, constraints=constraints)
        self.assertTrue(all(source.get("type") == "mantis" for source in response["sourcesForFollowUps"]))
        self.assertEqual([source["evidence_grade"] for source in response["sources"]], ["scoped", "reference"])
        self.assertIn("evidence_note", response["sources"][1])


class FlagOffPipelineTests(unittest.TestCase):
    def test_flag_off_keeps_the_existing_path_and_sends_no_tools(self):
        from backend.services.chat_service import chat_service
        from backend.services.contracts import StageResult
        model = FakeModel([])
        with agent_environment(enabled=False, model=model):
            context = explain_context("ADX 원인 알려줘")
            with patch("backend.services.chat_service.plan_chat_query", return_value=StageResult.from_response(None)), \
                    patch("backend.services.chat_service.retrieve_chat_evidence", return_value=StageResult.from_response(None)), \
                    patch("backend.services.agent.orchestrator.run_agent_investigation", side_effect=AssertionError), \
                    patch("backend.services.grounded_explanation.generate_grounded_explanation",
                          return_value={"content": "기존 경로", "raw": {}}) as grounded:
                with policy.request_transfer_scope("company-test"):
                    result = chat_service._complete_response(chat_request("ADX 원인 알려줘"), context, {}, lambda r: r)
        grounded.assert_called_once()
        self.assertEqual(result["content"], "기존 경로")
        self.assertFalse(any(call["tools"] for call in model.calls))

    def test_route_enters_for_verified_reads_with_an_active_policy(self):
        from backend.services.agent.route import agent_route_reason
        with agent_environment():
            context = explain_context("ADX 원인 알려줘")
            self.assertEqual(agent_route_reason(context), "not_candidate")
            with policy.request_transfer_scope("company-test"):
                self.assertEqual(agent_route_reason(context), "")
                for operation in ("count", "search", "aggregate"):
                    context.intent_plan = {**context.intent_plan, "operation": operation}
                    self.assertEqual(agent_route_reason(context), "")
                for operation in ("report", "write", "clarify"):
                    context.intent_plan = {**context.intent_plan, "operation": operation}
                    self.assertEqual(agent_route_reason(context), "not_explain")
                context.intent_plan = {**context.intent_plan, "operation": "explain", "planner": "rule"}
                self.assertEqual(agent_route_reason(context), "not_explain")

    def test_routing_fixture_agent_cases(self):
        from backend.intent.unified_validation import validate_plan
        from backend.services.agent.route import agent_route_reason
        from backend.tools.conditions import project_conditions
        fixture = Path(__file__).parent / "fixtures" / "routing_cases.jsonl"
        cases = [case for case in map(json.loads, fixture.read_text(encoding="utf-8").splitlines()) if "agent_route" in case]
        self.assertGreaterEqual(len(cases), 5)
        with agent_environment(), policy.request_transfer_scope("company-test"):
            for case in cases:
                with self.subTest(question=case["q"]):
                    plan = validate_plan(case["q"], case["unified"], {"mantisUsername": "석지원"}, {})
                    context = explain_context(case["q"], source=plan["source"], constraints=plan["resolved_constraints"])
                    context.intent_plan = plan
                    self.assertEqual(agent_route_reason(context) == "", case["agent_route"])
                    if "agent_notion_unapplied" in case:
                        notion = project_conditions(plan, plan["resolved_constraints"], "notion")
                        self.assertEqual(list(notion.unapplied), case["agent_notion_unapplied"])


class EndToEndTests(unittest.TestCase):
    PLAN = {"source": "notion", "operation": "explain", "scope": "default", "followup": False, "filters": [],
            "status": {"mode": "unspecified", "values": [], "evidence": ""}, "clear_fields": [],
            "terms": ["배포 가이드", "롤백"], "group_by": None, "limit": None}

    def run_chat(self, *, enabled):
        from backend.services.chat_service import chat_service
        question = "노션 배포 가이드에서 롤백 절차가 왜 필요한지 설명해줘"
        page = notion_page("page-roll", "배포 가이드", "롤백 절차는 장애 시 이전 버전으로 되돌리기 위해 필요합니다.")
        model = FakeModel([turn(tool_call("search_notion_pages", {"query": "롤백 절차"})),
                           turn(tool_call("get_notion_page_content", {"page_ref": "N1"})),
                           final("롤백은 장애 시 이전 버전으로 되돌리기 위해 필요합니다. [N1]")], planner=self.PLAN)
        sources = FakeSources(notion={"롤백": [page]})
        user = {"uid": "agent-e2e", "mantisUsername": "석지원", "displayName": "석지원", "role": "member"}
        with agent_environment(enabled=enabled, model=model, sources=sources,
                               extra_env={"UNIFIED_QUERY_PLANNER_ENABLED": "true"}), \
                patch("backend.services.retrieval_pipeline.execute_notion_search", side_effect=sources.search_notion), \
                patch("backend.tools.notion_tools.notion_scope", return_value={'db': 'db'}), \
                patch("backend.tools.notion_tools.read_current_page", return_value={**page, '_verified_body': True}):
            result = chat_service.build_admitted_response(chat_request(question, abTest=True), user=user)
        return result, model, sources

    def test_flag_on_prepares_conditions_inside_agent_with_company_model_only(self):
        result, model, sources = self.run_chat(enabled=True)
        self.assertEqual({call["model"] for call in model.calls}, {"company-test"})
        self.assertEqual([t['function']['name'] for t in model.calls[0]['payload']['tools']], ['prepare_query'])
        self.assertTrue(any(call["tools"] for call in model.calls))
        self.assertEqual(result["raw"]["mode"], "agent_model")
        self.assertEqual(result["verification"]["delivery"], "allowed")
        self.assertIn("agent", [item["id"] for item in result["verification"]["dimensions"]])
        self.assertNotIn("abTest", result)
        self.assertTrue(result["auditId"])
        self.assertEqual(len(sources.notion_calls), 1)

    def test_flag_off_runs_the_existing_pipeline_without_tool_definitions(self):
        result, model, _sources = self.run_chat(enabled=False)
        self.assertFalse(any(call["tools"] for call in model.calls))
        self.assertNotIn("agentReview", result)
        self.assertNotEqual(result["raw"].get("mode"), "agent_model")


class FallbackModelTests(unittest.TestCase):
    """선택 모델 장애 시 fallbackModelIds 중 허용·검증된 모델로만 바꾸는지 검사한다(예: Luna → Gemini)."""

    ENV = {"AGENT_ALLOWED_MODEL_IDS": "company-test,backup-test",
           "AGENT_ALLOWED_ENDPOINT_ORIGINS": "https://company.test,https://backup.test"}

    def run_agent(self, model, *, env=None, records=None):
        from agent_harness_cases import BACKUP, backup_record
        from backend.services.agent.orchestrator import run_agent_investigation
        question = "ADX 지표가 표시되지 않은 원인 알려줘"
        company = {**COMPANY, "fallbackModelIds": ["backup-test", "external-test"]}
        with agent_environment(model=model, sources=FakeSources(mantis={"ADX": [ADX]}), company=company,
                               extra_models=(BACKUP,), extra_env=env or self.ENV,
                               extra_records={"backup-test": backup_record()} if records is None else records):
            with policy.request_transfer_scope("company-test") as active:
                return run_agent_investigation(chat_request(question), explain_context(question), active)

    def test_transient_primary_failure_switches_to_verified_backup_for_every_call(self):
        model = FakeModel([turn(tool_call("search_mantis_issues", {"query": "ADX"})), final("x")],
                          failing={"company-test": 503})
        result = self.run_agent(model)
        called = [call["model"] for call in model.calls]
        self.assertEqual(called[:3], ["company-test", "company-test", "backup-test"])
        self.assertEqual(set(called[2:]), {"backup-test"})
        self.assertNotIn("external-test", called)
        self.assertEqual(result["agentReview"]["modelIds"], ["backup-test"])
        self.assertEqual(result["agentReview"]["finalization"], "grounded")
        self.assertTrue(result["groundingReview"]["completed"])
        self.assertTrue(all(check["status"] == "pass" for check in result["agentChecks"]))
        self.assertEqual(result["agentReview"]["modelRequests"], len(model.calls))

    def test_non_transient_primary_error_switches_without_retrying_the_same_model(self):
        model = FakeModel([turn(tool_call("search_mantis_issues", {"query": "ADX"})), final("x")],
                          failing={"company-test": 402})
        self.run_agent(model)
        self.assertEqual([call["model"] for call in model.calls][:2], ["company-test", "backup-test"])

    def test_unverified_backup_is_not_used_for_tool_decisions(self):
        model = FakeModel([turn(tool_call("search_mantis_issues", {"query": "ADX"}))], failing={"company-test": 503})
        result = self.run_agent(model, records={})
        self.assertFalse(any(call["tools"] for call in model.calls if call["model"] == "backup-test"))
        self.assertEqual(result["agentReview"]["stopReason"], "model_error")

    def test_decision_format_error_on_backup_does_not_block_backup_for_text_calls(self):
        # 선택 모델이 첫 판단으로 원문을 확보한 뒤 장애가 나고, 대체 모델은 판단 호출만 형식 오류(400)를 낸다.
        model = FakeModel([turn(tool_call("search_mantis_issues", {"query": "ADX"}))],
                          failing_after={"company-test": (1, 503)}, failing_tools={"backup-test": 400})
        result = self.run_agent(model)
        decisions = [call["model"] for call in model.calls if call["tools"]]
        self.assertEqual(decisions, ["company-test", "company-test", "company-test", "backup-test"])
        self.assertEqual(result["agentReview"]["finalization"], "grounded")
        self.assertTrue(result["groundingReview"]["completed"])
        self.assertEqual({call["model"] for call in model.calls if not call["tools"]}, {"backup-test"})

    def test_gemini_requests_get_a_skip_signature_for_foreign_tool_calls(self):
        from backend.services.agent.orchestrator import _provider_messages
        messages = [{"role": "assistant", "content": "", "tool_calls": [
            {"id": "a", "type": "function", "function": {"name": "f", "arguments": "{}"}},
            {"id": "b", "type": "function", "function": {"name": "f", "arguments": "{}"},
             "extra_content": {"google": {"thought_signature": "own"}}}]}]
        adapted = _provider_messages({"provider": "gemini"}, messages)
        signatures = [call["extra_content"]["google"]["thought_signature"] for call in adapted[0]["tool_calls"]]
        self.assertEqual(signatures, ["skip_thought_signature_validator", "own"])
        self.assertNotIn("extra_content", messages[0]["tool_calls"][0])
        self.assertIs(_provider_messages({"provider": "openai"}, messages), messages)

    def test_backup_outside_the_allowlist_is_never_called(self):
        model = FakeModel([turn(tool_call("search_mantis_issues", {"query": "ADX"}))], failing={"company-test": 503})
        result = self.run_agent(model, env={"AGENT_ALLOWED_MODEL_IDS": "company-test",
                                            "AGENT_ALLOWED_ENDPOINT_ORIGINS": "https://company.test"})
        self.assertEqual({call["model"] for call in model.calls}, {"company-test"})
        self.assertEqual(result["raw"]["mode"], "agent_no_evidence")

    def test_unified_planner_uses_the_allowed_backup_when_the_primary_fails(self):
        from agent_harness_cases import BACKUP, backup_record
        from backend.intent import unified
        model = FakeModel([], failing={"company-test": 503})
        company = {**COMPANY, "fallbackModelIds": ["backup-test", "external-test"]}
        with agent_environment(model=model, company=company, extra_models=(BACKUP,), extra_env=self.ENV,
                               extra_records={"backup-test": backup_record()}), \
                patch.object(model, "grounded", return_value=_status(503)):
            with policy.request_transfer_scope("company-test"):
                unified.plan_question("ADX 이슈 원인 알려줘", llm.get_private_model("company-test"), "company-key",
                                      {"uid": "u"}, {})
        called = {call["model"] for call in model.calls}
        self.assertIn("backup-test", called)
        self.assertNotIn("external-test", called)


class RegistrySnapshotTests(unittest.TestCase):
    def test_models_outside_the_origin_allowlist_are_not_agent_candidates(self):
        with agent_environment(extra_env={"AGENT_ALLOWED_MODEL_IDS": "company-test,external-test,local-test"}):
            self.assertIsNone(policy.agent_candidate("external-test"))
            self.assertIsNone(policy.agent_candidate("local-test"))
        self.assertTrue(all(model["id"] != EXTERNAL["id"] or model["provider"] == "openai" for model in registry()))


if __name__ == "__main__":
    unittest.main()
