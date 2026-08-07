"""채팅 요청 오케스트레이션 서비스.

HTTP 라우팅과 분리해 질문 해석, 검색, 결정형 응답, 모델 폴백을 조정한다.
"""

import json
import os
import re
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException

from backend.answers import (
    CONVERSATION_TONE_NOTE,
    answer_style_instruction,
    build_current_date_note,
    build_mantis_count_report,
    build_mantis_pivot_report,
    build_mantis_spike_report,
    build_top_customers_report,
    build_unknown_customer_report,
    format_deterministic_combined_answer,
    format_deterministic_issue_answer,
    format_model_timeout_fallback,
    select_grouped_issue_rows,
    should_return_deterministic_issue_list,
)
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
from backend.reports.effort import (
    build_effort_ranking_report,
    build_effort_report,
    build_effort_report_from_plan,
)
from backend.reports.evaluation import finalize_chat_response
from backend.reports.similar import build_similar_resolution_report
from backend.reports.sla import build_sla_report
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
    extract_notion_constraints,
    format_deterministic_notion_answer,
    inherit_notion_context,
    run_notion_write_command,
    should_return_deterministic_notion_list,
)
from backend.understanding import (
    build_contextual_retrieval_text,
    extract_query_constraints,
    get_latest_user_text,
    get_recent_user_texts,
    merge_query_plan_constraints,
    plan_search_intent_with_model,
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


def should_scope_to_authenticated_mantis_user(text: str) -> bool:
    """개인 업무 요청인지 판정해 로그인 사용자의 Mantis 담당 범위를 적용한다."""
    normalized = re.sub(r"\s+", "", str(text or "")).casefold()
    if any(marker in normalized for marker in ("notion", "노션", "노선")):
        return False
    constraints = extract_query_constraints(text)
    if constraints.get("people", {}).get("names"):
        return False
    if any(
        marker in normalized
        for marker in ("업무보고서", "주간보고서", "주간업무보고", "주간업무")
    ):
        return True
    return any(
        marker in normalized
        for marker in ("내담당", "내이슈", "나의이슈", "내업무", "내가맡은")
    )


def apply_authenticated_mantis_scope(
    text: str,
    user: Optional[Dict[str, Any]],
) -> str:
    """개인 Mantis 요청에 인증 계정의 담당자명을 검색 조건으로 명시한다."""
    mantis_username = str((user or {}).get("mantisUsername") or "").strip()
    if not mantis_username or not should_scope_to_authenticated_mantis_user(text):
        return text
    return f"{text} 담당자: {mantis_username}".strip()


class ChatService:
    """단일 채팅 요청의 전체 검색·답변 파이프라인을 실행한다."""

    def build_response(
        self,
        request: ChatRequest,
        user: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        emit_chat_status("질문과 대화 맥락을 분석하고 있습니다", "analysis")
        latest_user_text = get_latest_user_text(request.messages)
        if is_identity_question(latest_user_text):
            return finalize_chat_response(
                request,
                latest_user_text,
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
                },
            )
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

        messages = [message.model_dump() for message in request.messages]
        # 대화가 길어져도 모델에 보내는 히스토리는 최근 N개로 제한한다(속도·컨텍스트 한도 보호).
        # 검색 라우팅은 request.messages 전체(최근 5턴)를 별도로 참조하므로 영향받지 않는다.
        history_limit = clamp_int(os.getenv("MODEL_HISTORY_MAX_MESSAGES", "20"), 2, 200) or 20
        if len(messages) > history_limit:
            messages = messages[-history_limit:]
        # 후속 질문이면 직전 대화의 조건을 물려받은 라우팅용 텍스트. 검색/조건 추출에만 쓰고
        # 감사 로그·중복 판정·Notion 쓰기는 사용자가 실제 입력한 latest_user_text를 그대로 쓴다.
        retrieval_text, needs_follow_up_rewrite = build_contextual_retrieval_text(request.messages)
        # "그중 3번째" 같은 서수 참조는 직전 답변이 실제로 보여준 결과 목록(감사 로그)에서
        # 해당 항목을 직접 찾는다. 재검색 결과가 달라져 다른 항목을 가리키는 문제가 없고
        # LLM 재작성보다 빠르고 정확하므로 최우선으로 시도한다.
        resolved_reference = resolve_reference_from_context(latest_user_text, request.contextAuditId)
        if resolved_reference:
            retrieval_text = resolved_reference
            needs_follow_up_rewrite = False
        if not resolved_reference:
            # Mantis 조건 상속은 Notion 상태·DB를 물려주지 않아, 노션 후속 질문이
            # 소스와 상태를 모두 잃고 모델 재작성으로 엉뚱한 상태가 채워졌다.
            notion_prefix = inherit_notion_context(get_recent_user_texts(request.messages, limit=5))
            if notion_prefix and not extract_notion_constraints(retrieval_text).get("status"):
                retrieval_text = f"{notion_prefix} {retrieval_text or latest_user_text}".strip()
                needs_follow_up_rewrite = False
        if needs_follow_up_rewrite:
            # 규칙 기반 마커/상속으로 대상을 못 찾은 후속 질문만 모델에게 재작성시킨다.
            # 실패하면 원문 그대로 쓰던 기존 동작으로 조용히 폴백한다.
            rewritten = rewrite_follow_up_query_with_model(
                get_recent_user_texts(request.messages, limit=5),
                model,
                api_key,
            )
            if rewritten:
                retrieval_text = rewritten
        retrieval_text = retrieval_text or latest_user_text
        retrieval_text = apply_authenticated_mantis_scope(retrieval_text, user)
        system_notes = []
        retrieval_result = None
        intent_plan = None
        query_constraints = extract_query_constraints(retrieval_text) if retrieval_text else {}
        # Notion은 화면의 기본 소스 선택에서는 숨겨두지만, 노션을 명시한 질문은
        # 실제 연결된 DB에서 조회할 수 있어야 한다. 클라이언트가 false를 보내면
        # 기존처럼 해당 요청에서만 비활성화한다.
        enabled_tools = {"mantis": True, "vectorDb": True, "notion": True, **request.tools}
        system_notes.append(answer_style_instruction(request.answerStyle))
        system_notes.append(CONVERSATION_TONE_NOTE)
        system_notes.append(build_current_date_note())
        active_limit = clamp_int(request.searchLimit, 1, 20) or 5

        # 공수 질문 빠른 경로: 규칙(정규식+known_people)이 확신하는 경우 LLM 없이 바로
        # 결정형 계산으로 응답한다. 규칙이 인물/의도를 못 잡으면(pending) 바로 에러를
        # 돌려주지 않고, 아래 LLM 플래너에게 분류를 한 번 더 맡긴 뒤 그래도 안 되면
        # 보류해둔 안내 문구를 반환한다.
        # 등록되지 않은 고객사를 물으면 조건 없는 전체 검색으로 흘러가 엉뚱한
        # 고객사 이슈가 답이 된다. 검색 전에 그 사실을 그대로 알린다.
        if latest_user_text and enabled_tools.get("mantis"):
            unknown_customer_report = build_unknown_customer_report(retrieval_text or latest_user_text)
            if unknown_customer_report:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **unknown_customer_report},
                )

        # "#12345랑 비슷한 이슈 어떻게 해결했어?"처럼 기준 이슈 + 유사 + 해결 의도가
        # 모두 있으면 유사 사례와 그때의 조치를 함께 정리한다. 기준 이슈 번호가
        # 필요한 가장 구체적인 의도라 먼저 처리한다.
        if latest_user_text and enabled_tools.get("mantis") and enabled_tools.get("vectorDb"):
            similar_resolution_report = build_similar_resolution_report(latest_user_text)
            if similar_resolution_report:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **similar_resolution_report},
                )

        # "처리시간 얼마나 걸려", "오래 방치된 이슈"처럼 시간 성과를 묻는 질문은
        # 라이브 이슈의 등록/최종수정/상태로 리드타임·적체를 결정형으로 계산한다.
        if latest_user_text and enabled_tools.get("mantis"):
            sla_report = build_sla_report(latest_user_text)
            if sla_report:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **sla_report},
                )

        # "고객사별 월별"처럼 축과 월 두 축을 함께 지정한 요청은 피벗 표로 답한다.
        # 단일 축 집계(count_report)나 급증보다 구체적인 의도라 가장 먼저 처리한다.
        if latest_user_text and enabled_tools.get("mantis"):
            pivot_report = build_mantis_pivot_report(latest_user_text)
            if pivot_report:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **pivot_report},
                )

        # "평소보다 급증한 고객사/화면번호"처럼 시점 변화를 묻는 질문은 '많은 순'이
        # 아니라 최근 월을 직전 평균과 비교한 '튄 순'으로 답한다. '많은 순'을 답하는
        # top_customers보다 구체적인 의도라 먼저 처리한다.
        if latest_user_text and enabled_tools.get("mantis"):
            spike_report = build_mantis_spike_report(latest_user_text)
            if spike_report:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **spike_report},
                )

        # "문제 많은 고객사"처럼 기준이 빠진 질문은 임의 기준으로 답하지 말고
        # 무엇을 세었는지 밝히면서 답한다.
        if latest_user_text and enabled_tools.get("mantis"):
            top_customers_report = build_top_customers_report(latest_user_text)
            if top_customers_report:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **top_customers_report},
                )

        pending_effort_report = None
        if latest_user_text and enabled_tools.get("mantis"):
            ranking_report = build_effort_ranking_report(latest_user_text)
            if ranking_report:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **ranking_report},
                )
            effort_report = build_effort_report(latest_user_text)
            if effort_report:
                if effort_report.pop("needsPerson", False):
                    pending_effort_report = effort_report
                else:
                    return finalize_chat_response(
                        request,
                        latest_user_text,
                        {"modelId": request.modelId, **effort_report},
                    )

        if latest_user_text and enabled_tools.get("notion"):
            notion_write_result = run_notion_write_command(latest_user_text, request.messages)
            if notion_write_result:
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **notion_write_result},
                )

        if retrieval_text:
            rule_plan = rule_based_search_intent(retrieval_text, query_constraints)
            model_plan: Dict[str, Any] = {}
            planner_enabled = os.getenv("QUERY_PLANNER_ENABLED", "true").strip().lower() not in {"0", "false", "off"}
            should_plan = (
                planner_enabled
                and any(enabled_tools.get(key) for key in ("mantis", "notion", "vectorDb"))
                and (
                    rule_plan.get("source") in {"mantis", "notion", "both"}
                    or should_run_retrieval(retrieval_text, query_constraints)
                )
            )
            if should_plan:
                emit_chat_status("질문을 도구 실행 계획으로 변환하고 있습니다", "routing")
                model_plan = plan_search_intent_with_model(retrieval_text, model, api_key)
                if rule_plan.get("confidence", 0) >= 0.9 and rule_plan.get("source") != "general":
                    model_plan["source"] = rule_plan["source"]
            query_plan = build_query_plan(
                retrieval_text,
                rule_plan,
                query_constraints,
                model_plan if model_plan.get("planned") else None,
            )
            query_constraints = merge_query_plan_constraints(query_constraints, query_plan)
            intent_plan = {
                **query_plan,
                "planned": True,
                "ruleFallback": rule_plan,
            }

            if pending_effort_report is not None and enabled_tools.get("mantis"):
                effort_fallback = build_effort_report_from_plan(retrieval_text, model_plan)
                if effort_fallback:
                    return finalize_chat_response(
                        request,
                        latest_user_text,
                        {"modelId": request.modelId, **effort_fallback},
                    )
                # 플래너도 공수 의도를 특정하지 못했다. 규칙 경로가 보류해둔
                # "인물 이름을 입력해 달라"는 안내를 그대로 반환한다.
                return finalize_chat_response(
                    request,
                    latest_user_text,
                    {"modelId": request.modelId, **pending_effort_report},
                )

            if enabled_tools.get("mantis"):
                count_report = build_mantis_count_report(retrieval_text, query_plan)
                if count_report:
                    return finalize_chat_response(
                        request,
                        latest_user_text,
                        {"modelId": request.modelId, **count_report},
                    )

        if (
            retrieval_text
            and (enabled_tools.get("mantis") or enabled_tools.get("vectorDb") or enabled_tools.get("notion"))
            and (
                (intent_plan or {}).get("source") in {"notion", "mantis", "both"}
                or should_run_retrieval(retrieval_text, query_constraints)
            )
        ):
            active_source_labels = [
                label
                for key, label in (("mantis", "Mantis"), ("notion", "Notion"), ("vectorDb", "Vector DB"))
                if enabled_tools.get(key)
            ]
            emit_chat_status(f"{' · '.join(active_source_labels)}에서 자료를 찾고 있습니다", "retrieval")
            requested_limit = (intent_plan or {}).get("limit")
            sidebar_limit = clamp_int(request.searchLimit, 1, 20)
            group_by = (intent_plan or {}).get("group_by") or extract_group_by(retrieval_text)
            # 고객사별 최신 1건은 "하나씩"을 전체 1건 제한으로 해석하지 않고,
            # 충분한 최근 이슈를 조회한 뒤 고객사마다 한 건씩 선택한다.
            if group_by == "customer":
                limit = 800
            elif group_by:
                limit = requested_limit or 200
            else:
                limit = requested_limit or resolve_result_limit(latest_user_text, sidebar_limit)
            active_limit = limit
            retrieval_result = run_retrieval_pipeline(
                retrieval_text,
                limit,
                enabled_tools,
                intent_plan=intent_plan,
            )
            emit_chat_status("검색 결과를 질문 조건에 맞게 정리하고 있습니다", "filtering")
            if (intent_plan or {}).get("source") == "both":
                combined_sources = build_sources(retrieval_result, max_sources=limit)
                if combined_sources:
                    return finalize_chat_response(request, latest_user_text, {
                        "modelId": request.modelId,
                        "content": format_deterministic_combined_answer(combined_sources),
                        "sources": combined_sources if request.showSources else [],
                        "sourcesForFollowUps": combined_sources,
                        "retrieval": retrieval_result,
                        "raw": {"mode": "deterministic_combined_search"},
                    })
            if should_return_deterministic_notion_list(retrieval_text, retrieval_result):
                notion_pages = collect_retrieved_notion_pages(retrieval_result, limit)
                notion_sources = build_sources_from_notion_pages(notion_pages)
                return finalize_chat_response(request, latest_user_text, {
                    "modelId": request.modelId,
                    "content": format_deterministic_notion_answer(
                        retrieval_text,
                        retrieval_result,
                        limit,
                    ),
                    "sources": notion_sources if request.showSources else [],
                    "sourcesForFollowUps": notion_sources,
                    "retrieval": retrieval_result,
                    "raw": {"mode": "deterministic_notion_list"},
                })
            if should_return_deterministic_issue_list(retrieval_text, query_constraints):
                deterministic_issues = select_grouped_issue_rows(
                    collect_retrieved_issues(retrieval_result, limit),
                    group_by,
                )
                issue_sources = build_sources_from_issue_rows(deterministic_issues)
                return finalize_chat_response(request, latest_user_text, {
                    "modelId": request.modelId,
                    "content": format_deterministic_issue_answer(
                        retrieval_text,
                        query_constraints,
                        retrieval_result,
                        limit,
                        request.answerStyle,
                    ),
                    "sources": issue_sources if request.showSources else [],
                    "sourcesForFollowUps": issue_sources,
                    "retrieval": retrieval_result,
                    "raw": {"mode": "deterministic_issue_list"},
                })
            model_retrieval_context = build_model_retrieval_context(retrieval_result, limit)
            system_notes.append(
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
        else:
            system_notes.append(
                "너는 컨텐츠서비스 AI 비서다. 사용자가 일반 대화나 모델/사용법을 묻는 경우에는 Mantis나 ChromaDB 이슈를 언급하지 말고 직접 답하라. "
                "사용자가 고객사 이슈, 담당자, 화면번호, HTS/MTS, Mantis 조회처럼 사내 데이터 검색을 명확히 요청할 때만 이슈 조회가 필요하다고 이해하라."
            )

        if system_notes:
            messages.insert(0, {"role": "system", "content": "\n\n".join(system_notes)})

        # 1차 모델이 타임아웃/오류일 때 시도할 대체 모델 목록. models.json에 활성 모델이
        # 하나뿐이면(현재 기본 상태) 이 목록은 [model] 하나뿐이라 동작은 기존과 동일하다.
        candidate_models = [model] + [
            other
            for other in get_model_registry()
            if other["id"] != model["id"] and other.get("status") == "active"
        ]

        response = None
        final_model = model
        timeout_exc: Optional[requests.ReadTimeout] = None
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

            try:
                model_timeout = clamp_int(os.getenv("MODEL_TIMEOUT_SECONDS", "180"), 30, 300) or 180
                candidate_response = post_model_chat(
                    candidate,
                    candidate_api_key,
                    candidate_payload,
                    timeout=model_timeout,
                    stream=bool(request.stream),
                )
            except requests.ReadTimeout as exc:
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
            if timeout_exc is not None:
                if retrieval_result:
                    fallback_sources = build_sources(retrieval_result, max_sources=active_limit)
                    return finalize_chat_response(request, latest_user_text, {
                        "modelId": final_model["id"],
                        "content": format_model_timeout_fallback(
                            query_constraints,
                            retrieval_result,
                            active_limit,
                        ),
                        "sources": fallback_sources if request.showSources else [],
                        "sourcesForFollowUps": fallback_sources,
                        "retrieval": retrieval_result,
                        "raw": {"mode": "model_timeout_fallback", "error": str(timeout_exc)},
                    })
                raise HTTPException(status_code=502, detail=f"Model connection failed: {timeout_exc}")
            raise HTTPException(status_code=502, detail=f"Model connection failed: {request_exc}")

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
            stream_chunks: List[Dict[str, Any]] = []
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
                stream_chunks.append(chunk)
                delta = chunk.get("message", {}).get("content", "")
                if delta:
                    streamed_content += delta
                    CHAT_STREAM_CONTEXT.content_streamed = True
                    emit_chat_event("delta", content=delta)
            data = {
                "choices": [{"message": {"content": streamed_content}}],
                "streamChunks": len(stream_chunks),
                "provider": "ollama",
            }
        elif request.stream and "text/event-stream" in response.headers.get("content-type", "").lower():
            streamed_content = ""
            stream_chunks: List[Dict[str, Any]] = []
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
                stream_chunks.append(chunk)
                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                if delta:
                    streamed_content += delta
                    CHAT_STREAM_CONTEXT.content_streamed = True
                    emit_chat_event("delta", content=delta)
            data = {
                "choices": [{"message": {"content": streamed_content}}],
                "streamChunks": len(stream_chunks),
            }
        else:
            data = response.json()
        content = extract_model_content(data)

        final_sources = build_sources(retrieval_result, max_sources=active_limit)
        return finalize_chat_response(request, latest_user_text, {
            "modelId": final_model["id"],
            "content": content or "응답 본문을 찾지 못했습니다.",
            "sources": final_sources if request.showSources else [],
            "sourcesForFollowUps": final_sources,
            "retrieval": retrieval_result,
            "raw": data,
            "usedFallbackModel": final_model["id"] if final_model["id"] != model["id"] else None,
        })
chat_service = ChatService()
