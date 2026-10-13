"""채팅 요청 오케스트레이션 서비스.

HTTP 라우팅과 분리해 질문 해석, 검색, 결정형 응답, 모델 폴백을 조정한다.
"""

import json
import os
import re
from time import perf_counter
from typing import Any, Callable, Dict, List, Optional

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
    customer_notice_instruction,
    select_grouped_issue_rows,
    should_return_deterministic_issue_list,
)
from backend.intent.request_scope import AUTHENTICATED_OWNER, authenticated_owner_scope
from backend.intent.status_semantics import (
    ALL_STATUSES, apply_status_resolution, resolution_is_grounded,
    resolve_status_intent, status_resolution_scope,
)
from backend.team_scope import has_mantis_team_scope
from backend.execution import checkpoint, request_execution
from backend.admission import CHAT_ADMISSION
from backend.audit import CHAT_STREAM_CONTEXT, emit_chat_event, emit_chat_status, load_audit_query_plan
from backend.common import clamp_int
from backend.llm import (
    build_generation_options,
    consume_model_response,
    extract_model_content,
    get_model_api_key,
    get_model_registry,
    get_private_model,
    is_ollama_model,
    post_model_chat,
)
from backend.query_planning import build_query_plan, extract_group_by
from backend.timing import TIMINGS, measured, request_timing
from backend.intent.search_terms import build_keyword_terms
from backend.intent.context import is_affirmative_read_follow_up
from backend.notion.formatting import append_notion_completeness_notice, format_notion_missing_information_answer
from backend.intent.confirmed_context import confirmed_scope_text, confirmed_constraints_scope, merge_confirmed_constraints, restore_confirmed_constraints, confirmed_keyword_text
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
from backend.services.contracts import ChatPipelineContext, DeterministicReportResult, StageResult
from backend.services.report_dispatcher import ReportRequest, dispatch_reports
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
NOTION_PERSONAL_SCOPE_PATTERN = re.compile(
    r"(?:^|[\s,])(?:"
    r"(?:내|나의|제)\s*(?:관련\s*(?:된|있는)?\s*)?(?:문서|항목|페이지|업무|자료)|"
    r"(?:나|저)(?:랑|와|과)\s*관련\s*(?:된|있는)?\s*(?:문서|항목|페이지|업무|자료)|"
    r"(?:내가|제가)\s*(?:담당|맡|작성|등록|참여|기여)[가-힣]*\s*(?:문서|항목|페이지|업무|자료)?|"
    r"(?:내게|나에게|나한테|저에게|저한테)\s*(?:할당|배정|지정|맡겨)[가-힣]*"
    r")",
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
    if re.search(r"(?:내|제)\s*(?:건|것|이슈|업무)(?:은|는)?\s*(?:말고|빼고|제외)", text):
        return False
    if any(marker in normalized for marker in ("notion", "노션", "노선")):
        return False
    constraints = extract_query_constraints(text)
    if constraints.get("mantis_team_members"):
        return False
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
    return has_personal_mantis_scope_phrase(text) or bool(re.search(r"^(?:나|저)(?:는)?\s+.*(?:뭐부터|무엇부터)", text))


def should_scope_to_authenticated_notion_user(text: str) -> bool:
    """Notion의 1인칭 관련 문서를 로그인 사용자 범위로 해석한다."""
    raw_text = str(text or "").strip()
    normalized = re.sub(r"\s+", "", raw_text).casefold()
    if not any(marker in normalized for marker in ("notion", "노션", "노선")):
        return False
    constraints = extract_query_constraints(raw_text)
    explicit_names = {
        str(name or "").strip().casefold()
        for name in constraints.get("people", {}).get("names") or []
    } - PERSONAL_SCOPE_PRONOUN_NAMES
    if explicit_names:
        return False
    return bool(NOTION_PERSONAL_SCOPE_PATTERN.search(raw_text))


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


def apply_authenticated_notion_scope(
    text: str,
    user: Optional[Dict[str, Any]],
    *,
    scope_text: Optional[str] = None,
) -> str:
    """개인 Notion 요청에 인증 계정의 표시 이름을 담당 조건으로 명시한다."""
    notion_username = str(
        (user or {}).get("displayName") or (user or {}).get("mantisUsername") or ""
    ).strip()
    if (
        not notion_username
        or "@" in notion_username
        or not should_scope_to_authenticated_notion_user(scope_text or text)
    ):
        return text
    return f"{text} 담당자: {notion_username}".strip()


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
    return dispatch_reports(
        ReportRequest(latest_user_text, retrieval_text, enabled_tools, messages, user),
        {
            "unknown_customer_guard": build_unknown_customer_report,
            "similar_resolution": build_similar_resolution_report,
            "incident_triage": build_incident_triage_report,
            "monthly_customer": build_monthly_customer_report,
            "weekly_report": build_weekly_report,
            "customer_profile": build_customer_profile_report,
            "sla": build_sla_report,
            "pivot": build_mantis_pivot_report,
            "spike": build_mantis_spike_report,
            "top_customers": build_top_customers_report,
            "effort_trend": build_effort_trend_report,
            "effort_ranking": build_effort_ranking_report,
        },
        effort_builder=build_effort_report,
        notion_writer=run_notion_write_command,
    )


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

    data = None
    final_model = model
    last_error: Optional[Exception] = None
    model_timeout = clamp_int(os.getenv("MODEL_TIMEOUT_SECONDS", "180"), 30, 300) or 180
    stream_read_timeout = min(
        model_timeout,
        clamp_int(os.getenv("MODEL_STREAM_READ_TIMEOUT_SECONDS", "60"), 10, 300) or 60,
    )
    first_content_timeout = min(
        model_timeout,
        clamp_int(os.getenv("MODEL_FIRST_CONTENT_TIMEOUT_SECONDS", "60"), 10, 300) or 60,
    )
    emit_chat_status("답변을 생성하고 있습니다", "generation")

    for candidate in candidate_models:
        checkpoint()
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
            first_content_deadline = perf_counter() + first_content_timeout if request.stream else None
            candidate_response = post_model_chat(
                candidate,
                candidate_api_key,
                candidate_payload,
                timeout=stream_read_timeout if request.stream else model_timeout,
                stream=bool(request.stream),
            )
            # HTTP 200 뒤에 발생하는 본문 읽기 오류도 같은 후보 폴백 범위에서
            # 처리한다. 본문 소비가 끝나야 회로 성공 여부를 확정하고 연결을 닫는다.
            with consume_model_response(candidate_response):
                if candidate_response.status_code >= 400:
                    error_text = candidate_response.text
                    unsupported_hint = ""
                    if any(keyword in error_text.lower() for keyword in (
                        "extra", "unknown", "unsupported", "unexpected", "not permitted",
                    )):
                        unsupported_hint = " 선택한 생성 옵션 중 회사 모델 서버가 지원하지 않는 값이 있을 수 있습니다. 해당 옵션을 비우고 다시 시도해 주세요."
                    raise HTTPException(
                        status_code=candidate_response.status_code,
                        detail=error_text[:1000] + unsupported_hint,
                    )
                data = _read_model_response(
                    candidate_response, candidate, request.stream,
                    first_content_deadline=first_content_deadline,
                )
                if not extract_model_content(data).strip():
                    raise requests.ConnectionError("모델 응답에 답변 본문이 없습니다.")
        except (requests.RequestException, HTTPException) as exc:
            checkpoint()
            last_error = exc
            # 실패한 후보의 부분 답변과 다음 후보/근거 답변을 이어 붙이지 않는다.
            if getattr(CHAT_STREAM_CONTEXT, "content_streamed", False):
                emit_chat_event("reset")
                CHAT_STREAM_CONTEXT.content_streamed = False
            emit_chat_status("응답 연결을 다시 확인하고 있습니다", "generation")
            data = None
            continue
        break

    if data is None:
        if retrieval_result and isinstance(last_error, requests.RequestException):
            fallback_sources = build_sources(retrieval_result, max_sources=active_limit)
            notion_pages = collect_retrieved_notion_pages(retrieval_result, active_limit)
            issues = collect_retrieved_issues(retrieval_result, active_limit)
            if notion_pages and issues:
                fallback_content = format_deterministic_combined_answer(fallback_sources)
            elif not issues and any(stage.get("source") == "notion" for stage in retrieval_result.get("stages", [])):
                fallback_content = format_deterministic_notion_answer(
                    str(retrieval_result.get("query") or ""), retrieval_result, active_limit,
                )
            else:
                fallback_content = format_model_timeout_fallback(query_constraints, retrieval_result, active_limit)
            if notion_pages:
                fallback_content = "모델 분석 응답이 지연되어 확인한 자료를 안내합니다.\n\n" + fallback_content
            return {
                "modelId": final_model["id"],
                "content": append_notion_completeness_notice(fallback_content, retrieval_result),
                "sources": fallback_sources if request.showSources else [],
                "sourcesForFollowUps": fallback_sources,
                "retrieval": retrieval_result,
                "raw": {
                    "mode": "model_timeout_fallback",
                    "error": str(last_error),
                    "errorType": type(last_error).__name__,
                },
            }
        if isinstance(last_error, HTTPException):
            raise last_error
        if last_error is None:
            raise HTTPException(status_code=503, detail="답변을 생성할 모델의 API 키가 설정되지 않았습니다.")
        raise HTTPException(status_code=502, detail=f"Model connection failed: {last_error}")

    final_sources = build_sources(retrieval_result, max_sources=active_limit)
    content = sanitize_customer_notice_answer(
        messages[-1].get("content", "") if messages else "",
        extract_model_content(data),
    )
    return {
        "modelId": final_model["id"],
        # 모델 스트림도 최종 검증 전까지 버퍼링되므로 저장·검증·전송에
        # 동일한 안내가 포함된다. 결정형 목록은 이 모델 경로를 거치지 않는다.
        "content": append_notion_completeness_notice(content or "응답 본문을 찾지 못했습니다.", retrieval_result),
        "sources": final_sources if request.showSources else [],
        "sourcesForFollowUps": final_sources,
        "retrieval": retrieval_result,
        "raw": data,
        "usedFallbackModel": final_model["id"] if final_model["id"] != model["id"] else None,
    }


def _read_model_response(
    response: requests.Response, model: Dict[str, Any], stream: bool,
    *, first_content_deadline: Optional[float] = None,
) -> Dict[str, Any]:
    has_content = False

    def lines():
        for raw_line in response.iter_lines(decode_unicode=True):
            checkpoint()
            # 역할/추론 청크만 계속 보내는 연결도 무한히 첫 답변을 미루지 않게 한다.
            # 블로킹된 읽기는 별도 stream read timeout으로 끝난다.
            if not has_content and first_content_deadline is not None and perf_counter() >= first_content_deadline:
                raise requests.Timeout("모델의 첫 답변 대기 시간을 초과했습니다.")
            yield (raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes)
                   else str(raw_line or "")).strip()

    if stream and is_ollama_model(model):
        streamed_content = ""
        ollama_stream_chunks: List[Dict[str, Any]] = []
        for line in lines():
            if not line:
                continue
            try:
                chunk = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            ollama_stream_chunks.append(chunk)
            message = chunk.get("message")
            delta = message.get("content") if isinstance(message, dict) else None
            if isinstance(delta, str) and delta:
                has_content = has_content or bool(delta.strip())
                streamed_content += delta
                # 본문은 최종 검증 전까지 보내지 않고 후보별로 보관한다.
        data = {
            "choices": [{"message": {"content": streamed_content}}],
            "streamChunks": len(ollama_stream_chunks),
            "provider": "ollama",
        }
    elif stream and "text/event-stream" in response.headers.get("content-type", "").lower():
        streamed_content = ""
        sse_stream_chunks: List[Dict[str, Any]] = []
        for line in lines():
            if not line or not line.startswith("data:"):
                continue
            body = line[5:].strip()
            if body == "[DONE]":
                break
            try:
                chunk = json.loads(body)
            except json.JSONDecodeError:
                continue
            if not isinstance(chunk, dict):
                continue
            sse_stream_chunks.append(chunk)
            choices = chunk.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else None
            delta_message = choice.get("delta") if isinstance(choice, dict) else None
            delta = delta_message.get("content") if isinstance(delta_message, dict) else None
            if isinstance(delta, str) and delta:
                has_content = has_content or bool(delta.strip())
                streamed_content += delta
                # 본문은 최종 검증 전까지 보내지 않고 후보별로 보관한다.
        data = {
            "choices": [{"message": {"content": streamed_content}}],
            "streamChunks": len(sse_stream_chunks),
        }
    else:
        data = response.json()

    checkpoint()
    return data


@measured("context")
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
    messages = [message.model_dump(exclude={"notionActionId"}) for message in request.messages]
    history_limit = clamp_int(os.getenv("MODEL_HISTORY_MAX_MESSAGES", "20"), 2, 200) or 20
    if len(messages) > history_limit:
        messages = messages[-history_limit:]

    retrieval_text, needs_follow_up_rewrite = build_contextual_retrieval_text(request.messages)
    inherited_resolution = None
    inherited_constraints = None
    latest_constraints = extract_query_constraints(latest_user_text)
    lookup_reply = bool(latest_constraints.get("customer_key") and
                        re.fullmatch(r"[가-힣A-Za-z0-9]+(?:\s*(?:쪽|이야|요))?[.?!]?", latest_user_text.strip()))
    # 확인 질문 직후의 짧은 상태 선택은 서버에 저장된 해당 턴에만 연결한다.
    status_choice = bool(re.fullmatch(
        r"\s*(?:미종료|전체\s*상태|모든\s*상태|상태\s*무관)(?:로|만|으로)?"
        r"\s*(?:보여줘|조회해줘|알려줘)?[.!?]?\s*", latest_user_text,
    ))
    prior_questions = get_recent_user_texts(request.messages, limit=2)
    prior_plan = load_audit_query_plan(
        request.contextAuditId or "", str((user or {}).get("uid") or ""),
        prior_questions[1] if len(prior_questions) > 1 else "",
    ) if (status_choice or lookup_reply or is_follow_up_question(latest_user_text)) else {}
    prior_resolution = prior_plan.get("status_resolution") or {}
    lookup_continuation = lookup_reply and bool((prior_plan.get("resolved_constraints") or {}).get("work_lookup"))
    if (prior_plan.get("source") == "mantis" and prior_plan.get("resolved_constraints")
            and (lookup_continuation or is_follow_up_question(latest_user_text) or (status_choice and prior_resolution.get("needs_clarification")))):
        # 이전 자연어를 다시 추측하지 않고 서버가 확정한 범위를 복원한다.
        inherited_constraints = restore_confirmed_constraints(prior_plan)
        prior_text = confirmed_scope_text(inherited_constraints)
        scope_messages = [request.messages[-1].model_copy(update={"content": prior_text}),
                          request.messages[-1].model_copy(update={"content": latest_user_text if is_follow_up_question(latest_user_text) else f"그중 {latest_user_text}"})]
        retrieval_text, needs_follow_up_rewrite = build_contextual_retrieval_text(scope_messages)
        retrieval_text = "멘티스 " + retrieval_text
        # 문장은 라우팅/표시에만 사용하고 실행 조건은 아래에서 구조적으로 병합한다.
        needs_follow_up_rewrite = False
    elif status_choice and prior_resolution.get("needs_clarification"):
        follow_up_messages = [*request.messages[:-1], request.messages[-1].model_copy(
            update={"content": f"그중 {latest_user_text}"},
        )]
        retrieval_text, needs_follow_up_rewrite = build_contextual_retrieval_text(follow_up_messages)
    if (is_follow_up_question(latest_user_text) and not latest_constraints.get("statuses")
            and not latest_constraints.get("excluded_statuses") and not ALL_STATUSES.search(latest_user_text)
            and not latest_constraints.get("issue_ids")):
        if prior_plan.get("source") == "mantis" and resolution_is_grounded(prior_resolution):
            inherited_resolution = {**prior_resolution, "origin": "inherited",
                "parent_grounding_text": prior_resolution.get("parent_grounding_text") or prior_resolution.get("grounding_text"),
                "parent_audit_id": request.contextAuditId}
            needs_follow_up_rewrite = False
    team_scope = has_mantis_team_scope(retrieval_text)
    resolved_reference = resolve_reference_from_context(
        latest_user_text,
        request.contextAuditId,
        user_uid=str((user or {}).get("uid") or "").strip(),
    )
    if resolved_reference:
        retrieval_text = resolved_reference
        needs_follow_up_rewrite = False
        inherited_resolution = None
        inherited_constraints = None
    elif (is_affirmative_read_follow_up(latest_user_text)
          and prior_plan.get("source") == "notion" and prior_plan.get("grounding_text")):
        # 직전 서버 검색 입력에는 이미 후속 턴에서 바꾼 조건이 반영되어 있다.
        retrieval_text = f"노션 {prior_plan['grounding_text']} 상세 내용 알려줘"
        needs_follow_up_rewrite = False
    else:
        notion_prefix = inherit_notion_context(
            get_recent_user_texts(request.messages, limit=5),
            prior_source=str(prior_plan.get("source") or ""),
        )
        if notion_prefix and not team_scope and inherited_constraints is None:
            detail = "상세 내용 알려줘" if is_affirmative_read_follow_up(latest_user_text) else latest_user_text
            retrieval_text = f"{notion_prefix} {detail}".strip()
            needs_follow_up_rewrite = False

    if needs_follow_up_rewrite and api_key:
        rewritten = rewrite_follow_up_query_with_model(
            get_recent_user_texts(request.messages, limit=5),
            model,
            api_key,
        )
        if rewritten:
            retrieval_text = rewritten

    retrieval_text = retrieval_text or latest_user_text
    if team_scope and not has_mantis_team_scope(retrieval_text):
        retrieval_text = f"우리 팀 {retrieval_text}"
    scope_text = retrieval_text
    if is_follow_up_question(latest_user_text):
        scope_text = " ".join(get_recent_user_texts(request.messages, limit=5))
    mantis_personal_scope = should_scope_to_authenticated_mantis_user(scope_text)
    notion_personal_scope = should_scope_to_authenticated_notion_user(scope_text)
    if mantis_personal_scope:
        owner = str(
            (user or {}).get("mantisUsername") or (user or {}).get("displayName") or ""
        ).strip()
    elif notion_personal_scope:
        owner = str(
            (user or {}).get("displayName") or (user or {}).get("mantisUsername") or ""
        ).strip()
        if "@" in owner:
            owner = ""
    else:
        owner = ""
    retrieval_text = apply_authenticated_mantis_scope(
        retrieval_text,
        user,
        scope_text=scope_text,
    )
    retrieval_text = apply_authenticated_notion_scope(
        retrieval_text,
        user,
        scope_text=scope_text,
    )
    with authenticated_owner_scope(owner):
        scoped_constraints = extract_query_constraints(retrieval_text) if retrieval_text else {}
        if inherited_constraints is not None:
            scoped_constraints = merge_confirmed_constraints(
                inherited_constraints, extract_query_constraints(latest_user_text), latest_user_text,
            )
            scoped_constraints["inherited_from_audit_id"] = request.contextAuditId
            keyword_text = confirmed_keyword_text(scoped_constraints)
            if keyword_text:
                retrieval_text = f"{retrieval_text} {keyword_text}"
    if inherited_resolution:
        inherited_resolution["grounding_text"] = retrieval_text
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
        status_resolution=inherited_resolution,
    )


@measured("planning")
def plan_chat_query(request: ChatRequest, context: ChatPipelineContext, user: Optional[Dict[str, Any]]) -> StageResult:
    return StageResult.from_response(_resolve_chat_plan(request, context, user))


def _resolve_chat_plan(request: ChatRequest, context: ChatPipelineContext, user: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
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
    if context.enabled_tools.get("mantis"):
        from backend.reports.work_queries import build_work_query_report
        work_report = build_work_query_report(context.latest_user_text, context.query_constraints,
                                              limit=extract_requested_limit(context.latest_user_text) or clamp_int(request.searchLimit, 1, 20) or 10)
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
    from backend.intent.status_semantics import is_structured_status_neutral_query
    if is_structured_status_neutral_query(text, constraints):
        return True
    if re.search(r"비슷|유사|원인|분석|비교|추이|예측|관련|방법|해결책", text):
        return False
    from backend.query_planning import infer_operation
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
    if (context.intent_plan or {}).get("source") == "notion":
        notion_pages = collect_retrieved_notion_pages(retrieval_result, limit)
        missing_information = format_notion_missing_information_answer(context.retrieval_text, notion_pages)
        if missing_information:
            notion_sources = build_sources_from_notion_pages(notion_pages)
            return {
                "modelId": request.modelId, "content": missing_information,
                "sources": notion_sources if request.showSources else [],
                "sourcesForFollowUps": notion_sources, "retrieval": retrieval_result,
                "raw": {"mode": "deterministic_notion_missing_field"},
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
        "첨부파일이나 이미지의 실제 내용은 이 JSON에 명시된 텍스트가 없으면 확인하지 못한 것이다. "
        "'타 증권사와 비교한 이미지'라는 설명만으로 타사의 정상 동작이나 비교 결과를 단정하지 마라. "
        "증상, 원인, 조치 완료 여부와 일정은 각각 원문에 직접 적힌 사실만 요약하고, 없는 내용은 추측해 채우지 마라. "
        "원문의 권고·가능성·의무 수준을 유지해라. '지양'·'권장'을 '금지'·'필수'로 강화하지 마라. "
        "Notion 코드·표를 인용할 때 주석·식별자·셀 값을 추가하거나 바꾸지 마라. "
        "해설은 인용 밖에 쓰고 식별자의 약어나 업무상 의미가 원문에 정의되어 있지 않으면 이름만으로 확정하지 마라. "
        "Notion 공지의 일시·장소가 빈칸이면 '원문에 확정 일시·장소가 미기재'라고 답하고, 투표 후보를 확정 장소로 바꾸지 마라. "
        "미기재 또는 본문 일부 미확인은 아직 확정되지 않았다는 근거가 아니다. 확정 여부 자체를 단정하지 마라. "
        "Notion 속성의 목표일·기한·등록일은 그 이름을 유지하고, 행사일이나 최종 결정일로 바꾸지 마라. "
        "날짜의 요일은 원문에 적혀 있지 않으면 덧붙이지 마라. "
        "이전 답변에 나온 날짜·장소·안내도 이번 검색 원문에서 확인되지 않으면 재사용하지 마라. "
        "일반 분석 답변은 결론을 먼저 1~2문장으로 말하고, 주요 근거와 필요한 다음 행동을 이어서 정리해라. "
        "사용자가 요청한 보고서·안내문·인수인계 형식이 있으면 그 형식을 우선해라. "
        "검색 파이프라인은 Notion DB 조회, Mantis 저장 필터, Mantis 페이지 스캔, ChromaDB 검색으로 구성된다. "
        "Notion 결과는 팀 주요 이슈/공지/일정/결재 상태 조회에 사용하고, Mantis 결과는 원본 이슈 목록 조회에 우선 사용하며, ChromaDB 결과는 유사 사례나 보조 맥락으로 사용해라. "
        "ChromaDB 문서에 latest_issue가 있으면 ChromaDB 스냅샷 대신 latest_issue 값을 최신 Mantis 원본으로 사용해라. "
        "다만 Mantis 결과가 요청 개수보다 부족하고 ChromaDB 결과가 질문의 고객사/담당자/키워드 조건과 일치하면, ChromaDB 결과도 실제 이슈 목록으로 포함해라. "
        "질문에 고객사, 담당자, HTS, MTS 같은 조건이 있으면 그 조건과 일치하는 이슈만 답변에 포함해라. "
        "예를 들어 HTS 질문에는 제목에 MTS가 있더라도 본문이나 제목에 HTS가 함께 있을 때만 포함하고, MTS 전용 이슈는 제외해라. "
        "실시간 조회를 할 수 없다고 말하지 마라. "
        "사용자가 검색 과정이나 기준을 묻지 않는 한 mantis_filter, mantis_page_scan, chroma_db 같은 내부 단계명이나 스킵 여부는 말하지 마라. "
        "대신 필요한 경우 '검색 기준: 고객사/담당자/상태/키워드, 정렬: 최신 이슈 ID 기준'처럼 사용자에게 의미 있는 기준만 짧게 말해라. "
        "사용자가 이슈 목록을 요청하면 번호 목록으로 ID, 프로젝트, 제목, 상태, 담당자, 등록일을 포함해 요약해라. "
        "이슈 번호(#숫자)는 결과 JSON의 issue_id에 있는 값만 써라. 제목·본문의 [0389]처럼 대괄호 안 숫자는 화면번호이므로 "
        "#을 붙여 이슈 번호처럼 쓰지 말고 '화면 0389'처럼 써라.\n\n"
        f"[검색 파이프라인 결과]\n{json.dumps(model_retrieval_context, ensure_ascii=False, indent=2)}"
    )
    notice_instruction = customer_notice_instruction(context.latest_user_text)
    if notice_instruction:
        context.system_notes.append(notice_instruction)


@measured("retrieval")
def retrieve_chat_evidence(request: ChatRequest, context: ChatPipelineContext) -> StageResult:
    return StageResult.from_response(_retrieve_chat_evidence(request, context))


def _retrieve_chat_evidence(
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
    grounding = build_model_retrieval_context(context.retrieval_result, limit)
    if not grounding.get("issues") and not grounding.get("notion_pages"):
        notion_only = (context.intent_plan or {}).get("source") == "notion" or (
            bool((context.retrieval_result or {}).get("stages"))
            and all(stage.get("source") == "notion" for stage in context.retrieval_result["stages"])
        )
        guidance = (
            "조회한 범위에서 문서를 찾지 못했습니다. 문서 제목이나 검색어를 더 구체적으로 지정하거나 연결된 노션 범위를 확인해 주세요."
            if notion_only else "전체가 0건이거나 모두 완료됐다는 뜻은 아닙니다. 고객사·담당자·이슈 번호를 확인해 주세요."
        )
        return {"modelId": request.modelId,
                "content": f"질문 조건에 맞는 원문 근거를 확보하지 못했습니다. {guidance}",
                "sources": [], "retrieval": context.retrieval_result, "raw": {"mode": "empty_evidence"}}
    append_retrieval_system_note(context, limit)
    return None


def attach_status_resolution(response: Dict[str, Any], context: ChatPipelineContext) -> Dict[str, Any]:
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
        with CHAT_ADMISSION.admit(user_key):
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
        def finalize_chat_response(response: Dict[str, Any]) -> Dict[str, Any]:
            response["timings"] = {"stages": dict(TIMINGS.get() or {}),
                                   "beforeFinalizationMs": round((perf_counter() - started) * 1000, 2)}
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
        with confirmed_constraints_scope(context.retrieval_text, context.query_constraints):
            return self._complete_response(request, context, user, finalize_chat_response)

    def _complete_response(
        self,
        request: ChatRequest,
        context: ChatPipelineContext,
        user: Optional[Dict[str, Any]],
        finalize_chat_response: Callable[[Dict[str, Any]], Dict[str, Any]],
    ) -> Dict[str, Any]:
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
        generated_response = measured("generation")(generate_model_response)(
            request,
            context.model,
            context.messages,
            context.retrieval_result,
            context.query_constraints,
            context.active_limit,
        )
        generated = StageResult.from_response(generated_response)
        assert generated.response is not None
        return finalize_chat_response(attach_status_resolution(generated.response, context))

chat_service = ChatService()
