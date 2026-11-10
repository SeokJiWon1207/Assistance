"""요청·대화·서버 감사 기록에서 공유 채팅 문맥을 준비한다."""

import os
import re
from typing import Any, Dict, Optional
from fastapi import HTTPException

from backend.answers import CONVERSATION_TONE_NOTE, answer_style_instruction, build_current_date_note
from backend.intent.request_scope import authenticated_owner_scope
from backend.intent.status_semantics import ALL_STATUSES, resolution_is_grounded
from backend.domain.team import has_mantis_team_scope
from backend.audit import load_audit_query_plan, load_audit_sources
from backend.utils.numbers import clamp_int
from backend.llm import get_private_model
from backend.intent.list_continuation import (
    list_continuation_mode,
    confirmed_list_continuation,
    remove_list_continuation_request,
)
from backend.timing import measured
from backend.intent.context import (
    is_affirmative_read_follow_up,
    build_contextual_retrieval_text,
    get_recent_user_texts,
    is_follow_up_question,
    resolve_reference_from_context,
    rewrite_follow_up_query_with_model,
)
from backend.intent.confirmed_context import (
    confirmed_scope_text,
    merge_confirmed_constraints,
    restore_confirmed_constraints,
    confirmed_keyword_text,
)
from backend.services.contracts import ChatPipelineContext
from backend.schemas import ChatRequest
from backend.notion.search import inherit_notion_context
from backend.intent.constraints import extract_query_constraints
from backend.intent.keyword_terms import extract_requested_limit, wants_all_results
from backend import llm
from backend.services.chat.scope import (
    apply_authenticated_mantis_scope,
    apply_authenticated_notion_scope,
    should_scope_to_authenticated_mantis_user,
    should_scope_to_authenticated_notion_user,
)


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

    api_key = llm.get_model_api_key(model)
    messages = [message.model_dump(exclude={"notionActionId"}) for message in request.messages]
    history_limit = clamp_int(os.getenv("MODEL_HISTORY_MAX_MESSAGES", "20"), 2, 200) or 20
    if len(messages) > history_limit:
        messages = messages[-history_limit:]

    from backend.intent.direct_references import explicit_reference_plan
    direct_plan = explicit_reference_plan(latest_user_text)
    if direct_plan:
        return ChatPipelineContext(
            latest_user_text=latest_user_text, model=model, api_key=api_key, messages=messages,
            retrieval_text=latest_user_text, enabled_tools={'mantis': True, 'notion': True, 'vectorDb': True, **request.tools},
            system_notes=[answer_style_instruction(request.answerStyle), CONVERSATION_TONE_NOTE, build_current_date_note()],
            query_constraints=direct_plan['resolved_constraints'], active_limit=direct_plan['limit'], intent_plan=direct_plan,
            authenticated_owner=str((user or {}).get('mantisUsername') or (user or {}).get('displayName') or ''),
        )

    from backend.intent.unified import enabled as unified_enabled
    from backend.intent.entities import short_handler_lookup
    if unified_enabled() or short_handler_lookup(latest_user_text):
        from backend.services.unified_planning import prepare_context
        return prepare_context(request, latest_user_text, user, model, api_key, messages, [
            answer_style_instruction(request.answerStyle), CONVERSATION_TONE_NOTE, build_current_date_note(),
        ])

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
    prior_questions = get_recent_user_texts(request.messages, limit=20)
    prior_question = prior_questions[1] if len(prior_questions) > 1 else ""
    # 재시도·새로 고침 뒤 미완료 질문이 남아도 감사 ID가 붙은 완료 답변의 질문을 사용한다.
    for index in range(len(request.messages) - 2, -1, -1):
        if request.messages[index].role == "assistant":
            prior_question = next((message.content for message in reversed(request.messages[:index])
                                   if message.role == "user"), "")
            break
    prior_plan = load_audit_query_plan(
        request.contextAuditId or "", str((user or {}).get("uid") or ""),
        prior_question, **({"variant": request.contextVariant} if request.contextVariant != "A" else {}),
    ) if (status_choice or lookup_reply or is_follow_up_question(latest_user_text)) else {}
    if not prior_plan and request.contextAuditId and (status_choice or lookup_reply or is_follow_up_question(latest_user_text)):
        # 큰 답변이 전송 문맥에서 제외된 경우에도 대화에 실제로 남은 질문만 감사 원문과 대조한다.
        for candidate in dict.fromkeys(prior_questions[1:]):
            if candidate == prior_question:
                continue
            prior_plan = load_audit_query_plan(request.contextAuditId, str((user or {}).get("uid") or ""), candidate,
                                               **({"variant": request.contextVariant} if request.contextVariant != "A" else {}))
            if prior_plan:
                break
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
        **({"variant": request.contextVariant} if request.contextVariant != "A" else {}),
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

    if list_continuation_mode(latest_user_text):
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
            if list_continuation_mode(latest_user_text):
                retrieval_text = remove_list_continuation_request(retrieval_text) + "이슈 보여줘"
            keyword_text = confirmed_keyword_text(scoped_constraints)
            if keyword_text:
                retrieval_text = f"{retrieval_text} {keyword_text}"
    # 공수 요청의 생략형은 인증된 직전 계획에서 작업까지 이어받는다.
    # 명시적인 목록/건수 요청에는 이전 작업 증거를 남기지 않는다.
    from backend.intent.context import is_dimension_only_followup
    scoped_constraints.pop("followup_effort", None)
    if (inherited_constraints is not None and is_dimension_only_followup(latest_user_text)
            and prior_plan.get("operation") == "aggregate"
            and "공수" in str(prior_plan.get("grounding_text") or "")):
        scoped_constraints["followup_effort"] = {
            "parent_audit_id": request.contextAuditId,
            "parent_question": prior_plan["grounding_text"],
            "question": latest_user_text,
        }
        retrieval_text += " 공수 집계"
    # 페이지 이력은 조건 변경·전체 요청으로 전파하지 않는다.
    scoped_constraints.pop("list_continuation", None)
    scoped_constraints.pop("list_cursor", None)
    from backend.intent.scope_changes import has_scope_edit
    from backend.intent.context import is_customer_only_refinement
    scoped_constraints.pop("list_scope_edit", None)
    scoped_constraints.pop("list_show_all", None)
    if inherited_constraints is not None and (
        has_scope_edit(latest_user_text) or is_customer_only_refinement(latest_user_text, latest_constraints)
        or latest_constraints.get("mantis_team_scope") in {"team", "all"}
    ):
        scoped_constraints["list_scope_edit"] = True
        if extract_requested_limit(latest_user_text) is None and (
            inherited_constraints.get("list_show_all")
            or wants_all_results(str(prior_plan.get("grounding_text") or ""))
        ):
            scoped_constraints["list_show_all"] = True
    if wants_all_results(latest_user_text) and extract_requested_limit(latest_user_text) is None:
        scoped_constraints["list_show_all"] = True
    if list_continuation_mode(latest_user_text):
        if inherited_constraints is not None:
            scoped_constraints["list_continuation"] = confirmed_list_continuation(
                latest_user_text, prior_plan, load_audit_sources(
                    request.contextAuditId or "", str((user or {}).get("uid") or ""),
                    **({"variant": request.contextVariant} if request.contextVariant != "A" else {}),
                ),
            )
        else:
            scoped_constraints["list_context_missing"] = True
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
