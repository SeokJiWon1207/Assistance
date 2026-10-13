"""A/B 답변의 근거 사용과 정보 충실도를 비교 채점한다."""

import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, TypeVar

import requests
from fastapi import HTTPException

from backend.answers import answer_style_instruction
from backend.common import clamp_int, normalize_for_match
from backend.mantis_status import source_status
from backend.llm import (
    build_generation_options,
    extract_model_content,
    get_model_api_key,
    get_private_model,
    post_model_chat,
)
from backend.retrieval import build_model_retrieval_context, build_sources
from backend.schemas import ChatRequest
from backend.sources.notion import (
    extract_notion_constraints,
    merge_common_notion_constraints,
    notion_page_matches_constraints,
)
from backend.understanding import (
    extract_query_constraints,
    extract_requested_limit,
    has_constraint,
    text_matches_constraints,
    text_matches_people,
    text_matches_screen_numbers,
    text_matches_status,
)
from backend.evaluation.verification import build_basic_grounding


Dependency = TypeVar("Dependency", bound=Callable[..., Any])


def _compat_dependency(name: str, default: Dependency) -> Dependency:
    """호환 파사드에 적용된 테스트 패치를 실제 구현에도 전달한다."""
    core = sys.modules.get("backend.evaluation.core")
    return getattr(core, name, default) if core is not None else default


EVASIVE_ANSWER_PHRASES = (
    "데이터는 부족",
    "데이터가 부족",
    "정보가 부족",
    "분석할 수 있는 데이터",
    "분석하기 어렵",
    "판단하기 어렵",
    "확인하기 어렵",
    "찾지 못했습니다",
    "찾을 수 없습니다",
    "제공하기 어렵",
    "포함하기 어렵",
)


def contains_evasive_phrase(content: str) -> bool:
    normalized = normalize_for_match(content or "")
    return any(normalize_for_match(phrase) in normalized for phrase in EVASIVE_ANSWER_PHRASES)


def evaluate_ab_variant(
    question: str,
    content: str,
    sources: List[Dict[str, Any]],
    status: str = "ok",
) -> Dict[str, Any]:
    constraints = extract_query_constraints(question)
    notion_constraints = merge_common_notion_constraints(
        extract_notion_constraints(question),
        constraints,
    )
    requested_limit = extract_requested_limit(question)
    failures: List[str] = []

    if status != "ok":
        return {
            "score": 0,
            "metrics": {"evidence": 0, "constraints": 0, "grounding": 0, "answerUse": 0, "informativeness": 0},
            "failureReasons": ["응답 생성에 실패했습니다."],
            "sourceCount": len(sources),
        }

    evidence_score = 100 if sources else 0
    if not sources:
        failures.append("검색 근거가 연결되지 않았습니다.")
    elif requested_limit and len(sources) < requested_limit:
        evidence_score = round(len(sources) / requested_limit * 100)
        failures.append(f"요청 {requested_limit}건 중 {len(sources)}건만 확인했습니다.")

    matches: List[bool] = []
    for source in sources:
        if source.get("type") == "notion":
            matches.append(notion_page_matches_constraints(source, notion_constraints))
            continue
        source_text = json.dumps(source, ensure_ascii=False, default=str)
        matched = text_matches_constraints(source_text, constraints)
        matched = matched and text_matches_status(source_status(source), constraints.get("statuses", []))
        matched = matched and text_matches_screen_numbers(source_text, constraints.get("screen_numbers", []))
        matched = matched and text_matches_people(
            source_text, constraints.get("people", {}),
            str(source.get("handler") or (source.get("detail") or {}).get("handler") or ""),
            str(source.get("reporter") or (source.get("detail") or {}).get("reporter") or ""),
        )
        if constraints.get("issue_ids"):
            matched = matched and source.get("issue_id") in constraints["issue_ids"]
        matches.append(matched)
    constraint_score = round(sum(matches) / len(matches) * 100) if matches else (100 if not has_constraint(constraints) else 0)
    mismatch_count = matches.count(False)
    if mismatch_count:
        failures.append(f"질문 조건과 일치하지 않을 수 있는 근거가 {mismatch_count}건 있습니다.")

    answer_ids = {int(value) for value in re.findall(r"#?0*(\d{5,6})\b", content or "")}
    source_ids = {int(source["issue_id"]) for source in sources if source.get("issue_id")}
    unsupported_ids = sorted(answer_ids - source_ids)
    # 언급한 ID 중 근거 없는 게 하나라도 있으면 무조건 0점으로 처리하면, 5개 중
    # 1개만 지어낸 답변과 전부 지어낸 답변이 똑같이 취급된다. 실제로 맞힌 비율로
    # 채점해 환각의 심각도에 비례한 감점이 되게 한다.
    grounding_score = (
        round((len(answer_ids) - len(unsupported_ids)) / len(answer_ids) * 100)
        if answer_ids
        else 100
    )
    if unsupported_ids:
        failures.append(f"근거에 없는 이슈 ID가 포함되었습니다: {', '.join(map(str, unsupported_ids))}")

    referenced = 0
    normalized_content = normalize_for_match(content)
    for source in sources:
        issue_id = source.get("issue_id")
        title = source.get("title", "")
        if issue_id and re.search(rf"#?0*{issue_id}\b", content or ""):
            referenced += 1
        elif title and normalize_for_match(title) in normalized_content:
            referenced += 1
    answer_use_score = round(referenced / len(sources) * 100) if sources else 0
    if sources and referenced == 0:
        failures.append("답변에서 연결된 근거를 직접 확인하기 어렵습니다.")

    # grounding·answerUse는 "지어내지 않았는가"와 "근거를 인용했는가"만 본다.
    # 그래서 "찾은 근거가 있는데도 실질적인 분석 없이 회피성 문구로 답변을 흐리는"
    # 저정보 답변은 두 지표 모두 만점을 받을 수 있다. 근거가 있음에도 회피 표현을
    # 쓰면서 근거 활용도까지 낮으면 별도로 감점한다.
    has_evasive_phrase = contains_evasive_phrase(content)
    if not sources:
        informativeness_score = 100
    elif has_evasive_phrase and answer_use_score < 60:
        informativeness_score = max(0, answer_use_score - 30)
        failures.append("찾은 근거가 있는데도 회피성 표현으로 결론을 흐렸습니다.")
    elif has_evasive_phrase:
        informativeness_score = 70
        failures.append("회피성 표현이 포함되어 있어 확인이 필요합니다.")
    else:
        informativeness_score = 100

    score = round(
        evidence_score * 0.25
        + constraint_score * 0.30
        + grounding_score * 0.20
        + answer_use_score * 0.10
        + informativeness_score * 0.15
    )
    return {
        "score": score,
        "metrics": {
            "evidence": evidence_score,
            "constraints": constraint_score,
            "grounding": grounding_score,
            "answerUse": answer_use_score,
            "informativeness": informativeness_score,
        },
        "failureReasons": failures,
        "sourceCount": len(sources),
    }


def evaluate_ab_comparison(
    question: str,
    variant_a: Dict[str, Any],
    variant_b: Dict[str, Any],
) -> Dict[str, Any]:
    result_a = evaluate_ab_variant(
        question,
        str(variant_a.get("content", "")),
        variant_a.get("sources") or [],
        str(variant_a.get("status", "ok")),
    )
    result_b = evaluate_ab_variant(
        question,
        str(variant_b.get("content", "")),
        variant_b.get("sources") or [],
        str(variant_b.get("status", "ok")),
    )
    difference = result_a["score"] - result_b["score"]
    winner = "tie" if abs(difference) < 5 else "A" if difference > 0 else "B"
    return {
        "winner": winner,
        "scoreDifference": abs(difference),
        "variantA": result_a,
        "variantB": result_b,
        "metricLabels": {
            "evidence": "근거 연결",
            "constraints": "조건 일치",
            "grounding": "근거 정확성",
            "answerUse": "답변 내 근거 사용",
            "informativeness": "정보 충실도",
        },
        "evaluatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def generate_basic_grounded_baseline(
    request: ChatRequest,
    question: str,
    retrieval_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Answer from raw Mantis/Notion retrieval without the A-variant post-processing."""
    get_model = _compat_dependency("get_private_model", get_private_model)
    post_chat = _compat_dependency("post_model_chat", post_model_chat)
    model = get_model(request.modelId)
    api_key = get_model_api_key(model)
    if not api_key:
        return {
            "status": "error",
            "content": "비교 응답을 만들 수 없습니다. 선택한 모델의 API 키를 확인해 주세요.",
        }

    limit = clamp_int(request.searchLimit, 1, 20) or 5
    grounding, baseline_sources = build_basic_grounding(retrieval_result, limit)
    evidence_count = len(grounding.get("issues", [])) + len(grounding.get("notion_pages", []))
    payload = {
        "model": model.get("model", model["id"]),
        "messages": [
            {
                "role": "system",
                "content": (
                    "너는 컨텐츠서비스 AI 비서다. 아래 [기본 검색 결과]에 포함된 Mantis와 Notion 데이터만 "
                    "사내 사실 근거로 사용해라. 별칭 확장, 조건 재검증, 재정렬, Vector DB 보강은 적용되지 않았다. "
                    "근거에 없는 사실은 추측하지 말고, 결과가 없으면 찾지 못했다고 명확히 말해라. "
                    f"{answer_style_instruction(request.answerStyle)}\n\n"
                    f"[기본 검색 결과]\n{json.dumps(grounding, ensure_ascii=False, indent=2)}"
                ),
            },
            {"role": "user", "content": question},
        ],
        "temperature": request.temperature,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    generation_options = build_generation_options(request)
    generation_options.pop("response_format", None)
    payload.update(generation_options)
    # 비교안은 장문의 보고서가 아니라 차이를 빠르게 확인하는 용도다.
    payload["max_tokens"] = min(clamp_int(request.maxTokens, 1, 1200) or 800, 1200)

    try:
        timeout = clamp_int(os.getenv("AB_BASELINE_TIMEOUT_SECONDS", "60"), 10, 120) or 60
        baseline_response = post_chat(
            model,
            api_key,
            payload,
            timeout=timeout,
            think=False,
        )
        if baseline_response.status_code >= 400:
            return {
                "status": "error",
                "content": f"비교 응답 생성에 실패했습니다. (HTTP {baseline_response.status_code})",
            }
        data = baseline_response.json()
        content = extract_model_content(data)
        return {
            "status": "ok" if content else "error",
            "content": content or "비교 응답 본문을 찾지 못했습니다.",
            "modelId": model["id"],
            "sources": baseline_sources,
            "sourceCount": evidence_count,
        }
    except requests.ReadTimeout:
        return {
            "status": "error",
            "content": "비교 응답 시간이 초과되었습니다. 전처리 적용 답변은 정상적으로 사용할 수 있습니다.",
        }
    except (requests.RequestException, ValueError) as exc:
        return {
            "status": "error",
            "content": f"비교 응답 연결에 실패했습니다: {type(exc).__name__}",
        }


def generate_candidate_model_answer(
    request: ChatRequest,
    question: str,
    retrieval_result: Dict[str, Any],
    candidate_model_id: str,
) -> Dict[str, Any]:
    """동일한 검증·검색 결과로 후보 모델 답변을 생성한다."""
    get_model = _compat_dependency("get_private_model", get_private_model)
    post_chat = _compat_dependency("post_model_chat", post_model_chat)
    try:
        model = get_model(candidate_model_id)
    except HTTPException:
        return {"status": "error", "content": f"비교 모델을 찾을 수 없습니다: {candidate_model_id}"}
    api_key = get_model_api_key(model)
    if not api_key:
        return {"status": "error", "content": "비교 모델의 API 키가 설정되지 않았습니다."}

    limit = clamp_int(request.searchLimit, 1, 20) or 5
    grounding = build_model_retrieval_context(retrieval_result, limit)
    candidate_sources = build_sources(retrieval_result, max_sources=max(1, limit * 2))
    payload = {
        "model": model.get("model", model["id"]),
        "messages": [
            {
                "role": "system",
                "content": (
                    "너는 컨텐츠서비스 AI 비서다. 아래 [검증된 검색 결과]만 회사 사실의 근거로 사용해라. "
                    "질문의 포함·제외 조건을 그대로 지키고, 근거에 없는 사실은 추측하지 마라. "
                    "결과가 부족하거나 적용되지 않은 조건이 있으면 명확히 밝혀라. "
                    f"{answer_style_instruction(request.answerStyle)}\n\n"
                    f"[검증된 검색 결과]\n{json.dumps(grounding, ensure_ascii=False, indent=2)}"
                ),
            },
            {"role": "user", "content": question},
        ],
        "temperature": request.temperature,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    generation_options = build_generation_options(request)
    generation_options.pop("response_format", None)
    payload.update(generation_options)
    payload["max_tokens"] = min(clamp_int(request.maxTokens, 1, 1200) or 800, 1200)
    try:
        response = post_chat(
            model,
            api_key,
            payload,
            timeout=clamp_int(os.getenv("AB_CANDIDATE_TIMEOUT_SECONDS", "60"), 10, 120) or 60,
            think=False,
        )
        if response.status_code >= 400:
            return {"status": "error", "content": f"비교 모델 응답 실패 (HTTP {response.status_code})"}
        content = extract_model_content(response.json())
        return {
            "status": "ok" if content else "error",
            "content": content or "비교 모델 응답 본문을 찾지 못했습니다.",
            "modelId": model["id"],
            "sources": candidate_sources,
            "sourceCount": len(candidate_sources),
        }
    except requests.ReadTimeout:
        return {"status": "error", "content": "비교 모델 응답 시간이 초과되었습니다."}
    except (requests.RequestException, ValueError) as exc:
        return {"status": "error", "content": f"비교 모델 연결 실패: {type(exc).__name__}"}


def should_attach_ab_test(request: ChatRequest, retrieval_result: Any, mode: str) -> bool:
    if not request.abTest or retrieval_result is None:
        return False
    return not mode.startswith("notion_write") and mode not in {
        "deterministic_work_report",
        "empty_evidence",
        "status_clarification",
        "notion_create",
        "notion_update",
        "notion_create_missing_title",
        "notion_update_missing_fields",
        "notion_update_missing_target",
        "notion_update_not_found",
        "notion_update_ambiguous",
    }


__all__ = [
    "contains_evasive_phrase",
    "evaluate_ab_comparison",
    "evaluate_ab_variant",
    "generate_basic_grounded_baseline",
    "generate_candidate_model_answer",
    "should_attach_ab_test",
]
