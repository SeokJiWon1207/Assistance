"""검색 근거와 제약 조건의 일치 여부를 검증한다."""

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from backend.answers import display_constraints
from backend.common import normalize_for_match
from backend.query_planning import plan_created_at_matches, validate_query_plan
from backend.reports.effort import EFFORT_FIELD_LABELS, parse_effort_field_value
from backend.retrieval import (
    build_model_retrieval_context,
    build_sources,
    retrieval_trace,
    similar_base_issue_ids_from_retrieval,
)
from backend.schemas import ChatRequest
from backend.understanding import (
    extract_query_constraints,
    extract_requested_limit,
    has_constraint,
    text_matches_constraints,
    text_matches_people,
    text_matches_screen_numbers,
    text_matches_status,
)


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
    intent_checks = cast(List[Dict[str, str]], intent_validation["checks"])
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
        effort_stage: Dict[str, Any] = next(
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
        count_stage: Dict[str, Any] = next(
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
    """기존 공개 경로를 유지하며 비교 답변 구현을 지연 호출한다."""
    from backend.evaluation.core import generate_basic_grounded_baseline as generate

    return generate(request, question, retrieval_result)


__all__ = [
    "build_basic_grounding",
    "build_verification_report",
    "generate_basic_grounded_baseline",
    "ungrounded_constraint_labels",
]
