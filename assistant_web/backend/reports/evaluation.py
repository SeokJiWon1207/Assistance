"""검증·평가 리포트 — 근거 검증, A/B 비교, 회귀 평가 케이스 실행(상위 계층).

답변-근거 일치 검증(build_verification_report), 기본 그라운딩 베이스라인과
A/B 비교(evaluate_ab_*), 감사 로그 기록을 포함한 응답 마무리(finalize_chat_response),
회귀 평가 케이스 실행/저장(run_evaluation_case 등), 정확도 리포트 분석을 담당한다.

의존: retrieval/answers/effort + sources + understanding + llm/audit.
"""

import json
import os
import re
import sqlite3
import sys
import time
import uuid
from contextlib import closing
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import HTTPException

from backend.answers import (
    answer_style_instruction,
    build_mantis_month_count_report,
    display_constraints,
    generate_follow_up_questions,
)
from backend.audit import (
    CHAT_STREAM_CONTEXT,
    emit_chat_event,
    emit_chat_status,
    init_audit_db,
)
from backend.common import (
    clamp_int,
    normalize_for_match,
    text_matches_customer_aliases,
)
from backend.config import AUDIT_DB_PATH, CONFIG_DIR
from backend.llm import (
    build_generation_options,
    extract_model_content,
    get_model_api_key,
    get_private_model,
    post_model_chat,
)
from backend.query_planning import plan_created_at_matches, validate_query_plan
from backend.reports.effort import (
    EFFORT_FIELD_LABELS,
    build_effort_report,
    parse_effort_field_value,
)
from backend.retrieval import (
    build_model_retrieval_context,
    build_sources,
    retrieval_trace,
    run_retrieval_pipeline,
    similar_base_issue_ids_from_retrieval,
)
from backend.schemas import ChatRequest
from backend.sources.notion import (
    extract_notion_constraints,
    merge_common_notion_constraints,
    notion_page_matches_constraints,
)
from backend.understanding import (
    extract_query_constraints,
    extract_requested_limit,
    field_matches_screen_number,
    has_constraint,
    text_matches_constraints,
    text_matches_people,
    text_matches_screen_numbers,
    text_matches_status,
    title_has_screen_number,
)


DEFAULT_EVALUATION_CASES = [
    {
        "id": "mantis-kb-hts",
        "category": "고객사·플랫폼",
        "question": "KB증권 HTS 최근 이슈 5개 보여줘",
        "expected": {"customer": "KB증권", "platform": "HTS", "minResults": 1},
    },
    {
        "id": "mantis-eugene-exclude-mts",
        "category": "플랫폼 제외",
        "question": "유진증권 이슈에서 MTS는 빼고 최근 10개 보여줘",
        "expected": {"customer": "유진증권", "platformExclude": "MTS", "minResults": 1},
    },
    {
        "id": "mantis-eugene-screen",
        "category": "화면번호",
        "question": "유진증권 화면번호 5031 관련 이슈 찾아줘",
        "expected": {"customer": "유진증권", "screenNumber": "5031", "minResults": 1},
    },
    {
        "id": "mantis-db-alias",
        "category": "고객사 별칭",
        "question": "DB증권 최근 이슈 보여줘",
        "expected": {"customer": "동부증권", "minResults": 1},
    },
    {
        "id": "mantis-exact-id",
        "category": "정확 ID",
        "question": "0028508 이슈 내용 알려줘",
        "expected": {"issueId": 28508, "minResults": 1},
    },
    {
        "id": "notion-incident",
        "category": "Notion 키워드",
        "question": "노션에서 장애보고 관련 이슈 찾아줘",
        "expected": {"source": "notion", "keyword": "장애보고", "minResults": 1},
    },
    {
        "id": "notion-major-hanwha-order",
        "category": "Notion 복합 키워드",
        "question": "노션에서 한화 서버 자동주문 장애에 관한 건 알려줘",
        "expected": {
            "source": "notion",
            "titleContains": "한화 서버 자동주문 장애",
            "databaseLabel": "주요이슈현황",
            "minResults": 1,
        },
    },
    {
        "id": "mantis-effort-person-month",
        "category": "Mantis 공수 집계",
        "question": "2026년 2월 석지원의 공수를 알고 싶어",
        "expected": {"effortTotal": 5.25, "issueCount": 18, "minResults": 1},
    },
]


def load_evaluation_cases() -> List[Dict[str, Any]]:
    cases_path = CONFIG_DIR / "evaluation_cases.json"
    if not cases_path.exists():
        return DEFAULT_EVALUATION_CASES
    try:
        data = json.loads(cases_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"[warn] evaluation_cases.json 로딩 실패, 내장 기본값 사용: {exc}", file=sys.stderr)
        return DEFAULT_EVALUATION_CASES
    if not isinstance(data, list) or not all(
        isinstance(case, dict)
        and isinstance(case.get("id"), str)
        and isinstance(case.get("question"), str)
        and isinstance(case.get("expected"), dict)
        for case in data
    ):
        print(
            "[warn] evaluation_cases.json 형식 오류(id/question 문자열, expected 객체 필수), 내장 기본값 사용",
            file=sys.stderr,
        )
        return DEFAULT_EVALUATION_CASES
    return data


EVALUATION_CASES = load_evaluation_cases()


def ungrounded_constraint_labels(
    grounding_text: str,
    constraints: Dict[str, Any],
) -> List[str]:
    """질문에 근거가 없는 검색 조건을 찾는다.

    기존 검증은 계획과 결과가 서로 맞는지만 봐서, 모델이 만들어낸 고객사·
    담당자 필터가 조용히 적용돼도 전부 통과했다. 조건이 실제로 사용자가 한
    말에서 나왔는지를 따로 확인한다.

    근거 텍스트로는 검색에 실제 쓰인 문장을 쓴다. 후속 질문에서 이전 턴의
    조건을 물려받은 경우까지 근거 없음으로 잡히면 경고가 무의미해진다.
    """
    normalized = normalize_for_match(grounding_text)
    labels: List[str] = []

    customer_key = constraints.get("customer_key")
    if customer_key and not any(
        normalize_for_match(alias) in normalized
        for alias in [customer_key, *(constraints.get("customer_aliases") or [])]
        if alias
    ):
        labels.append(f"고객사={customer_key}")

    platform_key = constraints.get("platform_key")
    if platform_key and not any(
        normalize_for_match(alias) in normalized
        for alias in [platform_key, *(constraints.get("platform_aliases") or [])]
        if alias
    ):
        labels.append(f"플랫폼={platform_key}")

    for name in constraints.get("people", {}).get("names") or []:
        if normalize_for_match(name) not in normalized:
            labels.append(f"담당자={name}")

    for number in constraints.get("screen_numbers") or []:
        if str(number) not in str(grounding_text):
            labels.append(f"화면번호={number}")

    return labels


def build_verification_report(
    question: str,
    content: str,
    sources: List[Dict[str, Any]],
    retrieval_result: Optional[Dict[str, Any]],
    mode: str,
) -> Dict[str, Any]:
    constraints = (retrieval_result or {}).get("constraints") or extract_query_constraints(question)
    query_plan = (retrieval_result or {}).get("query_plan") or (retrieval_result or {}).get("intent_plan") or {}
    retrieval_checks: List[Dict[str, str]] = []
    retrieval_used = bool(retrieval_result)
    successful_count_stage = next(
        (
            stage for stage in (retrieval_result or {}).get("stages", [])
            if stage.get("status") == "ok" and stage.get("meta", {}).get("operation") == "count"
        ),
        None,
    )
    has_evidence = bool(sources or successful_count_stage or not retrieval_used)
    retrieval_checks.append(
        {
            "id": "evidence",
            "label": "원문 근거",
            "status": "pass" if has_evidence else "fail",
            "detail": (
                f"연결된 원문 {len(sources)}건"
                if sources
                else "집계 실행 완료"
                if successful_count_stage
                else "일반 답변"
                if not retrieval_used
                else "연결된 원문 없음"
            ),
        }
    )

    answer_ids = {int(value) for value in re.findall(r"#?0*(\d{5,6})\b", content or "")}
    source_ids = {int(source["issue_id"]) for source in sources if source.get("issue_id")}
    similar_base_issue_ids = set(similar_base_issue_ids_from_retrieval(retrieval_result or {}))
    unsupported_ids = sorted(answer_ids - source_ids)
    unsupported_ids = [issue_id for issue_id in unsupported_ids if issue_id not in similar_base_issue_ids]
    retrieval_checks.append(
        {
            "id": "issue_ids",
            "label": "이슈 ID 일치",
            "status": "pass" if not unsupported_ids else "fail",
            "detail": "모든 ID가 원문과 일치" if not unsupported_ids else f"근거 없는 ID: {', '.join(map(str, unsupported_ids))}",
        }
    )

    match_constraints = constraints
    if similar_base_issue_ids:
        match_constraints = {**constraints, "issue_ids": []}

    matched_sources = []
    for source in sources:
        if not has_constraint(match_constraints) or source.get("type") == "notion":
            matched_sources.append(True)
            continue
        source_text = json.dumps(source, ensure_ascii=False, default=str)
        source_issue_id = source.get("issue_id")
        matches = text_matches_constraints(source_text, match_constraints)
        matches = matches and text_matches_status(source_text, match_constraints.get("statuses", []))
        matches = matches and text_matches_screen_numbers(source_text, match_constraints.get("screen_numbers", []))
        matches = matches and text_matches_people(source_text, match_constraints.get("people", {}))
        matches = matches and plan_created_at_matches(
            str(source.get("created_at") or (source.get("detail") or {}).get("created_at") or ""),
            match_constraints.get("created_at"),
        )
        if match_constraints.get("issue_ids"):
            matches = matches and source_issue_id in match_constraints["issue_ids"]
        matched_sources.append(matches)
    retrieval_checks.append(
        {
            "id": "constraints",
            "label": "검색 조건",
            "status": "pass" if not matched_sources or all(matched_sources) else "warning",
            "detail": "조건 일치" if not matched_sources or all(matched_sources) else f"조건 불일치 가능성 {matched_sources.count(False)}건",
        }
    )

    ungrounded = ungrounded_constraint_labels(
        str((retrieval_result or {}).get("query") or question),
        constraints,
    )
    retrieval_checks.append(
        {
            "id": "constraint_grounding",
            "label": "조건 근거",
            "status": "pass" if not ungrounded else "warning",
            "detail": (
                "모든 조건이 질문에 근거함"
                if not ungrounded
                else f"질문에 없는 조건: {', '.join(ungrounded)}"
            ),
        }
    )

    requested_limit = extract_requested_limit(question)
    if requested_limit:
        count_status = "pass" if len(sources) >= requested_limit else "warning"
        count_detail = f"요청 {requested_limit}건 · 확인 {len(sources)}건"
    else:
        count_status = "pass"
        count_detail = f"확인 {len(sources)}건"
    retrieval_checks.append({"id": "count", "label": "요청 건수", "status": count_status, "detail": count_detail})

    def score_checks(checks: List[Dict[str, str]]) -> tuple[int, str]:
        weights = {"pass": 1.0, "warning": 0.5, "fail": 0.0, "review": 0.0}
        score = round(sum(weights.get(check["status"], 0) for check in checks) / len(checks) * 100)
        status = (
            "fail"
            if any(check["status"] == "fail" for check in checks)
            else "warning"
            if any(check["status"] in {"warning", "review"} for check in checks)
            else "pass"
        )
        return score, status

    retrieval_score, retrieval_status = score_checks(retrieval_checks)
    intent_validation = validate_query_plan(question, query_plan) if retrieval_used else {"valid": True, "checks": []}
    intent_checks = intent_validation["checks"]
    intent_score, intent_status = score_checks(intent_checks) if intent_checks else (None, "na")
    dimensions: List[Dict[str, Any]] = []
    if intent_checks:
        dimensions.append(
            {
                "id": "intent",
                "label": "질문·실행 계획 일치",
                "score": intent_score,
                "status": intent_status,
                "checks": intent_checks,
                "description": "질문의 기간, 작업 유형, 검색 소스가 실행 계획에 반영됐는지 검사합니다.",
            }
        )
    dimensions.append(
        {
            "id": "retrieval",
            "label": "검색·근거 일치",
            "score": retrieval_score,
            "status": retrieval_status,
            "checks": retrieval_checks,
            "description": "검색 조건과 연결된 원문을 검사합니다.",
        }
    )

    if mode == "deterministic_effort_report":
        effort_stage = next(
            (stage for stage in (retrieval_result or {}).get("stages", []) if stage.get("source") == "mantis_effort"),
            {},
        )
        effort_meta = effort_stage.get("meta", {})
        person = effort_meta.get("person", "")
        recomputed_totals = {field: 0.0 for field in EFFORT_FIELD_LABELS}
        for source in sources:
            for field_name, value in (source.get("detail", {}).get("effort_fields") or {}).items():
                if field_name not in recomputed_totals:
                    continue
                for effort_person, amount in parse_effort_field_value(value):
                    if normalize_for_match(effort_person) == normalize_for_match(person):
                        recomputed_totals[field_name] += amount
        recomputed_total = round(sum(recomputed_totals.values()), 4)
        stored_totals = effort_meta.get("totals", {})
        stored_total = float(effort_meta.get("total_effort", 0))
        displayed_total_match = re.search(r"총\s*공수:\s*\*\*([0-9.]+)", content or "")
        displayed_total = float(displayed_total_match.group(1)) if displayed_total_match else None
        calculation_checks = [
            {
                "id": "effort_recalculation",
                "label": "원문 재합산",
                "status": "pass" if abs(recomputed_total - stored_total) < 0.0001 else "fail",
                "detail": f"원문 {recomputed_total:.2f} · 집계 {stored_total:.2f}",
            },
            {
                "id": "effort_breakdown",
                "label": "유형별 합계",
                "status": "pass"
                if all(abs(recomputed_totals[field] - float(stored_totals.get(field, 0))) < 0.0001 for field in recomputed_totals)
                else "fail",
                "detail": "문의·서버·PC·QC 재계산 일치",
            },
            {
                "id": "effort_display",
                "label": "화면 표시값",
                "status": "pass"
                if displayed_total is not None and abs(displayed_total - stored_total) < 0.0001
                else "fail",
                "detail": f"표시 {displayed_total:.2f} · 집계 {stored_total:.2f}" if displayed_total is not None else "총 공수 표시값을 찾지 못함",
            },
            {
                "id": "effort_issue_count",
                "label": "반영 이슈 수",
                "status": "pass" if len(sources) == int(effort_meta.get("displayed_count", 0)) else "fail",
                "detail": f"원문 {len(sources)}건 · 집계 {effort_meta.get('displayed_count', 0)}건",
            },
        ]
        calculation_score, calculation_status = score_checks(calculation_checks)
        dimensions.append(
            {
                "id": "calculation",
                "label": "계산 검증",
                "score": calculation_score,
                "status": calculation_status,
                "checks": calculation_checks,
                "description": "원문 공수를 독립적으로 다시 합산합니다.",
            }
        )
    elif mode == "deterministic_mantis_count":
        count_stage = next(
            (
                stage for stage in (retrieval_result or {}).get("stages", [])
                if stage.get("meta", {}).get("operation") == "count"
            ),
            {},
        )
        expected_count = int(count_stage.get("meta", {}).get("total_count", -1))
        displayed_match = re.search(r"총\s*(\d+)\s*건", content or "")
        displayed_count = int(displayed_match.group(1)) if displayed_match else None
        calculation_checks = [
            {
                "id": "count_result",
                "label": "집계 결과",
                "status": "pass" if displayed_count == expected_count and expected_count >= 0 else "fail",
                "detail": f"실행 결과 {expected_count}건 · 답변 {displayed_count if displayed_count is not None else '미표시'}건",
            },
            {
                "id": "count_filters",
                "label": "집계 조건 적용",
                "status": "pass"
                if count_stage.get("meta", {}).get("applied_filters") == constraints
                else "fail",
                "detail": "실행 계획의 필터로 집계" if count_stage else "집계 실행 기록 없음",
            },
        ]
        calculation_score, calculation_status = score_checks(calculation_checks)
        dimensions.append(
            {
                "id": "calculation",
                "label": "계산 검증",
                "score": calculation_score,
                "status": calculation_status,
                "checks": calculation_checks,
                "description": "도구의 전체 집계값과 답변에 표시된 건수를 비교합니다.",
            }
        )
    else:
        dimensions.append(
            {
                "id": "calculation",
                "label": "계산 검증",
                "score": None,
                "status": "na",
                "checks": [],
                "description": "계산형 답변이 아닙니다.",
            }
        )

    deterministic_mode = mode.startswith("deterministic_") or mode.startswith("notion_")
    content_checks: List[Dict[str, str]] = []
    if deterministic_mode:
        if mode == "deterministic_notion_list":
            missing_titles = [source.get("title", "") for source in sources if source.get("title") and normalize_for_match(source["title"]) not in normalize_for_match(content)]
            content_checks.append(
                {
                    "id": "notion_titles",
                    "label": "문서 제목",
                    "status": "pass" if not missing_titles else "warning",
                    "detail": "표시 제목이 원문과 일치" if not missing_titles else f"답변에 없는 제목 {len(missing_titles)}건",
                }
            )
        elif mode == "deterministic_effort_report":
            content_checks.append(
                {
                    "id": "effort_content",
                    "label": "집계 서술",
                    "status": "pass" if "총 공수" in content and "유형별 공수" in content else "fail",
                    "detail": "검증된 집계값으로 코드 생성",
                }
            )
        else:
            content_checks.append(
                {
                    "id": "deterministic_content",
                    "label": "답변 항목",
                    "status": "pass" if not unsupported_ids else "fail",
                    "detail": "검색 결과를 코드로 변환" if not unsupported_ids else "원문에 없는 이슈 ID 포함",
                }
            )
        content_score, content_status = score_checks(content_checks)
    else:
        content_checks.append(
            {
                "id": "model_content_review",
                "label": "AI 생성 서술",
                "status": "review",
                "detail": "출처 연결과 별개로 사람의 내용 검토가 필요합니다.",
            }
        )
        content_score, content_status = None, "review"
    dimensions.append(
        {
            "id": "content",
            "label": "답변 내용 검증",
            "score": content_score,
            "status": content_status,
            "checks": content_checks,
            "description": "답변에 표시된 사실과 원문 사용 방식을 검사합니다.",
        }
    )

    applicable_dimensions = [dimension for dimension in dimensions if dimension["status"] != "na"]
    completed_dimensions = [dimension for dimension in applicable_dimensions if dimension["score"] is not None]
    score = round(
        sum(dimension["score"] for dimension in completed_dimensions) / len(applicable_dimensions)
    ) if applicable_dimensions else 0
    status = (
        "fail"
        if any(dimension["status"] == "fail" for dimension in applicable_dimensions)
        else "warning"
        if any(dimension["status"] in {"warning", "review"} for dimension in applicable_dimensions)
        else "pass"
    )
    all_checks = [
        check
        for dimension in dimensions
        for check in dimension.get("checks", [])
    ]
    return {
        "score": score,
        "status": status,
        "checks": all_checks,
        "dimensions": dimensions,
        "completedDimensions": len(completed_dimensions),
        "applicableDimensions": len(applicable_dimensions),
        "constraints": display_constraints(constraints),
        "queryPlan": query_plan,
        "trace": retrieval_trace(retrieval_result),
        "sourceCount": len(sources),
        "mode": mode,
        "verifiedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "각 검증 영역은 독립적으로 표시되며, '검토 필요'는 자동 사실 판정이 불가능한 영역입니다.",
    }


def build_basic_grounding(
    retrieval_result: Dict[str, Any],
    limit: int,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    basic_stages = [
        stage
        for stage in retrieval_result.get("stages", [])
        if stage.get("source") == "notion" or str(stage.get("source", "")).startswith("mantis_")
    ]
    basic_result = {
        "query": retrieval_result.get("query", ""),
        "keyword": retrieval_result.get("keyword", ""),
        "constraints": {},
        "limit": limit,
        "stages": basic_stages,
    }
    return build_model_retrieval_context(basic_result, limit), build_sources(
        basic_result,
        max_sources=max(1, limit * 2),
    )


def generate_basic_grounded_baseline(
    request: ChatRequest,
    question: str,
    retrieval_result: Dict[str, Any],
) -> Dict[str, Any]:
    """Answer from raw Mantis/Notion retrieval without the A-variant post-processing."""
    model = get_private_model(request.modelId)
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
        baseline_response = post_model_chat(
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
    try:
        model = get_private_model(candidate_model_id)
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
        response = post_model_chat(
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
        "notion_create",
        "notion_update",
        "notion_create_missing_title",
        "notion_update_missing_fields",
        "notion_update_missing_target",
        "notion_update_not_found",
        "notion_update_ambiguous",
    }


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
        matched = matched and text_matches_status(source_text, constraints.get("statuses", []))
        matched = matched and text_matches_screen_numbers(source_text, constraints.get("screen_numbers", []))
        matched = matched and text_matches_people(source_text, constraints.get("people", {}))
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


def finalize_chat_response(
    request: ChatRequest,
    question: str,
    response: Dict[str, Any],
    user_uid: str = "",
) -> Dict[str, Any]:
    sources = response.get("sources") or []
    # sourcesForFollowUps는 화면 출처 표시 여부(showSources)와 무관하게 항상 채워지는
    # 전체 근거 목록이다. 사용자가 "출처 표시"를 꺼서 sources가 []가 되더라도
    # 후속 질문 제안과 검증 리포트는 실제로 찾은 근거를 기준으로 계속 동작해야 한다.
    evidence_sources = response.pop("sourcesForFollowUps", None)
    if evidence_sources is None:
        evidence_sources = sources
    retrieval_result = response.get("retrieval")
    mode = (response.get("raw") or {}).get("mode", "model")
    if getattr(CHAT_STREAM_CONTEXT, "emitter", None) and not getattr(
        CHAT_STREAM_CONTEXT,
        "content_streamed",
        False,
    ):
        content = str(response.get("content", ""))
        for start in range(0, len(content), 120):
            emit_chat_event("delta", content=content[start : start + 120])
        CHAT_STREAM_CONTEXT.content_streamed = True
    emit_chat_status("근거와 답변을 검증하고 있습니다", "verification")
    followups = response.get("followUps") or generate_follow_up_questions(
        question,
        evidence_sources,
        retrieval_result,
        mode,
    )
    verification = build_verification_report(
        question,
        response.get("content", ""),
        evidence_sources,
        retrieval_result,
        mode,
    )
    if should_attach_ab_test(request, retrieval_result, mode):
        candidate_model_id = str(
            request.abModelId or os.getenv("AB_COMPARISON_MODEL_ID", "")
        ).strip()
        comparing_models = bool(
            candidate_model_id
            and candidate_model_id != request.modelId
            and mode == "model"
        )
        emit_chat_status(
            "후보 모델로 동일한 검색 근거를 비교하고 있습니다"
            if comparing_models
            else "기본 Mantis·Notion 연동 답변을 생성하고 있습니다",
            "comparison",
        )
        baseline = (
            generate_candidate_model_answer(
                request,
                question,
                retrieval_result,
                candidate_model_id,
            )
            if comparing_models
            else generate_basic_grounded_baseline(request, question, retrieval_result)
        )
        variant_a_evaluation_input = {
            "status": "ok",
            "content": response.get("content", ""),
            "sources": evidence_sources,
        }
        response["abTest"] = {
            "enabled": True,
            "variantA": {
                "label": f"현재 모델 · {request.modelId}" if comparing_models else "전처리 적용",
                "description": (
                    "현재 모델이 검증된 동일 검색 근거로 생성한 답변"
                    if comparing_models
                    else "Mantis·Notion 검색, 조건 필터링, 근거 검증을 적용한 답변"
                ),
            },
            "variantB": {
                "label": f"후보 모델 · {candidate_model_id}" if comparing_models else "기본 검색 연동",
                "description": (
                    "후보 모델이 현재 모델과 같은 검증·검색 결과로 생성한 답변"
                    if comparing_models
                    else "Mantis·Notion 기본 검색 결과만 전달하고 별칭 보정·재정렬·검증은 적용하지 않은 답변"
                ),
                **baseline,
            },
        }
        response["abTest"]["evaluation"] = evaluate_ab_comparison(
            question,
            variant_a_evaluation_input,
            baseline,
        )
    log_id = uuid.uuid4().hex
    # 모델 fallback이 일어난 경우 response["modelId"]는 실제로 답변한 모델이다.
    # 감사 로그는 사용자가 고른 모델이 아니라 실제로 응답을 생성한 모델을 기록해야 한다.
    answered_model_id = response.get("modelId") or request.modelId
    payload = {
        "question": question,
        "userUid": str(user_uid or "").strip(),
        "answer": response.get("content", ""),
        "modelId": answered_model_id,
        "mode": mode,
        "generation": {
            "temperature": request.temperature,
            "answerStyle": request.answerStyle,
            "responseFormat": request.responseFormat,
        },
        "sources": sources,
        "queryPlan": (retrieval_result or {}).get("query_plan") if retrieval_result else None,
        "filterAudit": (retrieval_result or {}).get("filter_audit") if retrieval_result else None,
        "retrievalStages": [
            {
                "source": stage.get("source"),
                "status": stage.get("status"),
                "reason": stage.get("reason"),
                "meta": stage.get("meta", {}),
            }
            for stage in (retrieval_result or {}).get("stages", [])
        ],
        "followUps": followups,
        "verification": verification,
        "abTest": response.get("abTest"),
    }
    init_audit_db()
    with closing(sqlite3.connect(AUDIT_DB_PATH)) as connection, connection:
        connection.execute(
            """
            INSERT INTO validation_logs (
                id, created_at, question, model_id, mode, score, status, payload,
                user_uid, failed_checks_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                log_id,
                verification["verifiedAt"],
                question,
                answered_model_id,
                mode,
                verification["score"],
                verification["status"],
                json.dumps(payload, ensure_ascii=False),
                str(user_uid or "").strip(),
                json.dumps(
                    [
                        {
                            "id": str(check.get("id") or "unknown")[:128],
                            "label": str(check.get("label") or check.get("id") or "unknown")[:256],
                        }
                        for check in verification.get("checks", [])
                        if check.get("status") == "fail"
                    ],
                    ensure_ascii=False,
                ),
            ),
        )
    response["verification"] = verification
    response["auditId"] = log_id
    response["followUps"] = followups
    return response


def run_evaluation_case(case: Dict[str, Any]) -> Dict[str, Any]:
    started = time.perf_counter()
    question = case["question"]
    expected = case["expected"]
    constraints = extract_query_constraints(question)
    effort_report = build_effort_report(question) if expected.get("effortTotal") is not None else None
    count_report = build_mantis_month_count_report(question) if expected.get("totalCount") is not None else None
    retrieval_result = (
        effort_report.get("retrieval")
        if effort_report
        else count_report.get("retrieval")
        if count_report
        else run_retrieval_pipeline(question, 5, {"mantis": True, "vectorDb": True, "notion": True})
    )
    sources = (
        effort_report.get("sources", [])
        if effort_report
        else count_report.get("sources", [])
        if count_report
        else build_sources(retrieval_result, max_sources=20)
    )
    checks: List[Dict[str, Any]] = []

    def add_check(label: str, passed: bool, actual: str) -> None:
        checks.append({"label": label, "passed": passed, "actual": actual})

    if expected.get("customer"):
        aliases = constraints.get("customer_aliases", [])
        passed = any(normalize_for_match(expected["customer"]) in normalize_for_match(alias) or normalize_for_match(alias) in normalize_for_match(expected["customer"]) for alias in aliases)
        add_check("고객사 인식", passed, constraints.get("customer_key") or "미인식")
        mantis_sources = [source for source in sources if source.get("type") in {"mantis", "chroma"}]
        if mantis_sources:
            result_aliases = constraints.get("customer_aliases", []) or [expected["customer"]]
            mismatches = [
                source.get("project", "미상")
                for source in mantis_sources
                if not text_matches_customer_aliases(str(source.get("project", "")), result_aliases)
            ]
            add_check(
                "고객사 결과 일치",
                not mismatches,
                "모두 일치" if not mismatches else ", ".join(dict.fromkeys(mismatches)),
            )
    if expected.get("platform"):
        actual_platform = constraints.get("platform_key") or ""
        add_check(
            "플랫폼 인식",
            normalize_for_match(actual_platform) == normalize_for_match(expected["platform"]),
            actual_platform or "미인식",
        )
    if expected.get("platformExclude"):
        excluded = constraints.get("excluded_platform_keys", [])
        expected_platform = normalize_for_match(expected["platformExclude"])
        add_check(
            "플랫폼 제외 인식",
            expected_platform in {normalize_for_match(value) for value in excluded},
            ", ".join(excluded) or "미인식",
        )
        mismatched_ids = []
        for source in sources:
            if source.get("type") not in {"mantis", "chroma"}:
                continue
            detail = source.get("detail") or {}
            media_type = str(detail.get("media_type") or "")
            if media_type and normalize_for_match(media_type) == expected_platform:
                mismatched_ids.append(str(source.get("issue_id") or "?"))
        add_check(
            "제외 플랫폼 결과 일치",
            not mismatched_ids,
            "모두 제외됨" if not mismatched_ids else f"포함된 ID: {', '.join(mismatched_ids)}",
        )
    if expected.get("screenNumber"):
        actual = constraints.get("screen_numbers", [])
        add_check("화면번호 인식", expected["screenNumber"] in actual, ", ".join(actual) or "미인식")
        mantis_sources = [source for source in sources if source.get("type") in {"mantis", "chroma"}]
        if mantis_sources:
            mismatched_ids = []
            for source in mantis_sources:
                detail = source.get("detail") or {}
                screen_field = str(detail.get("screen_number") or "")
                title = str(source.get("title") or "")
                if not (
                    field_matches_screen_number(screen_field, expected["screenNumber"])
                    or title_has_screen_number(title, expected["screenNumber"])
                ):
                    mismatched_ids.append(str(source.get("issue_id") or "?"))
            add_check(
                "화면번호 결과 정확 일치",
                not mismatched_ids,
                "모두 일치" if not mismatched_ids else f"불일치 ID: {', '.join(mismatched_ids)}",
            )
    if expected.get("person"):
        actual_people = constraints.get("people", {}).get("names", [])
        add_check(
            "담당자 인식",
            expected["person"] in actual_people,
            ", ".join(actual_people) or "미인식",
        )
        mantis_sources = [source for source in sources if source.get("type") in {"mantis", "chroma"}]
        mismatched_ids = [
            str(source.get("issue_id") or "?")
            for source in mantis_sources
            if normalize_for_match(source.get("handler", "")) != normalize_for_match(expected["person"])
        ]
        if mantis_sources:
            add_check(
                "담당자 결과 일치",
                not mismatched_ids,
                "모두 일치" if not mismatched_ids else f"불일치 ID: {', '.join(mismatched_ids)}",
            )
    if expected.get("issueId"):
        actual_ids = {int(source["issue_id"]) for source in sources if source.get("issue_id")}
        add_check("정확 ID 조회", expected["issueId"] in actual_ids, ", ".join(map(str, sorted(actual_ids))) or "조회 없음")
    if expected.get("sourceIds"):
        actual_ids = {int(source["issue_id"]) for source in sources if source.get("issue_id")}
        required_ids = {int(issue_id) for issue_id in expected["sourceIds"]}
        missing_ids = sorted(required_ids - actual_ids)
        add_check(
            "기대 이슈 포함",
            not missing_ids,
            "모두 포함" if not missing_ids else f"누락: {', '.join(map(str, missing_ids))}",
        )
    if expected.get("source"):
        actual_types = {source.get("type") for source in sources}
        add_check("데이터 소스", expected["source"] in actual_types, ", ".join(sorted(actual_types)) or "조회 없음")
    if expected.get("titleContains"):
        titles = [source.get("title", "") for source in sources]
        matched_title = next(
            (title for title in titles if normalize_for_match(expected["titleContains"]) in normalize_for_match(title)),
            "",
        )
        add_check("제목 일치", bool(matched_title), matched_title or "일치 제목 없음")
    if expected.get("databaseLabel"):
        database_labels = [source.get("database_label", "") for source in sources]
        matched_database = next(
            (
                label
                for label in database_labels
                if normalize_for_match(expected["databaseLabel"]) in normalize_for_match(label)
            ),
            "",
        )
        add_check("Notion DB", bool(matched_database), matched_database or "일치 DB 없음")
    if expected.get("effortTotal") is not None:
        effort_meta = (retrieval_result.get("stages") or [{}])[0].get("meta", {})
        actual_total = float(effort_meta.get("total_effort", 0))
        add_check(
            "공수 합계",
            abs(actual_total - float(expected["effortTotal"])) < 0.0001,
            f"{actual_total:.2f}",
        )
    if expected.get("issueCount") is not None:
        add_check(
            "반영 이슈 수",
            len(sources) == int(expected["issueCount"]),
            f"{len(sources)}건",
        )
    if expected.get("totalCount") is not None:
        total_count = int((retrieval_result.get("stages") or [{}])[0].get("meta", {}).get("total_count", -1))
        add_check(
            "전체 건수",
            total_count == int(expected["totalCount"]),
            f"{total_count}건" if total_count >= 0 else "집계 없음",
        )
    add_check("최소 결과 수", len(sources) >= expected.get("minResults", 1), f"{len(sources)}건")
    passed = all(check["passed"] for check in checks)
    return {
        **case,
        "passed": passed,
        "checks": checks,
        "sourceCount": len(sources),
        "trace": retrieval_trace(retrieval_result),
        "durationMs": round((time.perf_counter() - started) * 1000),
    }


def run_evaluation_case_safely(case: Dict[str, Any]) -> Dict[str, Any]:
    # 케이스 하나의 예외가 전체 실행을 500으로 죽이지 않도록 실패 결과로 감싼다.
    started = time.perf_counter()
    try:
        return run_evaluation_case(case)
    except Exception as exc:
        return {
            **case,
            "passed": False,
            "checks": [{"label": "실행 오류", "passed": False, "actual": f"{type(exc).__name__}: {exc}"}],
            "sourceCount": 0,
            "trace": [],
            "durationMs": round((time.perf_counter() - started) * 1000),
        }


def build_evaluation_summary(results: List[Dict[str, Any]], duration_ms: int) -> Dict[str, Any]:
    passed_count = sum(1 for result in results if result["passed"])
    return {
        "total": len(results),
        "passed": passed_count,
        "failed": len(results) - passed_count,
        "score": round(passed_count / len(results) * 100) if results else 0,
        "runAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "durationMs": duration_ms,
    }


def save_evaluation_run(summary: Dict[str, Any], results: List[Dict[str, Any]]) -> str:
    run_id = uuid.uuid4().hex[:12]
    init_audit_db()
    with closing(sqlite3.connect(AUDIT_DB_PATH)) as connection, connection:
        connection.execute(
            "INSERT INTO evaluation_runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                summary["runAt"],
                summary["total"],
                summary["passed"],
                summary["failed"],
                summary["score"],
                summary["durationMs"],
                json.dumps(results, ensure_ascii=False),
            ),
        )
    return run_id


def build_accuracy_analytics(report_data: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Summarize score movement and recurring failure categories (newest first)."""
    if not report_data:
        return {"trend": [], "latestDelta": None, "failureCounts": []}
    trend = [
        {
            "runAt": data.get("run_at"),
            "score": (
                int((data.get("summary") or {}).get("score"))
                if (data.get("summary") or {}).get("score") is not None
                else None
            ),
            "passed": int((data.get("summary") or {}).get("passed") or 0),
            "total": int((data.get("summary") or {}).get("total") or 0),
            "status": (data.get("summary") or {}).get("status", "unknown"),
        }
        for data in reversed(report_data)
    ]
    scored_reports = [
        data for data in report_data if (data.get("summary") or {}).get("score") is not None
    ]
    latest_score = (
        int((scored_reports[0].get("summary") or {}).get("score"))
        if scored_reports
        else None
    )
    previous_score = (
        int((scored_reports[1].get("summary") or {}).get("score"))
        if len(scored_reports) > 1
        else None
    )
    failure_counts: Dict[str, int] = {}
    for data in report_data:
        for result in data.get("results") or []:
            if result.get("passed") or result.get("outcome") in {"dependency_error", "execution_error"}:
                continue
            category = str(result.get("category") or "미분류")
            failure_counts[category] = failure_counts.get(category, 0) + 1
    return {
        "trend": trend,
        "latestDelta": (
            latest_score - previous_score
            if latest_score is not None and previous_score is not None
            else None
        ),
        "previousScore": previous_score,
        "failureCounts": [
            {"category": category, "count": count}
            for category, count in sorted(
                failure_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ],
    }
