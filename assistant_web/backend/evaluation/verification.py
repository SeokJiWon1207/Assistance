"""검색 근거와 제약 조건의 일치 여부를 검증한다."""

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, cast

from backend.answers import display_constraints
from backend.effort_fields import issue_effort_people, issue_delivery_date
from backend.query_filters import matches_source_filters
from backend.common import normalize_for_match
from backend.mantis_status import source_status
from backend.notion.search import extract_notion_constraints, notion_page_matches_constraints
from backend.query_planning import plan_created_at_matches, validate_query_plan, infer_operation, extract_group_by
from backend.reports.effort import EFFORT_FIELD_LABELS, parse_effort_field_value
from backend.retrieval import (
    build_model_retrieval_context,
    build_model_evidence_sources,
    count_retrieved_issues,
    retrieval_trace,
    similar_base_issue_ids_from_retrieval,
)
from backend.schemas import ChatRequest
from backend.evaluation.issue_facts import check_issue_fields, extract_issue_claims, has_definite_scope_mismatch, plain_text
from backend.evaluation.report_scopes import report_evidence_constraints
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
    source: str = "",
    *, query_plan: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """질문에 근거가 없는 검색 조건을 찾는다.

    기존 검증은 계획과 결과가 서로 맞는지만 봐서, 모델이 만들어낸 고객사·
    담당자 필터가 조용히 적용돼도 전부 통과했다. 조건이 실제로 사용자가 한
    말에서 나왔는지를 따로 확인한다.

    근거 텍스트로는 검색에 실제 쓰인 문장을 쓴다. 후속 질문에서 이전 턴의
    조건을 물려받은 경우까지 근거 없음으로 잡히면 경고가 무의미해진다.
    """
    from backend.intent.condition_grounding import grounded_values
    from backend.config import CUSTOMER_ALIASES, PLATFORM_ALIASES
    certified = grounded_values(grounding_text, constraints, query_plan)
    def grounded(field, value):
        # 서버가 검증한 정식 명칭과 실행용 키를 같은 정적 별칭 집합으로 대조한다.
        # 모델이 제공한 임의 aliases는 인증 근거를 확장하는 데 사용하지 않는다.
        aliases = {'customer': CUSTOMER_ALIASES, 'platform': PLATFORM_ALIASES}.get(field, {})
        return any((field, normalize_for_match(str(alias).lstrip('#'))) in certified
                   for alias in [value, *aliases.get(value, [])])
    normalized = normalize_for_match(grounding_text)
    labels: List[str] = []

    customer_key = constraints.get("customer_key")
    if customer_key and not grounded("customer", customer_key) and not any(
        normalize_for_match(alias) in normalized
        for alias in [customer_key, *(constraints.get("customer_aliases") or [])]
        if alias
    ):
        labels.append(f"고객사={customer_key}")

    platform_key = constraints.get("platform_key")
    if platform_key and not grounded("platform", platform_key) and not any(
        normalize_for_match(alias) in normalized
        for alias in [platform_key, *(constraints.get("platform_aliases") or [])]
        if alias
    ):
        labels.append(f"플랫폼={platform_key}")

    for name in constraints.get("people", {}).get("names") or []:
        role = constraints.get("people", {}).get("role")
        field = "owner" if role == "handler" else role
        if not grounded(field, name) and normalize_for_match(name) not in normalized:
            labels.append(f"담당자={name}")

    for number in constraints.get("screen_numbers") or []:
        if not grounded("screen_number", number) and str(number) not in str(grounding_text):
            labels.append(f"화면번호={number}")

    expected = extract_query_constraints(grounding_text)
    from backend.intent.status_semantics import resolution_is_grounded
    if source in {"mantis", "both"} and resolution_is_grounded(constraints.get("status_resolution") or {}):
        if not expected.get("statuses"):
            expected["statuses"] = ["open"]
    if source == "notion":
        native = extract_notion_constraints(grounding_text)
        expected["statuses"] = [native["status"]] if native.get("status") else []
    for status in constraints.get("statuses") or []:
        if not grounded("status", status) and status not in (expected.get("statuses") or []):
            labels.append(f"상태={status}")
    for issue_id in constraints.get("issue_ids") or []:
        if not grounded("issue_id", issue_id) and issue_id not in (expected.get("issue_ids") or []):
            labels.append(f"이슈 ID={issue_id}")

    # 집계값과 실행 필터가 서로 일치해도, 질문에 없던 검색어로 범위를
    # 좁혔다면 올바른 답변이 아니다. 특히 0건은 대조할 원문도 없으므로
    # 제목/본문 필터와 상속 검색어의 근거를 원문 유무와 무관하게 확인한다.
    keyword_values = [("검색어", value) for value in constraints.get("inherited_terms") or []]
    for clause in constraints.get("filter_clauses") or []:
        if clause.get("field") not in {"keyword", "title"}:
            continue
        values = clause.get("value")
        if not isinstance(values, list):
            values = [values]
        label = "제목" if clause["field"] == "title" else "검색어"
        keyword_values.extend((label, value) for value in values)
    for label, value in keyword_values:
        term = normalize_for_match(str(value or ""))
        missing_label = f"{label}={value}"
        field = "title" if label == "제목" else "keyword"
        if term and not grounded(field, value) and term not in normalized and missing_label not in labels:
            labels.append(missing_label)

    return labels


def build_verification_report(
    question: str,
    content: str,
    sources: List[Dict[str, Any]],
    retrieval_result: Optional[Dict[str, Any]],
    mode: str,
) -> Dict[str, Any]:
    if mode == "deterministic_work_report":
        from backend.reports.work_queries import verify_work_report
        return verify_work_report(question, content, sources, retrieval_result or {})
    constraints = (retrieval_result or {}).get("constraints") or extract_query_constraints(question)
    query_plan = (retrieval_result or {}).get("query_plan") or (retrieval_result or {}).get("intent_plan") or {}
    if mode in {"status_clarification", "search_terms_clarification", "unified_plan_clarification"}:
        resolution = query_plan.get("status_resolution") or {}
        label = "검색어 확인 대기" if mode == "search_terms_clarification" else "상태 확인 대기"
        detail = (
            "이전 대화의 검색어 조건을 확정하지 못해 조회·집계를 실행하지 않음"
            if mode == "search_terms_clarification"
            else f"{resolution.get('origin', 'unknown')} · {resolution.get('reason', '')} · 상태를 확정하지 못해 조회·집계를 실행하지 않음"
        )
        check = {"id": mode, "label": label, "status": "warning", "detail": detail}
        return {"score": 0, "status": "warning", "checks": [check],
            "dimensions": [{"id": "intent", "label": label, "score": None, "status": "warning", "checks": [check]}],
            "completedDimensions": 0, "applicableDimensions": 1, "constraints": display_constraints(constraints),
            "queryPlan": query_plan, "trace": [], "sourceCount": 0, "mode": mode,
            "verifiedAt": datetime.now().astimezone().isoformat(), "note": "사용자 확인 전에는 결과를 확정하지 않습니다."}
    retrieval_checks: List[Dict[str, str]] = []
    if query_plan.get("followup_effort") or constraints.get("followup_effort"):
        # 실행 계획의 작업명이 맞아도 목록 응답으로 우회하면 공수 요청은 미충족이다.
        # 아래 계산 검증을 실제로 수행하는 경로만 인정한다.
        effort_executed = (query_plan.get("operation") == "aggregate" and mode in {
            "deterministic_effort_report", "deterministic_filtered_effort_report",
        })
        retrieval_checks.append({"id": "followup_effort_result", "label": "후속 공수 요청 이행",
            "status": "pass" if effort_executed else "fail",
            "detail": "직전 공수 요청을 이어받아 원문 재합산 검증 수행" if effort_executed
                      else "직전 공수 요청을 이어받았으나 공수 집계 결과가 제공되지 않음"})
    if constraints.get("unconfirmed_search_terms"):
        retrieval_checks.append({"id": "unconfirmed_search_terms", "label": "검색어 조건 확인", "status": "warning",
            "detail": "이전 대화의 검색어 조건이 아직 확정되지 않아 결과를 검증할 수 없음"})
    resolution = query_plan.get("status_resolution") or constraints.get("status_resolution") or {}
    if resolution:
        from backend.intent.status_semantics import resolution_is_grounded
        status = "warning" if resolution.get("needs_clarification") else "pass"
        if resolution.get("status_intent") == "open" and not resolution_is_grounded(resolution):
            status = "fail"
        retrieval_checks.append({"id": "status_resolution", "label": "상태 해석 경로", "status": status,
            "detail": f"{resolution.get('origin')} · {resolution.get('reason')} · 근거: {resolution.get('evidence') or '명시 조건/상태 제한 없음'}"})
    retrieval_used = bool(retrieval_result)
    successful_count_stage = next(
        (
            stage for stage in (retrieval_result or {}).get("stages", [])
            if stage.get("status") == "ok" and stage.get("meta", {}).get("operation") == "count"
        ),
        None,
    )
    dependency_failed = any(
        stage.get("status") in {"error", "partial_error"}
        and stage.get("error_kind") == "dependency"
        for stage in (retrieval_result or {}).get("stages", [])
    )
    completed_empty_search = any(
        (stage.get("status") == "ok" or (
            stage.get("source") == "mantis_id_lookup" and stage.get("status") == "not_found"
        ))
        and stage.get("source") in {
            "notion", "mantis_id_lookup", "mantis_filter", "mantis_page_scan",
            "mantis_snapshot", "mantis_sla", "chroma_db",
        }
        and stage.get("meta", {}).get("search_complete") is True
        and stage.get("meta", {}).get("coverage") not in {"saved_filter", "top_k", "snapshot_matches"}
        for stage in (retrieval_result or {}).get("stages", [])
    )
    from backend.evaluation.effort_trend import trend_checks
    monthly_checks = trend_checks(content, retrieval_result) if mode == 'deterministic_effort_trend' else []
    verified_monthly = bool(monthly_checks) and all(c['status'] == 'pass' for c in monthly_checks)
    has_evidence = bool(
        sources or successful_count_stage or verified_monthly or not retrieval_used or completed_empty_search
    )
    evidence_status = (
        "warning"
        if dependency_failed and not sources
        else "pass"
        if has_evidence
        else "fail"
    )
    retrieval_checks.append(
        {
            "id": "evidence",
            "label": "원문 근거",
            "status": evidence_status,
            "detail": (
                f"연결된 원문 {len(sources)}건"
                if sources
                else "집계 실행 완료"
                if successful_count_stage or verified_monthly
                else "외부 검색 오류로 원문 확인 미완료"
                if dependency_failed
                else "검색 완료 · 일치 항목 없음"
                if completed_empty_search
                else "일반 답변"
                if not retrieval_used
                else "연결된 원문 없음"
            ),
        }
    )

    # 날짜 소수부·전체 저장본 건수를 ID로 오인하지 않는다.
    answer_ids, field_claims = extract_issue_claims(
        content or "", source_sections=mode == "deterministic_issue_list",
        source_quotes=tuple(plain_text(text) for source in sources
                            for text in [(source.get("detail") or {}).get("description", ""),
                                         *[excerpt.get("text", "") for excerpt in
                                           ((source.get("detail") or {}).get("answer_evidence") or {}).get("excerpts", [])],
                                         *[note.get("text", "") for note in (source.get("detail") or {}).get("notes", [])]]
                            if text),
    )
    source_ids = {int(source["issue_id"]) for source in sources if source.get("issue_id")}
    similar_base_issue_ids = set(similar_base_issue_ids_from_retrieval(retrieval_result or {}))
    unsupported_ids = sorted(answer_ids - source_ids)
    unsupported_ids = [issue_id for issue_id in unsupported_ids if issue_id not in similar_base_issue_ids]
    if completed_empty_search and not sources:
        # 조회 실패 안내에서 사용자가 요청한 ID를 반복하는 것은 사실 생성이 아니다.
        requested_ids = set(extract_query_constraints(question).get("issue_ids") or [])
        if re.search(r"찾지 못|없습니다|없어요", content or ""):
            unsupported_ids = [issue_id for issue_id in unsupported_ids if issue_id not in requested_ids]
    retrieval_checks.append(
        {
            "id": "issue_ids",
            "label": "이슈 ID 일치",
            "status": "pass" if not unsupported_ids else "fail",
            "detail": "모든 ID가 원문과 일치" if not unsupported_ids else f"근거 없는 ID: {', '.join(map(str, unsupported_ids))}",
        }
    )

    retrieval_checks.append(check_issue_fields(field_claims, sources))
    if mode == "model" and query_plan.get("source") in {"mantis", "both"}:
        count_text = plain_text(content)
        claimed_counts = [int(match.group(1) or match.group(2)) for match in re.finditer(
            r"(?:이슈|결과)(?:는|가)?\s*(?:총|전체)\s*([\d,]+)\s*건"
            r"|(?:총|전체)\s*([\d,]+)\s*건(?:의)?\s*(?:이슈|결과)", count_text.replace(",", ""),
        )]
        if claimed_counts:
            count_stages = [stage for stage in (retrieval_result or {}).get("stages", [])
                            if str(stage.get("source", "")).startswith("mantis_")
                            and not stage.get("meta", {}).get("reference_only")]
            complete = any(stage.get("status") == "ok" and stage.get("meta", {}).get("search_complete") is True
                           and stage.get("meta", {}).get("coverage") not in {"saved_filter", "top_k", "snapshot_matches"}
                           for stage in count_stages) and not any(
                               stage.get("status") in {"error", "partial_error"} for stage in count_stages)
            expected_count = count_retrieved_issues(retrieval_result or {})
            retrieval_checks.append({"id": "answer_count", "label": "답변 총건수",
                "status": "pass" if complete and all(n == expected_count for n in claimed_counts) else "fail",
                "detail": f"확인한 이슈 {expected_count}건 · 전체 조회 {'완료' if complete else '미확인'}"})

    match_constraints = constraints
    if similar_base_issue_ids:
        match_constraints = {**constraints, "issue_ids": []}

    matched_sources = []
    definite_mismatches = 0
    notion_constraints = extract_notion_constraints(str((retrieval_result or {}).get("query") or question))
    expected_people = extract_query_constraints(str((retrieval_result or {}).get("query") or question)).get("people", {}).get("names") or []
    for stage in (retrieval_result or {}).get("stages", []):
        if stage.get("source") != "notion":
            continue
        actual = stage.get("meta", {}).get("constraints", {})
        if expected_people and actual.get("owner"):
            matched_owner = normalize_for_match(str(actual["owner"])) in {normalize_for_match(name) for name in expected_people}
            retrieval_checks.append({
                "id": "notion_owner_intent", "label": "Notion 담당자 조건",
                "status": "pass" if matched_owner else "fail",
                "detail": f"요청 담당자 {', '.join(expected_people)} · 실행 담당자 {actual['owner']}",
            })
    base_match_constraints = match_constraints
    for source in sources:
        match_constraints = report_evidence_constraints(mode, source, base_match_constraints, retrieval_result or {})
        if source.get("type") == "notion":
            matched = notion_page_matches_constraints(
                {**(source.get("detail") or {}), **source}, notion_constraints,
            )
            matched_sources.append(matched)
            definite_mismatches += int(not matched)
            continue
        definite_mismatches += int(has_definite_scope_mismatch(source, match_constraints))
        if not has_constraint(match_constraints):
            matched_sources.append(True)
            continue
        source_text = json.dumps(source, ensure_ascii=False, default=str)
        source_issue_id = source.get("issue_id")
        text_constraints = dict(match_constraints)
        if source.get("type") == "mantis":
            # 구조화 속성은 위에서 검증했다. 본문의 비교 대상 고객사·매체로
            # 같은 조건을 다시 판단하면 올바른 조회에도 잘못된 경고가 붙는다.
            source_detail = source.get("detail") or {}
            project = source.get("project") or source_detail.get("project")
            media = source.get("media_type") or source_detail.get("media_type")
            if project and project not in {"미상", "기타/미상"}:
                for key in ("customer_exact", "customer_aliases", "excluded_customer_aliases"):
                    text_constraints.pop(key, None)
            if media and media not in {"미상", "기타/미상"}:
                for key in ("platform_aliases", "excluded_platform_aliases"):
                    text_constraints.pop(key, None)
        matches = text_matches_constraints(source_text, text_constraints)
        matches = matches and text_matches_status(source_status(source), match_constraints.get("statuses", []))
        matches = matches and text_matches_screen_numbers(source_text, match_constraints.get("screen_numbers", []))
        people = match_constraints.get('people', {})
        if people.get('role') == 'effort_person':
            matches = matches and bool(set(map(normalize_for_match, people.get('names', []))) &
                                       set(map(normalize_for_match, issue_effort_people(source.get('detail') or {}))))
        else:
            matches = matches and text_matches_people(
                source_text, match_constraints.get("people", {}),
                str(source.get("handler") or (source.get("detail") or {}).get("handler") or ""),
                str(source.get("reporter") or (source.get("detail") or {}).get("reporter") or ""),
            )
        matches = matches and matches_source_filters('mantis', {
            'delivered_at': issue_delivery_date(source.get('detail') or {}),
            'effort_person': issue_effort_people(source.get('detail') or {}),
        }, [f for f in match_constraints.get('filter_clauses', []) if f['field'] in {'delivered_at', 'effort_person'}])
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
            "status": "fail" if definite_mismatches else "warning" if not all(matched_sources) else "pass",
            "detail": (
                f"원문 속성의 조건 불일치 {definite_mismatches}건"
                if definite_mismatches else f"원문 텍스트 기준 조건 불일치 가능성 {matched_sources.count(False)}건"
                if not all(matched_sources) else "조건 일치"
            ),
        }
    )

    ungrounded = ungrounded_constraint_labels(
        str((retrieval_result or {}).get("query") or question),
        constraints,
        str(query_plan.get("source") or ""),
        query_plan=query_plan,
    )
    retrieval_checks.append(
        {
            "id": "constraint_grounding",
            "label": "조건 근거",
            "status": "pass" if not ungrounded else "warning",
            "detail": (
                "모든 조건이 현재 질문 또는 인증 사용자·서버에서 확인한 이전 조건에 근거함"
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
    needs_full_scope = bool(
        query_plan.get("operation") in {"count", "aggregate"}
        or query_plan.get("followup_effort") or constraints.get("followup_effort")
        or infer_operation(question) in {"count", "aggregate"}
        or query_plan.get("group_by") or extract_group_by(question)
    )

    def stage_incomplete(stage):
        if stage.get("status") == "partial_error" or stage.get("error_kind") == "dependency":
            return True
        if stage.get("status") != "ok":
            return False
        meta = stage.get("meta", {})
        # 의미 검색 상위 N건은 전체 건수/부재를 증명하지 않지만 목록 요청은 충족할 수 있다.
        if (not needs_full_scope and stage.get("source") == "chroma_db"
                and meta.get("query_complete") is True and meta.get("result_window_complete") is True):
            return False
        if needs_full_scope and meta.get("coverage") in {"top_k", "saved_filter", "snapshot_matches"}:
            return True
        if meta.get("search_complete") is False:
            return True
        return (stage.get("source") in {"mantis_page_scan", "mantis_snapshot", "mantis_filter", "chroma_db", "notion"}
                and "search_complete" not in meta)

    incomplete_stages = [stage for stage in (retrieval_result or {}).get("stages", []) if stage_incomplete(stage)]
    if incomplete_stages:
        retrieval_checks.append({
            "id": "search_completeness", "label": "조회 완전성", "status": "warning",
            "detail": "일부 조회가 완료되지 않아 전체 결과를 검증할 수 없음",
        })

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

    if monthly_checks:
        monthly_score, monthly_status = score_checks(monthly_checks)
        dimensions.append({'id': 'calculation', 'label': '월별 계산 검증', 'score': monthly_score,
                           'status': monthly_status, 'checks': monthly_checks,
                           'description': '원문 등록일·전달일과 기입 공수를 월별로 다시 합산합니다.'})

    if mode in {"deterministic_effort_report", "deterministic_filtered_effort_report", "deterministic_effort_sla_report"}:
        component_mode = (retrieval_result or {}).get('effort_component_mode', mode)
        filtered_effort = component_mode == "deterministic_filtered_effort_report"
        effort_source = "mantis_effort_filtered" if filtered_effort else "mantis_effort"
        effort_stage: Dict[str, Any] = next(
            (stage for stage in (retrieval_result or {}).get("stages", []) if stage.get("source") == effort_source),
            {},
        )
        effort_meta = effort_stage.get("meta", {})
        person = effort_meta.get("person", "")
        effort_sources = sources
        if mode == 'deterministic_effort_sla_report':
            effort_ids = (retrieval_result or {}).get('effort_source_ids') or []
            effort_sources = [s for s in sources if s.get('issue_id') in effort_ids]
        recomputed_totals = {field: 0.0 for field in EFFORT_FIELD_LABELS}
        for source in effort_sources:
            for field_name, value in (source.get("detail", {}).get("effort_fields") or {}).items():
                if field_name not in recomputed_totals:
                    continue
                for effort_person, amount in parse_effort_field_value(value):
                    contributor_matches = matches_source_filters('mantis', {'effort_person': effort_person},
                        [f for f in constraints.get('filter_clauses', []) if f['field'] == 'effort_person'])
                    if contributor_matches and (filtered_effort or normalize_for_match(effort_person) == normalize_for_match(person)):
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
                "status": "pass" if len(effort_sources) == int(effort_meta.get("displayed_count", 0)) else "fail",
                "detail": f"원문 {len(effort_sources)}건 · 집계 {effort_meta.get('displayed_count', 0)}건",
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
        partial = (count_stage.get('status') in {'error', 'partial_error'}
                   or count_stage.get('meta', {}).get('search_complete') is False)
        count_status = 'pass' if displayed_count == expected_count and expected_count >= 0 else 'fail'
        if partial:
            reference = re.search(r'확인된 범위의 참고값은\s*(\d+)\s*건', content or '')
            displayed_count = int(reference[1]) if reference else None
            count_status = ('warning' if displayed_match is None and displayed_count == expected_count
                            and expected_count >= 0 and '확정할 수 없' in content else 'fail')
        calculation_checks = [
            {
                "id": "count_result",
                "label": "집계 결과",
                "status": count_status,
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
    elif not monthly_checks:
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
        elif mode in {"deterministic_effort_report", "deterministic_filtered_effort_report"}:
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
        sum(dimension["score"] for dimension in completed_dimensions) / len(completed_dimensions)
    ) if completed_dimensions else 0
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
        "note": "점수는 자동 판정이 완료된 영역만 평균하며, '검토 필요' 영역은 상태로 별도 표시합니다.",
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
    return build_model_retrieval_context(basic_result, limit), build_model_evidence_sources(basic_result, limit)


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
