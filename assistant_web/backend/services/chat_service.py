"""채팅 요청 오케스트레이션 서비스.

HTTP 라우팅과 분리해 질문 해석, 검색, 결정형 응답, 모델 폴백을 조정한다.
단계와 공유 규칙은 chat 하위 모듈이 소유하며, 이전 import 경로는 정적으로 재노출한다.
"""

import json as json
import os as os
import re as re
from time import perf_counter as perf_counter
from typing import Any as Any, Callable as Callable, Dict as Dict, List as List, Optional as Optional
import requests as requests
from fastapi import HTTPException as HTTPException

from backend.answers import (
    CONVERSATION_TONE_NOTE as CONVERSATION_TONE_NOTE,
    answer_style_instruction as answer_style_instruction,
    build_current_date_note as build_current_date_note,
    build_unknown_customer_report as build_unknown_customer_report,
    format_deterministic_combined_answer as format_deterministic_combined_answer,
    format_deterministic_issue_answer as format_deterministic_issue_answer,
    format_model_timeout_fallback as format_model_timeout_fallback,
    sanitize_customer_notice_answer as sanitize_customer_notice_answer,
    customer_notice_instruction as customer_notice_instruction,
    select_grouped_issue_rows as select_grouped_issue_rows,
    should_return_deterministic_issue_list as should_return_deterministic_issue_list,
)
from backend.intent.request_scope import (
    AUTHENTICATED_OWNER as AUTHENTICATED_OWNER,
    authenticated_owner_scope as authenticated_owner_scope,
)
from backend.intent.status_semantics import (
    ALL_STATUSES as ALL_STATUSES,
    apply_status_resolution as apply_status_resolution,
    resolution_is_grounded as resolution_is_grounded,
    resolve_status_intent as resolve_status_intent,
    status_resolution_scope as status_resolution_scope,
)
from backend.domain.team import has_mantis_team_scope as has_mantis_team_scope
from backend.execution import checkpoint as checkpoint, request_execution as request_execution
from backend.admission import CHAT_ADMISSION as CHAT_ADMISSION
from backend.audit import (
    CHAT_STREAM_CONTEXT as CHAT_STREAM_CONTEXT,
    emit_chat_event as emit_chat_event,
    emit_chat_status as emit_chat_status,
    load_audit_query_plan as load_audit_query_plan,
    load_audit_sources as load_audit_sources,
)
from backend.utils.numbers import clamp_int as clamp_int
from backend.llm import (
    build_generation_options as build_generation_options,
    consume_model_response as consume_model_response,
    extract_model_content as extract_model_content,
    get_model_api_key as get_model_api_key,
    get_model_registry as get_model_registry,
    get_fallback_models as get_fallback_models,
    get_private_model as get_private_model,
    is_ollama_model as is_ollama_model,
    post_model_chat as post_model_chat,
)
from backend.intent.list_continuation import (
    list_continuation_mode as list_continuation_mode,
    confirmed_list_continuation as confirmed_list_continuation,
    remove_list_continuation_request as remove_list_continuation_request,
)
from backend.query_planning import build_query_plan as build_query_plan
from backend.query.operations import extract_group_by as extract_group_by
from backend.timing import TIMINGS as TIMINGS, measured as measured, request_timing as request_timing
from backend.intent.keyword_terms import (
    build_keyword_terms as build_keyword_terms,
    extract_requested_limit as extract_requested_limit,
    resolve_result_limit as resolve_result_limit,
    wants_all_results as wants_all_results,
)
from backend.intent.context import (
    is_affirmative_read_follow_up as is_affirmative_read_follow_up,
    build_contextual_retrieval_text as build_contextual_retrieval_text,
    get_latest_user_text as get_latest_user_text,
    get_recent_user_texts as get_recent_user_texts,
    is_follow_up_question as is_follow_up_question,
    resolve_reference_from_context as resolve_reference_from_context,
    rewrite_follow_up_query_with_model as rewrite_follow_up_query_with_model,
)
from backend.notion.formatting import (
    append_notion_completeness_notice as append_notion_completeness_notice,
    format_notion_missing_information_answer as format_notion_missing_information_answer,
    build_sources_from_notion_pages as build_sources_from_notion_pages,
    format_deterministic_notion_answer as format_deterministic_notion_answer,
    should_return_deterministic_notion_list as should_return_deterministic_notion_list,
)
from backend.intent.confirmed_context import (
    confirmed_scope_text as confirmed_scope_text,
    confirmed_constraints_scope as confirmed_constraints_scope,
    merge_confirmed_constraints as merge_confirmed_constraints,
    restore_confirmed_constraints as restore_confirmed_constraints,
    confirmed_keyword_text as confirmed_keyword_text,
)
from backend.reports.aggregate import (
    build_mantis_count_report as build_mantis_count_report,
    build_mantis_pivot_report as build_mantis_pivot_report,
    build_mantis_spike_report as build_mantis_spike_report,
    build_top_customers_report as build_top_customers_report,
)
from backend.reports.effort import (
    build_effort_ranking_report as build_effort_ranking_report,
    build_effort_report as build_effort_report,
    build_effort_report_from_plan as build_effort_report_from_plan,
)
from backend.reports.effort_trend import (
    build_effort_trend_report as build_effort_trend_report,
    build_effort_trend_report_from_plan as build_effort_trend_report_from_plan,
)
from backend.evaluation.finalization import finalize_chat_response as persist_chat_response
from backend.reports.customer import build_customer_profile_report as build_customer_profile_report
from backend.reports.incident_triage import build_incident_triage_report as build_incident_triage_report
from backend.reports.monthly_customer import build_monthly_customer_report as build_monthly_customer_report
from backend.reports.similar import build_similar_resolution_report as build_similar_resolution_report
from backend.reports.sla import build_sla_report as build_sla_report
from backend.reports.weekly_customer import build_weekly_report as build_weekly_report
from backend.evidence.model_context import (
    build_model_evidence_sources as build_model_evidence_sources,
    build_model_retrieval_context as build_model_retrieval_context,
)
from backend.retrieval import build_sources as build_sources
from backend.evidence.retrieval_rows import collect_retrieved_issues as collect_retrieved_issues
from backend.services.retrieval_pipeline import run_retrieval_pipeline as run_retrieval_pipeline
from backend.services.contracts import (
    ChatPipelineContext as ChatPipelineContext,
    DeterministicReportResult as DeterministicReportResult,
    StageResult as StageResult,
)
from backend.services.report_dispatcher import (
    ReportRequest as ReportRequest,
    dispatch_reports as dispatch_reports,
)
from backend.schemas import ChatRequest as ChatRequest
from backend.sources.mantis_fields import build_sources_from_issue_rows as build_sources_from_issue_rows
from backend.notion.matching import collect_retrieved_notion_pages as collect_retrieved_notion_pages
from backend.notion.search import inherit_notion_context as inherit_notion_context
from backend.notion.write import run_notion_write_command as run_notion_write_command
from backend.intent.constraints import (
    extract_query_constraints as extract_query_constraints,
    merge_query_plan_constraints as merge_query_plan_constraints,
    plan_search_intent_with_model as plan_search_intent_with_model,
)
from backend.intent.source_router import (
    rule_based_search_intent as rule_based_search_intent,
    should_run_retrieval as should_run_retrieval,
)
from backend.services.chat.scope import (
    IDENTITY_QUESTIONS as IDENTITY_QUESTIONS,
    PERSONAL_SCOPE_GROUP_MARKERS as PERSONAL_SCOPE_GROUP_MARKERS,
    PERSONAL_SCOPE_PRONOUN_NAMES as PERSONAL_SCOPE_PRONOUN_NAMES,
    PERSONAL_SCOPE_DIRECT_PATTERN as PERSONAL_SCOPE_DIRECT_PATTERN,
    PERSONAL_SCOPE_SPACED_PATTERN as PERSONAL_SCOPE_SPACED_PATTERN,
    PERSONAL_SCOPE_COMPACT_PATTERN as PERSONAL_SCOPE_COMPACT_PATTERN,
    NOTION_PERSONAL_SCOPE_PATTERN as NOTION_PERSONAL_SCOPE_PATTERN,
    is_identity_question as is_identity_question,
    format_authenticated_identity as format_authenticated_identity,
    has_personal_mantis_scope_phrase as has_personal_mantis_scope_phrase,
    should_scope_to_authenticated_mantis_user as should_scope_to_authenticated_mantis_user,
    should_scope_to_authenticated_notion_user as should_scope_to_authenticated_notion_user,
    apply_authenticated_mantis_scope as apply_authenticated_mantis_scope,
    apply_authenticated_notion_scope as apply_authenticated_notion_scope,
)
from backend.services.chat.reports import (
    build_deterministic_report as build_deterministic_report,
    _build_deterministic_report as _build_deterministic_report,
)
from backend.services.chat.generate import (
    generate_model_response as generate_model_response,
    _read_model_response as _read_model_response,
)
from backend.services.chat.prepare import prepare_chat_pipeline as prepare_chat_pipeline
from backend.services.agent.policy import request_transfer_scope as request_transfer_scope
from backend.services.agent.route import run_agent_if_eligible as run_agent_if_eligible
from backend.services.chat.planning import (
    plan_chat_query as plan_chat_query,
    _resolve_chat_plan as _resolve_chat_plan,
    can_skip_general_planner as can_skip_general_planner,
    _plan_chat_query as _plan_chat_query,
)
from backend.services.chat.retrieve import (
    should_retrieve_chat_evidence as should_retrieve_chat_evidence,
    retrieval_limit as retrieval_limit,
    deterministic_retrieval_response as deterministic_retrieval_response,
    append_retrieval_system_note as append_retrieval_system_note,
    retrieve_chat_evidence as retrieve_chat_evidence,
    _retrieve_chat_evidence as _retrieve_chat_evidence,
)


def attach_status_resolution(response: Dict[str, Any], context: ChatPipelineContext) -> Dict[str, Any]:
    if "retrieval" in response and response["retrieval"] is None:
        # 원문 재검증에서 폐기한 근거·상속 계획을 복구하지 않는다.
        return response
    if (context.intent_plan or {}).get("source") == "general":
        return response
    if (context.intent_plan or {}).get("planner") == "unified_llm":
        from copy import deepcopy
        retrieval = response.setdefault("retrieval", {"query": context.retrieval_text, "stages": []})
        retrieval["query_plan"] = deepcopy(context.intent_plan)
        retrieval.setdefault("constraints", deepcopy(context.query_constraints))
        return response
    resolution = context.status_resolution
    if not resolution:
        return response
    retrieval = response.setdefault("retrieval", {"query": context.retrieval_text, "stages": []})
    constraints = retrieval.setdefault("constraints", dict(context.query_constraints))
    constraints["status_resolution"] = resolution
    plan = retrieval.get("query_plan") or build_query_plan(context.retrieval_text, {"source": "mantis"}, constraints)
    plan["status_resolution"] = resolution
    plan["terms"] = build_keyword_terms(context.retrieval_text, constraints)
    plan["resolved_constraints"] = {key: value for key, value in constraints.items() if key != "status_resolution"}
    if constraints.get("inherited_from_audit_id"):
        plan["inherited_from_audit_id"] = constraints["inherited_from_audit_id"]
    retrieval["query_plan"] = plan
    if resolution.get("status_intent") == "open" and not resolution.get("needs_clarification"):
        evidence = str(resolution.get("evidence") or "")
        inherited = "이전 질문에서 상속한 " if resolution.get("origin") == "inherited" else ""
        response["content"] = f"미종료 기준 적용({inherited}근거: ‘{evidence}’, 해결·종료 제외)\n\n" + response.get("content", "")
    return response


class ChatService:
    """단일 채팅 요청의 전체 검색·답변 파이프라인을 실행한다."""

    def build_response(
        self,
        request: ChatRequest,
        user: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        user_key = str((user or {}).get("uid") or "anonymous").strip()
        with CHAT_ADMISSION.wait(user_key):
            return self._build_response(request, user=user)

    def build_admitted_response(
        self,
        request: ChatRequest,
        user: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """라우터가 admission lease를 이미 보유한 요청을 실행한다."""
        return self._build_response(request, user=user)

    @request_execution
    @request_timing
    def _build_response(
        self,
        request: ChatRequest,
        user: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        started = perf_counter()
        from backend.services.answer_context import bind_answer_context
        request = bind_answer_context(request, user)
        verification_question: Optional[str] = None
        def finalize_chat_response(response: Dict[str, Any]) -> Dict[str, Any]:
            response["timings"] = {"stages": dict(TIMINGS.get() or {}),
                                   "beforeFinalizationMs": round((perf_counter() - started) * 1000, 2)}
            return persist_chat_response(
                request,
                latest_user_text,
                response,
                user_uid=str((user or {}).get("uid") or "").strip(),
                **({"verification_question": verification_question} if verification_question is not None else {}),
            )

        emit_chat_status("질문과 대화 맥락을 분석하고 있습니다", "analysis")
        latest_user_text = get_latest_user_text(request.messages)
        if is_identity_question(latest_user_text):
            return finalize_chat_response(
                {
                    "modelId": request.modelId,
                    "content": format_authenticated_identity(user),
                    "sources": [],
                    "followUps": [
                        "내 담당 미종료 이슈 보여줘",
                        "내 장기 미종료 이슈 알려줘",
                        "이번 주 내 업무보고서 작성해줘",
                    ],
                    "raw": {"mode": "authenticated_identity"},
                }
            )

        # 도구 호출 가능 여부와 별개로 플래너부터 전송 정책을 적용한다.
        with request_transfer_scope(request.modelId) as agent_policy:
            from backend.services.agent.query_shadow import QUERY_SHADOW
            if QUERY_SHADOW.get() and agent_policy is None:
                from backend.services.agent.route import shadow_skipped
                return finalize_chat_response(shadow_skipped(request, "승인되고 도구 호출이 검증된 모델이 필요합니다."))
            context = prepare_chat_pipeline(request, latest_user_text, user)
            if (context.intent_plan or {}).get("planner") == "unified_llm":
                # 사용자에게 묶인 서버 기록에서 복원한 계획 입력을 검증에도 사용한다.
                # 응답/모델이 반환한 query나 grounding_text를 검증 근거로 채택하지 않는다.
                verification_question = context.retrieval_text
            with confirmed_constraints_scope(context.retrieval_text, context.query_constraints):
                return self._complete_response(request, context, user, finalize_chat_response)

    def _complete_response(
        self,
        request: ChatRequest,
        context: ChatPipelineContext,
        user: Optional[Dict[str, Any]],
        finalize_chat_response: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
        from backend.services.agent.route import run_query_shadow_if_requested
        shadow = run_query_shadow_if_requested(request, context)
        if shadow is not None:
            return finalize_chat_response(attach_status_resolution(shadow, context))
        # 진입한 뒤에는 실패해도 아래 기존 생성 경로로 돌아오지 않고 에이전트가 근거 안내로 끝낸다.
        agent_response = measured("agent")(run_agent_if_eligible)(request, context)
        if agent_response is not None:
            return finalize_chat_response(attach_status_resolution(agent_response, context))

        # 조건 준비를 에이전트가 맡은 경우에도 기존 실행기는 갱신된 확정 조건을 사용한다.
        with confirmed_constraints_scope(context.retrieval_text, context.query_constraints):
            return self._complete_legacy_response(request, context, user, finalize_chat_response)

    def _complete_legacy_response(self, request, context, user, finalize_chat_response):
        planned = plan_chat_query(request, context, user)
        if planned.response is not None:
            return finalize_chat_response(attach_status_resolution(planned.response, context))

        retrieved = retrieve_chat_evidence(request, context)
        if retrieved.response is not None:
            return finalize_chat_response(attach_status_resolution(retrieved.response, context))

        if context.system_notes:
            context.messages.insert(
                0,
                {"role": "system", "content": "\n\n".join(context.system_notes)},
            )
        if (context.intent_plan or {}).get("operation") == "explain" and (context.intent_plan or {}).get("source") == "mantis":
            from backend.services.grounded_explanation import generate_grounded_explanation
            generated_response = measured("generation")(generate_grounded_explanation)(request, context, generate_model_response)
        else:
            generated_response = measured("generation")(generate_model_response)(
                request, context.model, context.messages, context.retrieval_result,
                context.query_constraints, context.active_limit,
            )
        generated = StageResult.from_response(generated_response)
        assert generated.response is not None
        return finalize_chat_response(attach_status_resolution(generated.response, context))


chat_service = ChatService()
