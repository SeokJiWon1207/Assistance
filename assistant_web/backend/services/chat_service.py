"""채팅 요청 오케스트레이션 서비스.

HTTP 라우팅과 분리해 질문 해석, 검색, 결정형 응답, 모델 폴백을 조정한다.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException

from backend.answers import (
    CONVERSATION_TONE_NOTE,
    answer_style_instruction,
    build_current_date_note,
    build_unknown_customer_report,
    format_deterministic_combined_answer,
    format_deterministic_issue_answer,
    format_model_timeout_fallback,
    sanitize_customer_notice_answer,
    select_grouped_issue_rows,
    should_return_deterministic_issue_list,
)
from backend.intent.request_scope import AUTHENTICATED_OWNER, authenticated_owner_scope
from backend.admission import CHAT_ADMISSION
from backend.audit import CHAT_STREAM_CONTEXT, emit_chat_event, emit_chat_status
from backend.common import clamp_int
from backend.llm import (
    build_generation_options,
    extract_model_content,
    get_model_api_key,
    get_model_registry,
    get_private_model,
    is_ollama_model,
    post_model_chat,
)
from backend.query_planning import build_query_plan, extract_group_by
from backend.reports.aggregate import (
    build_mantis_count_report,
    build_mantis_pivot_report,
    build_mantis_spike_report,
    build_top_customers_report,
)
from backend.reports.effort import (
    build_effort_ranking_report,
    build_effort_report,
    build_effort_report_from_plan,
)
from backend.reports.effort_trend import (
    build_effort_trend_report,
    build_effort_trend_report_from_plan,
)
from backend.reports.evaluation import finalize_chat_response as persist_chat_response
from backend.reports.customer import build_customer_profile_report
from backend.reports.incident_triage import build_incident_triage_report
from backend.reports.monthly_customer import build_monthly_customer_report
from backend.reports.similar import build_similar_resolution_report
from backend.reports.sla import build_sla_report
from backend.reports.weekly_customer import build_weekly_report
from backend.retrieval import (
    build_model_retrieval_context,
    build_sources,
    collect_retrieved_issues,
    run_retrieval_pipeline,
)
from backend.schemas import ChatRequest
from backend.sources.mantis import build_sources_from_issue_rows
from backend.sources.notion import (
    build_sources_from_notion_pages,
    collect_retrieved_notion_pages,
    format_deterministic_notion_answer,
    inherit_notion_context,
    run_notion_write_command,
    should_return_deterministic_notion_list,
)
from backend.understanding import (
    build_contextual_retrieval_text,
    extract_query_constraints,
    extract_requested_limit,
    get_latest_user_text,
    get_recent_user_texts,
    merge_query_plan_constraints,
    plan_search_intent_with_model,
    is_follow_up_question,
    resolve_reference_from_context,
    resolve_result_limit,
    rewrite_follow_up_query_with_model,
    rule_based_search_intent,
    should_run_retrieval,
)


IDENTITY_QUESTIONS = {
    "내가누구야",
    "나는누구야",
    "내가누구지",
    "나는누구지",
    "내이름뭐야",
    "내이름이뭐야",
    "내정보알려줘",
    "내계정정보알려줘",
    "내로그인정보알려줘",
    "지금로그인한사람누구야",
}

PERSONAL_SCOPE_GROUP_MARKERS = ("팀", "팀원", "부서", "조직", "전체", "우리")
PERSONAL_SCOPE_PRONOUN_NAMES = {
    "나", "나에게", "나한테", "내", "내게", "내가",
    "저", "저에게", "저한테", "제", "제가", "본인",
}
PERSONAL_SCOPE_DIRECT_PATTERN = re.compile(
    r"(?:^|[\s,])(?:"
    r"내\s*(?:담당|이슈|업무)|나의\s*이슈|"
    # "내가 지금 처리해야 하는", "제가 맡은"처럼 화자가 주어인 표현.
    r"(?:내가|제가)\s*(?:.{0,12}?)(?:맡은|담당한|처리|해결|대응|해야)|"
    # "나한테 배정된", "저에게 할당된"처럼 화자가 수신자인 표현.
    r"(?:내게|나에게|나한테|저에게|저한테|본인에게)\s*(?:.{0,8}?)(?:할당|배정|맡겨|지정)"
    r")",
    re.IGNORECASE,
)
PERSONAL_SCOPE_SPACED_PATTERN = re.compile(
    r"(?:^|[\s,])(?:내|나의|제|나|저)\s+(?P<qualifiers>.{0,40}?)(?:이슈|업무|건)"
    r"(?:를|을|가|은|는|도|만|의)?(?=$|[\s?!.,])",
    re.IGNORECASE,
)
PERSONAL_SCOPE_COMPACT_PATTERN = re.compile(
    r"(?:^|[\s,])(?:내|나의|제)(?:(?:최근|미종료|장기|열린|진행중|처리중|종료된|완료된))*"
    r"(?:이슈|업무|건)",
    re.IGNORECASE,
)


def is_identity_question(text: str) -> bool:
    normalized = re.sub(r"[\s?!.,]+", "", str(text or "")).casefold()
    return normalized in IDENTITY_QUESTIONS


def format_authenticated_identity(user: Optional[Dict[str, Any]]) -> str:
    if not user:
        return "현재 로그인한 사용자 정보를 확인할 수 없습니다. 다시 로그인해 주세요."
    role_labels = {"admin": "관리자", "manager": "매니저", "member": "사용자"}
    display_name = str(user.get("displayName") or user.get("email") or "사용자").strip()
    role = role_labels.get(str(user.get("role") or "member"), str(user.get("role") or "사용자"))
    team = str(user.get("team") or "미설정").strip()
    mantis_name = str(user.get("mantisUsername") or display_name).strip()
    return (
        f"{display_name}님으로 로그인되어 있습니다.\n\n"
        f"- 역할: {role}\n"
        f"- 팀: {team}\n"
        f"- Mantis 담당자: {mantis_name}"
    )


def has_personal_mantis_scope_phrase(text: str) -> bool:
    """개인 소유 표현과 이슈·업무 대상을 함께 확인한다.

    ``내 최근 이슈``처럼 수식어가 사이에 들어간 표현을 허용하되,
    ``내 팀 이슈``나 ``사내 이슈``를 로그인 사용자 개인 범위로 오인하지 않는다.
    """
    raw_text = str(text or "").strip()
    if PERSONAL_SCOPE_DIRECT_PATTERN.search(raw_text):
        return True
    if PERSONAL_SCOPE_COMPACT_PATTERN.search(raw_text):
        return True
    for match in PERSONAL_SCOPE_SPACED_PATTERN.finditer(raw_text):
        qualifiers = re.sub(r"\s+", "", match.group("qualifiers")).casefold()
        if not any(marker in qualifiers for marker in PERSONAL_SCOPE_GROUP_MARKERS):
            return True
    return False


def should_scope_to_authenticated_mantis_user(text: str) -> bool:
    """개인 업무 요청인지 판정해 로그인 사용자의 Mantis 담당 범위를 적용한다."""
    normalized = re.sub(r"\s+", "", str(text or "")).casefold()
    if any(marker in normalized for marker in ("notion", "노션", "노선")):
        return False
    constraints = extract_query_constraints(text)
    explicit_names = {
        str(name or "").strip().casefold()
        for name in constraints.get("people", {}).get("names") or []
    } - PERSONAL_SCOPE_PRONOUN_NAMES
    if explicit_names:
        return False
    if any(
        marker in normalized
        for marker in ("업무보고서", "주간보고서", "주간업무보고", "주간업무")
    ):
        # 고객사를 명시한 주간 보고서는 그 고객사 전체 현황이다. 로그인 사용자의
        # 담당 이슈로 다시 좁히면 팀 템플릿의 집계값이 사람마다 달라진다.
        return not bool(constraints.get("customer_key") or constraints.get("customer_exact"))
    return has_personal_mantis_scope_phrase(text)


def apply_authenticated_mantis_scope(
    text: str,
    user: Optional[Dict[str, Any]],
    *,
    scope_text: Optional[str] = None,
) -> str:
    """개인 Mantis 요청에 인증 계정의 담당자명을 검색 조건으로 명시한다."""
    mantis_username = str((user or {}).get("mantisUsername") or (user or {}).get("displayName") or "").strip()
    if not mantis_username or not should_scope_to_authenticated_mantis_user(scope_text or text):
        return text
    return f"{text} 담당자: {mantis_username}".strip()


@dataclass
class DeterministicReportResult:
    """LLM 계획 전에 실행하는 결정형 리포트의 판정 결과."""

    response: Optional[Dict[str, Any]] = None
    pending_effort_report: Optional[Dict[str, Any]] = None


def build_deterministic_report(
    latest_user_text: str,
    retrieval_text: str,
    enabled_tools: Dict[str, Any],
    messages: List[Any],
    user: Optional[Dict[str, Any]],
) -> DeterministicReportResult:
    scope_text = " ".join(get_recent_user_texts(messages, limit=5)) if is_follow_up_question(latest_user_text) else latest_user_text
    owner = AUTHENTICATED_OWNER.get()
    if not owner and should_scope_to_authenticated_mantis_user(scope_text):
        owner = str((user or {}).get("mantisUsername") or (user or {}).get("displayName") or "").strip()
    with authenticated_owner_scope(owner):
        return _build_deterministic_report(latest_user_text, retrieval_text, enabled_tools, messages, user)


def _build_deterministic_report(
    latest_user_text: str,
    retrieval_text: str,
    enabled_tools: Dict[str, Any],
    messages: List[Any],
    user: Optional[Dict[str, Any]],
) -> DeterministicReportResult:
    """우선순위가 있는 결정형 리포트 중 첫 번째 일치 결과를 반환한다."""
    if not latest_user_text:
        return DeterministicReportResult()

    if enabled_tools.get("mantis"):
        # 순서 계약: 아래 빌더는 첫 번째 일치 결과가 곧 최종 응답이므로 위치가 동작이다.
        # 1) 잘못된 고객사를 먼저 차단해 조건 없는 전체 조회로 넓어지는 것을 막는다.
        # 2) 기준 이슈가 명시된 유사 해결 검색은 일반 집계보다 구체적이라 먼저 처리한다.
        # 3) 월간·주간 리포트와 고객사 프로필처럼 여러 지표를 묶는 요청은 단일 SLA보다 앞선다.
        # 4) 피벗은 두 축 집계, 급증은 시점 비교이므로 단순 상위 고객사보다 앞선다.
        # 5) 공수 추이·순위는 이슈 건수 집계가 일치하지 않은 뒤에 평가한다.
        report_builders = [
            (
                "unknown_customer_guard",
                lambda: build_unknown_customer_report(retrieval_text or latest_user_text),
            ),
        ]
        if enabled_tools.get("vectorDb"):
            report_builders.append(
                ("similar_resolution", lambda: build_similar_resolution_report(retrieval_text or latest_user_text))
            )
        report_builders.append((
            "incident_triage",
            lambda: build_incident_triage_report(
                retrieval_text or latest_user_text, search_similar=bool(enabled_tools.get("vectorDb")),
            ),
        ))
        report_builders.extend(
            [
                ("monthly_customer", lambda: build_monthly_customer_report(retrieval_text or latest_user_text)),
                ("weekly_report", lambda: build_weekly_report(retrieval_text or latest_user_text)),
                ("customer_profile", lambda: build_customer_profile_report(retrieval_text or latest_user_text)),
                ("sla", lambda: build_sla_report(retrieval_text or latest_user_text)),
                ("pivot", lambda: build_mantis_pivot_report(retrieval_text or latest_user_text)),
                ("spike", lambda: build_mantis_spike_report(retrieval_text or latest_user_text)),
                ("top_customers", lambda: build_top_customers_report(retrieval_text or latest_user_text)),
                ("effort_trend", lambda: build_effort_trend_report(retrieval_text or latest_user_text)),
                ("effort_ranking", lambda: build_effort_ranking_report(retrieval_text or latest_user_text)),
            ]
        )
        for _report_name, build_report in report_builders:
            report = build_report()
            if report:
                return DeterministicReportResult(response=report)

        effort_report = build_effort_report(retrieval_text or latest_user_text)
        if effort_report:
            if effort_report.pop("needsPerson", False):
                pending_effort_report = effort_report
            else:
                return DeterministicReportResult(response=effort_report)
        else:
            pending_effort_report = None
    else:
        pending_effort_report = None

    if enabled_tools.get("notion"):
        notion_write_result = run_notion_write_command(
            latest_user_text,
            messages,
            allow_write=bool(user and user.get("role") in {"manager", "admin"}),
            user_uid=str((user or {}).get("uid") or ""),
        )
        if notion_write_result:
            return DeterministicReportResult(response=notion_write_result)

    return DeterministicReportResult(pending_effort_report=pending_effort_report)


def generate_model_response(
    request: ChatRequest,
    model: Dict[str, Any],
    messages: List[Dict[str, Any]],
    retrieval_result: Optional[Dict[str, Any]],
    query_constraints: Dict[str, Any],
    active_limit: int,
) -> Dict[str, Any]:
    """활성 모델 폴백, 스트림 해석, 검색 타임아웃 폴백을 한곳에서 처리한다."""
    candidate_models = [model] + [
        other
        for other in get_model_registry()
        if other["id"] != model["id"] and other.get("status") == "active"
    ]

    response = None
    final_model = model
    timeout_exc: Optional[requests.Timeout] = None
    request_exc: Optional[requests.RequestException] = None
    emit_chat_status("답변을 생성하고 있습니다", "generation")

    for index, candidate in enumerate(candidate_models):
        is_last_candidate = index == len(candidate_models) - 1
        candidate_api_key = get_model_api_key(candidate)
        if not candidate_api_key:
            continue

        candidate_payload = {
            "model": candidate.get("model", candidate["id"]),
            "messages": messages,
            "temperature": request.temperature,
            "stream": bool(request.stream),
        }
        candidate_payload.update(build_generation_options(request))
        final_model = candidate

        try:
            model_timeout = clamp_int(os.getenv("MODEL_TIMEOUT_SECONDS", "180"), 30, 300) or 180
            candidate_response = post_model_chat(
                candidate,
                candidate_api_key,
                candidate_payload,
                timeout=model_timeout,
                stream=bool(request.stream),
            )
        except requests.Timeout as exc:
            timeout_exc = exc
            if is_last_candidate:
                break
            continue
        except requests.RequestException as exc:
            request_exc = exc
            if is_last_candidate:
                break
            continue

        if candidate_response.status_code >= 400:
            if is_last_candidate:
                response = candidate_response
                final_model = candidate
                break
            continue

        response = candidate_response
        final_model = candidate
        timeout_exc = None
        request_exc = None
        break

    if response is None:
        connection_exc = timeout_exc or request_exc
        if retrieval_result and connection_exc is not None:
            fallback_sources = build_sources(retrieval_result, max_sources=active_limit)
            return {
                "modelId": final_model["id"],
                "content": format_model_timeout_fallback(
                    query_constraints,
                    retrieval_result,
                    active_limit,
                ),
                "sources": fallback_sources if request.showSources else [],
                "sourcesForFollowUps": fallback_sources,
                "retrieval": retrieval_result,
                "raw": {
                    "mode": "model_timeout_fallback",
                    "error": str(connection_exc),
                    "errorType": type(connection_exc).__name__,
                },
            }
        raise HTTPException(status_code=502, detail=f"Model connection failed: {connection_exc}")

    if response.status_code >= 400:
        unsupported_hint = ""
        if any(
            keyword in response.text.lower()
            for keyword in ("extra", "unknown", "unsupported", "unexpected", "not permitted")
        ):
            unsupported_hint = " 선택한 생성 옵션 중 회사 모델 서버가 지원하지 않는 값이 있을 수 있습니다. 해당 옵션을 비우고 다시 시도해 주세요."
        raise HTTPException(
            status_code=response.status_code,
            detail=(response.text[:1000] + unsupported_hint),
        )

    if request.stream and is_ollama_model(final_model):
        streamed_content = ""
        ollama_stream_chunks: List[Dict[str, Any]] = []
        for raw_line in response.iter_lines(decode_unicode=True):
            if isinstance(raw_line, bytes):
                line = raw_line.decode("utf-8", errors="replace").strip()
            else:
                line = str(raw_line or "").strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            ollama_stream_chunks.append(chunk)
            delta = chunk.get("message", {}).get("content", "")
            if delta:
                streamed_content += delta
                CHAT_STREAM_CONTEXT.content_streamed = True
                emit_chat_event("delta", content=delta)
        data = {
            "choices": [{"message": {"content": streamed_content}}],
            "streamChunks": len(ollama_stream_chunks),
            "provider": "ollama",
        }
    elif request.stream and "text/event-stream" in response.headers.get("content-type", "").lower():
        streamed_content = ""
        sse_stream_chunks: List[Dict[str, Any]] = []
        for raw_line in response.iter_lines(decode_unicode=True):
            line = str(raw_line or "").strip()
            if not line or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
            sse_stream_chunks.append(chunk)
            delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
            if delta:
                streamed_content += delta
                CHAT_STREAM_CONTEXT.content_streamed = True
                emit_chat_event("delta", content=delta)
        data = {
            "choices": [{"message": {"content": streamed_content}}],
            "streamChunks": len(sse_stream_chunks),
        }
    else:
        data = response.json()

    final_sources = build_sources(retrieval_result, max_sources=active_limit)
    content = sanitize_customer_notice_answer(
        messages[-1].get("content", "") if messages else "",
        extract_model_content(data),
    )
    return {
        "modelId": final_model["id"],
        "content": content or "응답 본문을 찾지 못했습니다.",
        "sources": final_sources if request.showSources else [],
        "sourcesForFollowUps": final_sources,
        "retrieval": retrieval_result,
        "raw": data,
        "usedFallbackModel": final_model["id"] if final_model["id"] != model["id"] else None,
    }


@dataclass
class ChatPipelineContext:
    """한 채팅 요청이 분석·라우팅·검색 단계를 지나며 공유하는 상태."""

    latest_user_text: str
    model: Dict[str, Any]
    api_key: str
    messages: List[Dict[str, Any]]
    retrieval_text: str
    enabled_tools: Dict[str, Any]
    system_notes: List[str]
    query_constraints: Dict[str, Any]
    active_limit: int
    authenticated_owner: str = ""
    pending_effort_report: Optional[Dict[str, Any]] = None
    intent_plan: Optional[Dict[str, Any]] = None
    retrieval_result: Optional[Dict[str, Any]] = None


def prepare_chat_pipeline(
    request: ChatRequest,
    latest_user_text: str,
    user: Optional[Dict[str, Any]],
) -> ChatPipelineContext:
    """모델·대화 문맥·인증 사용자 범위를 검증하고 검색 입력을 준비한다."""
    model = get_private_model(request.modelId)
    if model["status"] == "expired":
        raise HTTPException(status_code=400, detail="Selected model is expired")
    if model["status"] == "disabled":
        raise HTTPException(status_code=400, detail="Selected model is disabled")

    api_key = get_model_api_key(model)
    if not api_key:
        raise HTTPException(
            status_code=503,
            detail=f"Missing API key env: {model.get('apiKeyEnv')}",
        )

    messages = [message.model_dump(exclude={"notionActionId"}) for message in request.messages]
    history_limit = clamp_int(os.getenv("MODEL_HISTORY_MAX_MESSAGES", "20"), 2, 200) or 20
    if len(messages) > history_limit:
        messages = messages[-history_limit:]

    retrieval_text, needs_follow_up_rewrite = build_contextual_retrieval_text(request.messages)
    resolved_reference = resolve_reference_from_context(
        latest_user_text,
        request.contextAuditId,
        user_uid=str((user or {}).get("uid") or "").strip(),
    )
    if resolved_reference:
        retrieval_text = resolved_reference
        needs_follow_up_rewrite = False
    else:
        notion_prefix = inherit_notion_context(get_recent_user_texts(request.messages, limit=5))
        if notion_prefix:
            retrieval_text = f"{notion_prefix} {latest_user_text}".strip()
            needs_follow_up_rewrite = False

    if needs_follow_up_rewrite:
        rewritten = rewrite_follow_up_query_with_model(
            get_recent_user_texts(request.messages, limit=5),
            model,
            api_key,
        )
        if rewritten:
            retrieval_text = rewritten

    retrieval_text = retrieval_text or latest_user_text
    scope_text = retrieval_text
    if is_follow_up_question(latest_user_text):
        scope_text = " ".join(get_recent_user_texts(request.messages, limit=5))
    owner = str((user or {}).get("mantisUsername") or (user or {}).get("displayName") or "").strip()
    if not should_scope_to_authenticated_mantis_user(scope_text):
        owner = ""
    with authenticated_owner_scope(owner):
        scoped_constraints = extract_query_constraints(retrieval_text) if retrieval_text else {}
    retrieval_text = apply_authenticated_mantis_scope(
        retrieval_text,
        user,
        scope_text=scope_text,
    )
    enabled_tools = {"mantis": True, "vectorDb": True, "notion": True, **request.tools}
    system_notes = [
        answer_style_instruction(request.answerStyle),
        CONVERSATION_TONE_NOTE,
        build_current_date_note(),
    ]
    return ChatPipelineContext(
        latest_user_text=latest_user_text,
        model=model,
        api_key=api_key,
        messages=messages,
        retrieval_text=retrieval_text,
        enabled_tools=enabled_tools,
        system_notes=system_notes,
        query_constraints=scoped_constraints,
        authenticated_owner=owner,
        active_limit=clamp_int(request.searchLimit, 1, 20) or 5,
    )


def plan_chat_query(request: ChatRequest, context: ChatPipelineContext, user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if user and not context.authenticated_owner and should_scope_to_authenticated_mantis_user(context.latest_user_text):
        return {"modelId": request.modelId, "content": "개인 이슈를 조회할 담당자명이 등록되어 있지 않습니다. 관리자에게 Mantis 담당자명 등록을 요청해 주세요.",
                "sources": [], "raw": {"mode": "missing_personal_scope"}}
    with authenticated_owner_scope(context.authenticated_owner):
        return _plan_chat_query(request, context, user)


def _plan_chat_query(
    request: ChatRequest,
    context: ChatPipelineContext,
    user: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """결정형 리포트와 질의 계획을 평가하고 즉시 응답이 있으면 반환한다."""
    deterministic = build_deterministic_report(
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
    model_plan: Dict[str, Any] = {}
    planner_enabled = os.getenv("QUERY_PLANNER_ENABLED", "true").strip().lower() not in {
        "0",
        "false",
        "off",
    }
    should_plan = (
        planner_enabled
        and any(context.enabled_tools.get(key) for key in ("mantis", "notion", "vectorDb"))
        and (
            rule_plan.get("source") in {"mantis", "notion", "both"}
            or should_run_retrieval(context.retrieval_text, context.query_constraints)
        )
    )
    if should_plan:
        emit_chat_status("질문을 도구 실행 계획으로 변환하고 있습니다", "routing")
        model_plan = plan_search_intent_with_model(
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
        count_report = build_mantis_count_report(context.retrieval_text, query_plan)
        if count_report:
            return {"modelId": request.modelId, **count_report}
    return None


def should_retrieve_chat_evidence(context: ChatPipelineContext) -> bool:
    return bool(
        context.retrieval_text
        and any(context.enabled_tools.get(key) for key in ("mantis", "vectorDb", "notion"))
        and (
            (context.intent_plan or {}).get("source") in {"notion", "mantis", "both"}
            or should_run_retrieval(context.retrieval_text, context.query_constraints)
        )
    )


def retrieval_limit(request: ChatRequest, context: ChatPipelineContext) -> tuple[int, Optional[str]]:
    # 질문에 표시 개수가 적혀 있으면 플래너가 내놓은 숫자보다 우선한다. 플래너는
    # "하나만"처럼 한글로 쓴 개수를 놓치거나 다른 숫자를 지어내는 일이 있다.
    requested_limit = extract_requested_limit(context.latest_user_text) or (
        context.intent_plan or {}
    ).get("limit")
    sidebar_limit = clamp_int(request.searchLimit, 1, 20)
    group_by = (context.intent_plan or {}).get("group_by") or extract_group_by(
        context.retrieval_text
    )
    if group_by == "customer":
        return 800, group_by
    if group_by:
        return requested_limit or 200, group_by
    return requested_limit or resolve_result_limit(context.latest_user_text, sidebar_limit), group_by


def deterministic_retrieval_response(
    request: ChatRequest,
    context: ChatPipelineContext,
    limit: int,
    group_by: Optional[str],
) -> Optional[Dict[str, Any]]:
    """검색 결과가 결정형 목록 계약에 해당하면 모델 호출 없이 응답한다."""
    retrieval_result = context.retrieval_result or {}
    if (context.intent_plan or {}).get("source") == "both":
        combined_sources = build_sources(retrieval_result, max_sources=limit)
        if combined_sources:
            return {
                "modelId": request.modelId,
                "content": format_deterministic_combined_answer(combined_sources),
                "sources": combined_sources if request.showSources else [],
                "sourcesForFollowUps": combined_sources,
                "retrieval": retrieval_result,
                "raw": {"mode": "deterministic_combined_search"},
            }
    if should_return_deterministic_notion_list(context.retrieval_text, retrieval_result):
        notion_pages = collect_retrieved_notion_pages(retrieval_result, limit)
        notion_sources = build_sources_from_notion_pages(notion_pages)
        return {
            "modelId": request.modelId,
            "content": format_deterministic_notion_answer(
                context.retrieval_text,
                retrieval_result,
                limit,
            ),
            "sources": notion_sources if request.showSources else [],
            "sourcesForFollowUps": notion_sources,
            "retrieval": retrieval_result,
            "raw": {"mode": "deterministic_notion_list"},
        }
    if (context.intent_plan or {}).get("source") != "notion" and should_return_deterministic_issue_list(
        context.retrieval_text,
        context.query_constraints,
    ):
        deterministic_issues = select_grouped_issue_rows(
            collect_retrieved_issues(retrieval_result, None if group_by else limit),
            group_by,
        )[:limit]
        issue_sources = build_sources_from_issue_rows(deterministic_issues)
        return {
            "modelId": request.modelId,
            "content": format_deterministic_issue_answer(
                context.retrieval_text,
                context.query_constraints,
                retrieval_result,
                limit,
                request.answerStyle,
            ),
            "sources": issue_sources if request.showSources else [],
            "sourcesForFollowUps": issue_sources,
            "retrieval": retrieval_result,
            "raw": {"mode": "deterministic_issue_list"},
        }
    return None


def append_retrieval_system_note(context: ChatPipelineContext, limit: int) -> None:
    assert context.retrieval_result is not None, "검색 결과 없이 모델 컨텍스트를 만들 수 없습니다."
    model_retrieval_context = build_model_retrieval_context(context.retrieval_result, limit)
    context.system_notes.append(
        "너는 컨텐츠서비스 AI 비서다. 아래 [검색 파이프라인 결과] JSON에 있는 데이터만 사실로 사용해라. "
        "검색 파이프라인은 Notion DB 조회, Mantis 저장 필터, Mantis 페이지 스캔, ChromaDB 검색으로 구성된다. "
        "Notion 결과는 팀 주요 이슈/공지/일정/결재 상태 조회에 사용하고, Mantis 결과는 원본 이슈 목록 조회에 우선 사용하며, ChromaDB 결과는 유사 사례나 보조 맥락으로 사용해라. "
        "ChromaDB 문서에 latest_issue가 있으면 ChromaDB 스냅샷 대신 latest_issue 값을 최신 Mantis 원본으로 사용해라. "
        "다만 Mantis 결과가 요청 개수보다 부족하고 ChromaDB 결과가 질문의 고객사/담당자/키워드 조건과 일치하면, ChromaDB 결과도 실제 이슈 목록으로 포함해라. "
        "질문에 고객사, 담당자, HTS, MTS 같은 조건이 있으면 그 조건과 일치하는 이슈만 답변에 포함해라. "
        "예를 들어 HTS 질문에는 제목에 MTS가 있더라도 본문이나 제목에 HTS가 함께 있을 때만 포함하고, MTS 전용 이슈는 제외해라. "
        "실시간 조회를 할 수 없다고 말하지 마라. "
        "사용자가 검색 과정이나 기준을 묻지 않는 한 mantis_filter, mantis_page_scan, chroma_db 같은 내부 단계명이나 스킵 여부는 말하지 마라. "
        "대신 필요한 경우 '검색 기준: 고객사/담당자/상태/키워드, 정렬: 최신 이슈 ID 기준'처럼 사용자에게 의미 있는 기준만 짧게 말해라. "
        "이슈가 있으면 번호 목록으로 ID, 프로젝트, 제목, 상태, 담당자, 등록일을 포함해 요약해라.\n\n"
        f"[검색 파이프라인 결과]\n{json.dumps(model_retrieval_context, ensure_ascii=False, indent=2)}"
    )


def retrieve_chat_evidence(
    request: ChatRequest,
    context: ChatPipelineContext,
) -> Optional[Dict[str, Any]]:
    """최종 계획의 소스만 조회하고 결정형 목록 또는 모델 근거를 준비한다."""
    if not should_retrieve_chat_evidence(context):
        context.system_notes.append(
            "너는 컨텐츠서비스 AI 비서다. 사용자가 일반 대화나 모델/사용법을 묻는 경우에는 Mantis나 ChromaDB 이슈를 언급하지 말고 직접 답하라. "
            "사용자가 고객사 이슈, 담당자, 화면번호, HTS/MTS, Mantis 조회처럼 사내 데이터 검색을 명확히 요청할 때만 이슈 조회가 필요하다고 이해하라."
        )
        return None

    active_source_labels = [
        label
        for key, label in (("mantis", "Mantis"), ("notion", "Notion"), ("vectorDb", "Vector DB"))
        if context.enabled_tools.get(key)
    ]
    emit_chat_status(f"{' · '.join(active_source_labels)}에서 자료를 찾고 있습니다", "retrieval")
    limit, group_by = retrieval_limit(request, context)
    context.active_limit = limit
    context.retrieval_result = run_retrieval_pipeline(
        context.retrieval_text,
        limit,
        context.enabled_tools,
        intent_plan=context.intent_plan,
    )
    emit_chat_status("검색 결과를 질문 조건에 맞게 정리하고 있습니다", "filtering")
    deterministic = deterministic_retrieval_response(request, context, limit, group_by)
    if deterministic:
        return deterministic
    append_retrieval_system_note(context, limit)
    return None


class ChatService:
    """단일 채팅 요청의 전체 검색·답변 파이프라인을 실행한다."""

    def build_response(
        self,
        request: ChatRequest,
        user: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        user_key = str((user or {}).get("uid") or "anonymous").strip()
        with CHAT_ADMISSION.admit(user_key):
            return self._build_response(request, user=user)

    def build_admitted_response(
        self,
        request: ChatRequest,
        user: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """라우터가 admission lease를 이미 보유한 요청을 실행한다."""
        return self._build_response(request, user=user)

    def _build_response(
        self,
        request: ChatRequest,
        user: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        def finalize_chat_response(response: Dict[str, Any]) -> Dict[str, Any]:
            return persist_chat_response(
                request,
                latest_user_text,
                response,
                user_uid=str((user or {}).get("uid") or "").strip(),
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

        context = prepare_chat_pipeline(request, latest_user_text, user)
        planned_response = plan_chat_query(request, context, user)
        if planned_response:
            return finalize_chat_response(planned_response)

        retrieval_response = retrieve_chat_evidence(request, context)
        if retrieval_response:
            return finalize_chat_response(retrieval_response)

        if context.system_notes:
            context.messages.insert(
                0,
                {"role": "system", "content": "\n\n".join(context.system_notes)},
            )
        generated_response = generate_model_response(
            request,
            context.model,
            context.messages,
            context.retrieval_result,
            context.query_constraints,
            context.active_limit,
        )
        return finalize_chat_response(generated_response)

chat_service = ChatService()
