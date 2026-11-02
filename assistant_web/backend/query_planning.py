"""Structured query planning helpers for tool-backed chat requests.

The language model may enrich a plan, but these helpers keep critical intent
such as date ranges and count operations recoverable and independently
verifiable when the planner is unavailable or returns an incomplete payload.
"""

from __future__ import annotations

import calendar as calendar
import re
from datetime import date as date, timedelta as timedelta
from typing import Any as Any, Dict as Dict, List as List, Optional as Optional, Tuple as Tuple

from backend.query_filters import (
    NEGATIVE_OPERATORS,
    add_filter_clause,
    normalize_filter_clauses,
)


from backend.query.operations import (
    COUNT_MARKERS as COUNT_MARKERS,
    SUM_MARKERS as SUM_MARKERS,
    SIMILAR_MARKERS as SIMILAR_MARKERS,
    GROUP_BY_PATTERNS as GROUP_BY_PATTERNS,
    normalized_text as normalized_text,
    infer_operation as infer_operation,
    extract_group_by as extract_group_by,
    has_count_intent as has_count_intent,
)


from backend.query.dates import (
    _safe_date as _safe_date,
    _ISO_DATE_PATTERN as _ISO_DATE_PATTERN,
    has_invalid_month_expression as has_invalid_month_expression,
    extract_created_at_range as extract_created_at_range,
    extract_yearless_month_range as extract_yearless_month_range,
    extract_yearless_temporal_month_range as extract_yearless_temporal_month_range,
    extract_relative_year_range as extract_relative_year_range,
    _AGING_SUFFIX as _AGING_SUFFIX,
    _KOREAN_SMALL_NUMBERS as _KOREAN_SMALL_NUMBERS,
    _months_before as _months_before,
    _extract_aging_created_at_range as _extract_aging_created_at_range,
    extract_relative_created_at_range as extract_relative_created_at_range,
    _week_bounds as _week_bounds,
    _TEMPORAL_EXPRESSION_PATTERN as _TEMPORAL_EXPRESSION_PATTERN,
    question_has_temporal_expression as question_has_temporal_expression,
)


def _clause_contradicts_filters(clause: Dict[str, Any], filters: Dict[str, Any]) -> bool:
    """규칙이 잡은 긍정 조건을 그대로 다시 제외하는 절인지 판별한다."""
    if clause.get("operator") not in NEGATIVE_OPERATORS:
        return False
    field = clause.get("field")
    positive_key = {"platform": "platform", "customer": "customer"}.get(field) if isinstance(field, str) else None
    positive = filters.get(positive_key) if positive_key else None
    if not positive:
        return False
    values = clause.get("value")
    values = values if isinstance(values, list) else [values]
    normalized_positive = str(positive).strip().casefold()
    return any(str(value).strip().casefold() == normalized_positive for value in values)


def build_query_plan(
    question: str,
    source_plan: Optional[Dict[str, Any]] = None,
    constraints: Optional[Dict[str, Any]] = None,
    model_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    source_plan = source_plan or {}
    constraints = constraints or {}
    model_plan = model_plan or {}
    raw_model_filters = model_plan.get("filters")
    model_filters: Dict[str, Any] = raw_model_filters if isinstance(raw_model_filters, dict) else {}
    model_filter_clauses = normalize_filter_clauses(
        model_plan.get("filter_clauses")
        or (model_plan.get("filters") if isinstance(model_plan.get("filters"), list) else [])
    )
    detected_created_at = constraints.get("created_at") or extract_created_at_range(question)
    model_created_at = model_filters.get("created_at") or model_plan.get("created_at")
    if not model_created_at:
        for clause in model_filter_clauses:
            if clause["field"] == "created_at" and clause["operator"] == "between":
                values = clause.get("value") or []
                if len(values) == 2:
                    model_created_at = {"from": str(values[0]), "to": str(values[1]), "precision": "range"}
                    break
    if model_created_at and not detected_created_at and not question_has_temporal_expression(question):
        # 질문에 시점 표현이 없는데 모델만 날짜 범위를 내놓은 경우 → 환각으로 간주하고 버린다.
        model_created_at = None
    if has_invalid_month_expression(question):
        # "2026년 13월"은 연도가 있어 시점 표현으로는 잡히므로 위 규칙을 통과한다.
        # 규칙이 해석을 거부한 잘못된 월을 모델이 지어낸 범위로 메우지 않도록 막는다.
        model_created_at = None
    created_at = detected_created_at or model_created_at
    if not isinstance(created_at, dict) or not created_at.get("from") or not created_at.get("to"):
        created_at = None

    operation = str(model_plan.get("operation") or model_plan.get("intent") or "").lower()
    if operation not in {"search", "count", "get", "aggregate"}:
        operation = infer_operation(question)
    deterministic_operation = infer_operation(question)
    explicit_list_limit = bool(re.search(r"\d+\s*(?:개|건)\s*만?\s*(?:보여|알려|찾아|조회)", question))
    if deterministic_operation != "search" or explicit_list_limit:
        operation = deterministic_operation

    source = str(model_plan.get("source") or source_plan.get("source") or "general").lower()
    if source not in {"mantis", "notion", "both", "general"}:
        source = str(source_plan.get("source") or "general")

    requested_limit = model_plan.get("limit")
    if not isinstance(requested_limit, int) or requested_limit <= 0:
        requested_limit = None

    deterministic_group_by = extract_group_by(question)
    model_group_by = str(model_plan.get("group_by") or "").strip().lower()
    group_by = deterministic_group_by or (model_group_by if model_group_by in GROUP_BY_PATTERNS else None)

    filters = {
        "customer": constraints.get("customer_exact") or constraints.get("customer_key"),
        "platform": constraints.get("platform_key"),
        "statuses": list(constraints.get("statuses") or []),
        "people": dict(constraints.get("people") or {}),
        "issue_ids": list(constraints.get("issue_ids") or []),
        "screen_numbers": list(constraints.get("screen_numbers") or []),
        "date_fields": dict(constraints.get("date_fields") or {}),
        "created_at": created_at,
    }
    # 규칙이 뽑은 상태 조건이 있으면 사용자가 상태를 언급한 것이다.
    question_mentions_status = bool(
        constraints.get("statuses") or constraints.get("excluded_statuses")
    )
    filter_clauses = normalize_filter_clauses(constraints.get("filter_clauses") or [])
    from backend.intent.keyword_terms import build_keyword_terms
    structured_topic_free = bool(
        source in {"mantis", "both"}
        and filters["customer"]
        and created_at
        and not build_keyword_terms(question, constraints)
        and not any(clause["field"] in {"title", "keyword"} for clause in filter_clauses)
    )
    if group_by == "customer":
        model_filter_clauses = [
            clause for clause in model_filter_clauses if clause["field"] != "customer"
        ]
    for clause in model_filter_clauses:
        if structured_topic_free and clause["field"] in {"title", "keyword"}:
            # 고객사·기간만 지정한 조회에 모델이 설명/표시 문구를 필수
            # 검색어로 덧붙이면 정상 항목이 모두 탈락한다.
            continue
        if clause["field"] == "created_at" and (
            detected_created_at
            or not question_has_temporal_expression(question)
            or has_invalid_month_expression(question)
        ):
            # 규칙으로 날짜를 확정했으면 모델이 만든 별도 범위를 겹치지 않는다.
            # 두 범위가 함께 남으면 교집합만 검색해 정상 항목을 조용히 누락한다.
            # 질문에 시점 표현이 없거나 월 표현이 잘못된 경우도 모델 날짜를 버린다.
            continue
        if clause["field"] == "status" and (not question_mentions_status or source in {"mantis", "both"}):
            # 질문에 상태 표현이 없는데 모델이 "종료 제외" 같은 필터를 붙이면
            # 사용자가 요청한 개수보다 훨씬 적은 결과가 조용히 나간다.
            # Mantis 상태는 규칙이 확정한다. 개발완료에 모델의 resolved를
            # 겹쳐 붙여 교집합이 0건이 되는 경우도 차단한다.
            continue
        if clause["field"] == "platform" and constraints.get("platform_key"):
            # 별칭을 이미 정규화했으므로 MTS AND '모바일' 같은 중복 조건을 금지한다.
            continue
        if _clause_contradicts_filters(clause, filters):
            # 규칙이 이미 조건으로 잡은 값을 모델이 다시 제외하면(예: 플랫폼=MTS인데
            # 플랫폼 제외=MTS) 결과가 항상 0건이 된다. 사용자가 직접 말한 조건이
            # 우선이므로 모순되는 모델 절은 버린다.
            continue
        if clause not in filter_clauses:
            filter_clauses.append(clause)
    from backend.query_filters import notion_temporal_field
    temporal_field = notion_temporal_field(question) if source == "notion" else "created_at"
    temporal_date_range = created_at
    if temporal_field != "created_at":
        # 마감·배포일은 규칙으로 질문과 직접 연결된 기간만 허용한다. 모델이
        # 제목 속 월 표현을 날짜 조건으로 바꾸는 경우도 여기서 함께 차단한다.
        temporal_date_range = detected_created_at or extract_yearless_temporal_month_range(
            question, temporal_field,
        )
        filters["created_at"] = None
        filter_clauses = [clause for clause in filter_clauses if clause["field"] != "created_at"]
    # 날짜 종류를 모델이 임의로 추가하지 않도록 질문의 표현과 맞춘다.
    filter_clauses = [
        clause for clause in filter_clauses
        if clause["field"] not in {"due_at", "deployed_at"}
    ]
    if temporal_date_range:
        add_filter_clause(
            filter_clauses,
            temporal_field,
            "between",
            [temporal_date_range["from"], temporal_date_range["to"]],
        )
    if source == "notion":
        from backend.intent.notion_constraints import extract_notion_constraints
        native = extract_notion_constraints(question)
        if native.get("status") or native.get("excluded_statuses"):
            filters["statuses"] = [native["status"]] if native.get("status") else []
            filter_clauses = [clause for clause in filter_clauses if clause["field"] != "status"]
            for operator, values in (("in", filters["statuses"]), ("not_in", native.get("excluded_statuses") or [])):
                if values:
                    add_filter_clause(filter_clauses, "status", operator, values)
    terms = model_plan.get("terms") or source_plan.get("terms") or []
    if structured_topic_free:
        terms = []
    resolution = constraints.get("status_resolution") or {}
    if resolution.get("status_intent") == "open" and resolution.get("evidence"):
        evidence = re.sub(r"\s+", "", resolution["evidence"]).casefold()
        terms = [term for term in terms if re.sub(r"\s+", "", str(term)).casefold() not in evidence]
    return {
        "version": 2,
        "grounding_text": question,
        "status_resolution": constraints.get("status_resolution"),
        "followup_effort": constraints.get("followup_effort"),
        "operation": operation,
        "source": source,
        "filters": filters,
        "filter_clauses": filter_clauses,
        "sort": model_plan.get("sort") or {"field": "created_at", "direction": "desc"},
        "group_by": group_by,
        "limit": requested_limit,
        "terms": [str(term) for term in terms if str(term).strip()][:8],
        "confidence": float(model_plan.get("confidence") or source_plan.get("confidence") or 0.7),
        "planner": "model" if model_plan.get("planned") else source_plan.get("planner", "rule"),
        "reason": str(model_plan.get("reason") or source_plan.get("reason") or "")[:160],
    }


def validate_query_plan(question: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    if plan.get("planner") == "unified_llm":
        from backend.intent.execution_contract import validate_execution_contract
        return validate_execution_contract(question, plan)
    checks: List[Dict[str, str]] = []
    raw_filters = plan.get("filters")
    filters: Dict[str, Any] = raw_filters if isinstance(raw_filters, dict) else {}
    raw_created_at = filters.get("created_at")
    created_at = raw_created_at if isinstance(raw_created_at, dict) else None

    if plan.get("operation") == "explain":
        from backend.intent.explanation import explanation_date_text
        expected_date = extract_created_at_range(explanation_date_text(question))
    else:
        expected_date = extract_created_at_range(question)
    resolved = plan.get("resolved_constraints") or {}
    if isinstance(resolved, dict) and resolved.get("inherited_from_audit_id"):
        from backend.intent.scope_changes import followup_created_at
        previous_range = resolved.get("followup_base_created_at")
        if isinstance(previous_range, dict):
            expected_date = followup_created_at({"created_at": previous_range}, question) or expected_date
    if plan.get("source") == "notion":
        from backend.query_filters import notion_temporal_field
        if notion_temporal_field(question) != "created_at":
            expected_date = expected_date or extract_yearless_temporal_month_range(
                question, notion_temporal_field(question),
            )
    if expected_date:
        from backend.query_filters import notion_temporal_field
        temporal_field = notion_temporal_field(question) if plan.get("source") == "notion" else "created_at"
        passed = bool(
            created_at
            and created_at.get("from") == expected_date["from"]
            and created_at.get("to") == expected_date["to"]
        )
        if temporal_field != "created_at":
            passed = any(
                clause["field"] == temporal_field and clause["operator"] == "between"
                and clause.get("value") == [expected_date["from"], expected_date["to"]]
                for clause in normalize_filter_clauses(plan.get("filter_clauses", []))
            )
        checks.append({
            "id": "date_intent",
            "label": {"created_at": "등록일 기간 해석", "due_at": "기한 기간 해석", "deployed_at": "운영반영일 기간 해석"}[temporal_field],
            "status": "pass" if passed else "fail",
            "detail": (
                f"{expected_date['from']}~{expected_date['to']}"
                if passed
                else "질문의 기간 조건이 실행 계획에 정확히 반영되지 않음"
            ),
        })

    explanation_scope = None
    if plan.get("operation") == "explain":
        from backend.intent.explanation import explanation_constraints, wants_issue_explanation
        from backend.intent.constraints import extract_query_constraints
        parsed = extract_query_constraints(question)
        if resolved.get("inherited_from_audit_id"):
            parsed = {**resolved, **{k: v for k, v in parsed.items() if v}}
        if wants_issue_explanation(question, parsed):
            explanation_scope = explanation_constraints(question, parsed)
    expected_operation = "aggregate" if plan.get("followup_effort") else infer_operation(question)
    if explanation_scope is not None:
        expected_operation = "explain"
    operation_passed = plan.get("operation") == expected_operation
    checks.append({
        "id": "operation_intent",
        "label": "요청 작업 해석",
        "status": "pass" if operation_passed else "fail",
        "detail": f"요청={expected_operation} · 계획={plan.get('operation', '미상')}",
    })

    source = plan.get("source")
    source_passed = source in {"mantis", "notion", "both", "general"}
    checks.append({
        "id": "source_intent",
        "label": "검색 소스 해석",
        "status": "pass" if source_passed else "fail",
        "detail": f"소스={source or '미상'}",
    })
    clauses = normalize_filter_clauses(plan.get("filter_clauses") or [])
    if source in {"mantis", "both"}:
        from backend.intent.entities import extract_status_constraints
        expected_statuses = set(explanation_scope["statuses"] if explanation_scope is not None
                                else extract_status_constraints(question))
        from backend.intent.status_semantics import ALL_STATUSES, resolution_is_grounded
        if ALL_STATUSES.search(question) and not expected_statuses - {"open", "closed"}:
            expected_statuses = set()
        resolution = plan.get("status_resolution") or {}
        if not expected_statuses and resolution_is_grounded(resolution):
            expected_statuses = {"open"}
        if expected_statuses:
            planned_statuses = set(filters.get("statuses") or [])
            clause_statuses: set[str] = set()
            for clause in clauses:
                if clause["field"] == "status" and clause["operator"] in {"equals", "in"}:
                    values = clause["value"] if isinstance(clause["value"], list) else [clause["value"]]
                    clause_statuses.update(str(value) for value in values)
            status_passed = expected_statuses == planned_statuses == clause_statuses
            checks.append({
                "id": "status_intent", "label": "상태 조건 해석",
                "status": "pass" if status_passed else "fail",
                "detail": f"요청={','.join(sorted(expected_statuses))} · 계획={','.join(sorted(planned_statuses)) or '없음'}",
            })
    raw_clauses = plan.get("filter_clauses") or []
    schema_passed = len(clauses) == len(raw_clauses) if isinstance(raw_clauses, list) else False
    checks.append({
        "id": "filter_schema",
        "label": "필터 스키마 검증",
        "status": "pass" if schema_passed else "fail",
        "detail": f"검증된 조건={len(clauses)}개",
    })
    from backend.intent.scope_changes import cleared_scope_fields, without_scope_clear_phrases
    cleared = cleared_scope_fields(question)
    if cleared:
        active = {"customer": filters.get("customer"), "platform": filters.get("platform"),
                  "owner": (filters.get("people") or {}).get("names"), "created_at": filters.get("created_at")}
        uncleared = [field for field in cleared if active.get(field) or any(
            c["field"] in ({"owner", "reporter"} if field == "owner" else {field}) for c in clauses)]
        checks.append({"id": "scope_reset", "label": "조건 해제 반영", "status": "fail" if uncleared else "pass",
                       "detail": "해제할 조건이 남아 있음: " + ", ".join(uncleared) if uncleared else "요청한 조건만 해제됨"})
    negative_text = without_scope_clear_phrases(question)
    if explanation_scope is not None:
        from backend.intent.explanation import explanation_filter_text
        negative_text = explanation_filter_text(negative_text)
    has_negative_phrase = bool(re.search(r"빼고|제외|아닌|말고|없이|미포함|없는", negative_text))
    if has_negative_phrase:
        negative_passed = any(
            clause["operator"] in {"not_equals", "not_in", "not_contains", "not_exists"}
            for clause in clauses
        )
        checks.append({
            "id": "negative_intent",
            "label": "제외 조건 해석",
            "status": "pass" if negative_passed else "fail",
            "detail": "제외 연산자 반영" if negative_passed else "제외 표현이 포함 조건으로 뒤집힘",
        })
    return {
        "valid": all(check["status"] == "pass" for check in checks),
        "checks": checks,
    }


from backend.query.dates import (
    plan_created_at_matches as plan_created_at_matches,
)