"""공유 채팅 문맥의 질의 계획과 즉시 응답을 결정한다."""

import os
import re
from typing import Any, Dict, Optional

from backend.intent.request_scope import authenticated_owner_scope
from backend.intent.status_semantics import (
    apply_status_resolution,
    resolve_status_intent,
    status_resolution_scope,
)
from backend.audit import emit_chat_status
from backend.utils.numbers import clamp_int
from backend.query_planning import build_query_plan
from backend.query.operations import extract_group_by
from backend.timing import measured
from backend.intent.keyword_terms import build_keyword_terms, extract_requested_limit
from backend.reports.aggregate import build_mantis_count_report
from backend.reports.effort import build_effort_report_from_plan
from backend.reports.effort_trend import build_effort_trend_report_from_plan
from backend.services.contracts import ChatPipelineContext, StageResult
from backend.schemas import ChatRequest
from backend.intent.constraints import merge_query_plan_constraints, plan_search_intent_with_model
from backend.intent.source_router import rule_based_search_intent, should_run_retrieval
from backend.services.chat.reports import build_deterministic_report
from backend.services.chat.scope import (
    should_scope_to_authenticated_mantis_user,
    should_scope_to_authenticated_notion_user,
)


@measured("planning")
def plan_chat_query(request: ChatRequest, context: ChatPipelineContext, user: Optional[Dict[str, Any]]) -> StageResult:
    return StageResult.from_response(_resolve_chat_plan(request, context, user))


def _resolve_chat_plan(request: ChatRequest, context: ChatPipelineContext, user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if (context.intent_plan or {}).get("planner") == "unified_llm":
        from backend.services.unified_planning import execute_plan
        return execute_plan(request, context, user)
    from backend.intent.general import is_general_question
    if is_general_question(context.latest_user_text):
        context.intent_plan = {"source": "general", "operation": "explain", "planner": "rule", "authority": "authoritative"}
        return None
    if context.query_constraints.get("list_context_missing"):
        return {"modelId": request.modelId,
                "content": "이어서 조회할 이전 검색 기록을 확인하지 못했습니다. 고객사·기간·담당자·플랫폼 등 원래 조회 조건으로 다시 검색해 주세요.",
                "sources": [], "raw": {"mode": "list_context_clarification"}}
    if context.query_constraints.get("unconfirmed_search_terms"):
        terms = ", ".join(str(term) for term in context.query_constraints["unconfirmed_search_terms"])
        plan = build_query_plan(context.retrieval_text, {"source": "mantis"}, context.query_constraints)
        plan["resolved_constraints"] = dict(context.query_constraints)
        return {
            "modelId": request.modelId,
            "content": (
                f"이전 대화의 검색어 조건({terms})을 확인해야 합니다. "
                "필요한 검색어와 제목 조건을 ‘키워드 \"차트\" 포함’처럼 모두 다시 지정해 주세요. "
                "검색어 제한 없이 조회하려면 고객사·담당자 등 필요한 조건을 포함해 새로 질문해 주세요. "
                "조건을 확인하기 전에는 건수를 집계하지 않았습니다."
            ),
            "sources": [],
            "retrieval": {"query": context.retrieval_text, "constraints": context.query_constraints,
                          "query_plan": plan, "stages": []},
            "raw": {"mode": "search_terms_clarification"},
        }
    if user and not context.authenticated_owner and should_scope_to_authenticated_mantis_user(context.latest_user_text):
        return {"modelId": request.modelId, "content": "개인 이슈를 조회할 담당자명이 등록되어 있지 않습니다. 관리자에게 Mantis 담당자명 등록을 요청해 주세요.",
                "sources": [], "raw": {"mode": "missing_personal_scope"}}
    if user and not context.authenticated_owner and should_scope_to_authenticated_notion_user(context.latest_user_text):
        return {"modelId": request.modelId, "content": "개인 Notion 문서를 조회할 사용자 이름이 등록되어 있지 않습니다. 계정 표시 이름을 확인해 주세요.",
                "sources": [], "raw": {"mode": "missing_personal_notion_scope"}}
    from backend.intent.explanation import explanation_constraints, wants_issue_explanation
    if context.enabled_tools.get("mantis") and wants_issue_explanation(context.retrieval_text, context.query_constraints):
        context.query_constraints = explanation_constraints(context.retrieval_text, context.query_constraints)
        context.intent_plan = {**build_query_plan(context.retrieval_text, {"source": "mantis"}, context.query_constraints),
                               "source": "mantis", "operation": "explain", "planned": True,
                               "resolved_constraints": context.query_constraints, "terms": [],
                               "reason": "이슈 원문과 노트에 근거한 설명"}
        context.status_resolution = None
        return None
    if context.enabled_tools.get("mantis"):
        from backend.reports.work_queries import build_work_query_report, work_operation
        refining_list = bool(context.query_constraints.get("list_scope_edit")
                             and context.query_constraints.get("inherited_from_audit_id")
                             and work_operation(context.latest_user_text, context.query_constraints) in {None, "list"})
        work_report = None if refining_list else build_work_query_report(
            context.latest_user_text, context.query_constraints,
            limit=extract_requested_limit(context.latest_user_text) or clamp_int(request.searchLimit, 1, 20) or 10,
        )
        if work_report:
            context.query_constraints = work_report["retrieval"]["constraints"]
            context.intent_plan = work_report["retrieval"]["query_plan"]
            context.status_resolution = None
            if not request.showSources:
                work_report["sourcesForFollowUps"] = work_report["sources"]
                work_report["sources"] = []
            return {"modelId": request.modelId, **work_report}
    with authenticated_owner_scope(context.authenticated_owner):
        source = rule_based_search_intent(context.retrieval_text, context.query_constraints).get("source")
        mantis_scope = source == "mantis" or (source == "general" and bool(
            context.query_constraints.get("customer_key") or context.query_constraints.get("platform_key")
        ))
        if context.enabled_tools.get("mantis") and mantis_scope:
            resolution = context.status_resolution or resolve_status_intent(context.retrieval_text, context.query_constraints)
            context.status_resolution = resolution
            if resolution.get("needs_clarification"):
                return {"modelId": request.modelId,
                    "content": "조회할 상태를 확정하지 못했습니다. 해결·종료를 제외한 미종료 이슈를 뜻하시나요, 아니면 상태와 관계없는 전체 이슈를 뜻하시나요? 상태 조건을 빼고 집계하지 않았습니다.",
                    "sources": [], "raw": {"mode": "status_clarification"}}
            with status_resolution_scope(resolution):
                context.query_constraints = apply_status_resolution(context.retrieval_text, context.query_constraints, resolution)
                return _plan_chat_query(request, context, user)
        context.status_resolution = None
        return _plan_chat_query(request, context, user)


def can_skip_general_planner(text: str, constraints: Dict[str, Any], rule_plan: Dict[str, Any]) -> bool:
    """의미가 이미 확정된 단순 조회만 일반 플래너를 생략한다."""
    if rule_plan.get("source") == "notion":
        from backend.notion.formatting import is_simple_notion_title_lookup
        return is_simple_notion_title_lookup(text)
    if rule_plan.get("source") not in {"mantis", "general"}:
        return False
    if (constraints.get("list_scope_edit") and constraints.get("inherited_from_audit_id")
            and not build_keyword_terms(text, constraints)):
        return True
    from backend.intent.status_semantics import is_structured_status_neutral_query
    if is_structured_status_neutral_query(text, constraints):
        return True
    if re.search(r"비슷|유사|원인|분석|비교|추이|예측|관련|방법|해결책", text):
        return False
    from backend.query.operations import infer_operation
    operation = infer_operation(text)
    if operation == "get" and constraints.get("issue_ids"):
        return True
    if operation not in {"count", "search"} or extract_group_by(text):
        return False
    if operation == "search" and not re.search(r"이슈|목록|리스트", text):
        return False
    if not (constraints.get("customer_key") or constraints.get("mantis_team_members")):
        return False
    keyword_text = re.sub(r"(?:전체|모든)\s*상태(?:로는|로|만)?", " ", text)
    return not build_keyword_terms(keyword_text, constraints)


def _plan_chat_query(
    request: ChatRequest,
    context: ChatPipelineContext,
    user: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """결정형 리포트와 질의 계획을 평가하고 즉시 응답이 있으면 반환한다."""
    deterministic = measured("reports")(build_deterministic_report)(
        context.latest_user_text,
        context.retrieval_text,
        context.enabled_tools,
        request.messages,
        user,
    )
    if deterministic.response:
        return {"modelId": request.modelId, **deterministic.response}
    context.pending_effort_report = deterministic.pending_effort_report
    if not context.retrieval_text:
        return None

    rule_plan = rule_based_search_intent(context.retrieval_text, context.query_constraints)
    fast_path = can_skip_general_planner(context.retrieval_text, context.query_constraints, rule_plan)
    if fast_path:
        # '그중 전체 상태 몇 건'은 소스 단어가 생략되어도 확정 고객사 범위가 있다.
        rule_plan = {**rule_plan, "source": "notion" if rule_plan.get("source") == "notion" else "mantis", "reason": "확정된 구조화 조회"}
    model_plan: Dict[str, Any] = {}
    planner_enabled = os.getenv("QUERY_PLANNER_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    should_plan = (
        planner_enabled
        and bool(context.api_key)
        and not fast_path
        and any(context.enabled_tools.get(key) for key in ("mantis", "notion", "vectorDb"))
        and (
            rule_plan.get("source") in {"mantis", "notion", "both"}
            or should_run_retrieval(context.retrieval_text, context.query_constraints)
        )
    )
    if should_plan:
        emit_chat_status("질문을 도구 실행 계획으로 변환하고 있습니다", "routing")
        model_plan = measured("query_planner")(plan_search_intent_with_model)(
            context.retrieval_text,
            context.model,
            context.api_key,
        )
        if rule_plan.get("authority") == "authoritative":
            model_plan["source"] = rule_plan["source"]

    query_plan = build_query_plan(
        context.retrieval_text,
        rule_plan,
        context.query_constraints,
        model_plan if model_plan.get("planned") else None,
    )
    context.query_constraints = merge_query_plan_constraints(
        context.query_constraints,
        query_plan,
    )
    context.intent_plan = {
        **query_plan,
        "planned": True,
        "ruleFallback": rule_plan,
    }

    if context.enabled_tools.get("mantis") and model_plan.get("planned"):
        trend_fallback = build_effort_trend_report_from_plan(
            context.retrieval_text,
            model_plan,
            constraints=context.query_constraints,
        )
        if trend_fallback:
            return {"modelId": request.modelId, **trend_fallback}

    if context.pending_effort_report is not None and context.enabled_tools.get("mantis"):
        effort_fallback = build_effort_report_from_plan(context.retrieval_text, model_plan)
        if effort_fallback:
            return {"modelId": request.modelId, **effort_fallback}
        return {"modelId": request.modelId, **context.pending_effort_report}

    if context.enabled_tools.get("mantis"):
        count_report = measured("count")(build_mantis_count_report)(context.retrieval_text, query_plan)
        if count_report:
            return {"modelId": request.modelId, **count_report}
    return None
