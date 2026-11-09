"""원천별 검색 실행과 근거 조회 순서를 조정한다. 기존 실행·필터 계약을 유지한다."""

import re
from backend.sources.mantis_store import with_store_scope
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException

from backend.utils.text import (
    normalize_for_match,
)
from backend.sources.chroma_document import (
    normalize_chroma_issue_document,
)
from backend.intent.list_continuation import continuation_execution_constraints
from backend.query_filters import filter_audit_for_source
from backend.notion.formatting import has_deterministic_notion_list_intent
from backend.intent.notion_grounding import grounded_notion_body_constraints
from backend.query.notion_text import notion_literal_title, notion_structural_query, split_notion_body_query
from backend.sources.chroma import execute_chroma_search, get_chroma_document
from backend.sources.mantis_fields import get_issue_custom_field_value, normalize_issue
from backend.sources.mantis import execute_mantis_filter_search, execute_mantis_id_lookup, execute_mantis_page_scan, execute_mantis_snapshot_search, get_mantis_snapshot_freshness, revalidate_mantis_snapshot_stage
from backend.sources.mantis_client import fetch_mantis_issue_by_id
from backend.sources.mantis_matching import issue_matches_constraints
from backend.notion.search import build_notion_terms, execute_notion_search
from backend.intent.constants import COMBINED_SOURCE_MARKERS, EXPLICIT_MANTIS_MARKERS, EXPLICIT_NOTION_MARKERS
from backend.intent.keyword_terms import build_keyword_terms, build_similar_issue_search_text
from backend.intent.matching import chroma_text_matches_constraints
from backend.intent.constraints import extract_query_constraints, has_constraint, merge_query_plan_constraints, wants_similar_issue_search
from backend.intent.source_router import rule_based_search_intent


def notion_question_needs_blocks(query_text: str) -> bool:
    """Fetch bodies for model answers; skip them for deterministic lists/counts."""
    return not has_deterministic_notion_list_intent(query_text)


def _similar_reference_lookup(
    issue_ids: List[int], constraints: Dict[str, Any]
) -> Dict[str, Any]:
    """Resolve similarity base issues locally before trying the live API."""
    reference_issues: List[Dict[str, Any]] = []
    missing_ids: List[int] = []
    for issue_id in issue_ids:
        document = get_chroma_document(issue_id)
        content = str((document or {}).get("content") or "")
        issue = normalize_chroma_issue_document(content)
        if issue and chroma_text_matches_constraints(content, constraints):
            reference_issues.append(issue)
        elif not document:
            missing_ids.append(issue_id)

    local_reference_count = len(reference_issues)
    reason = ""
    if missing_ids:
        try:
            live_stage = execute_mantis_id_lookup(missing_ids, constraints)
            reference_issues.extend(live_stage.get("issues_list") or [])
            missing_ids = list((live_stage.get("meta") or {}).get("missing_ids") or [])
        except (HTTPException, requests.RequestException) as exc:
            reason = exc.detail if isinstance(exc, HTTPException) else type(exc).__name__

    reference_issues.sort(key=lambda issue: int(issue.get("id") or 0), reverse=True)
    return {
        "source": "mantis_id_lookup_reference",
        "status": "ok" if reference_issues else ("error" if reason else "not_found"),
        "reason": str(reason),
        "issues_list": [],
        "meta": {
            "issue_ids": issue_ids,
            "missing_ids": missing_ids,
            "displayed_count": 0,
            "reference_issues": reference_issues,
            "reference_only": True,
            "local_reference_count": local_reference_count,
        },
    }


def source_specific_terms(query_text: str, source: str, constraints: Dict[str, Any]) -> List[str]:
    if source == "notion":
        terms = build_notion_terms(query_text, constraints)
        blocked = {
            *EXPLICIT_MANTIS_MARKERS,
            *COMBINED_SOURCE_MARKERS,
            "장애",
            "오류",
            "버그",
            "화면번호",
            "공수",
            "이력",
        }
    else:
        terms = build_keyword_terms(query_text, constraints)
        blocked = {
            *EXPLICIT_NOTION_MARKERS,
            *COMBINED_SOURCE_MARKERS,
            "공지",
            "회식",
            "휴가",
            "결재요청",
            "결재완료",
            "주요이슈현황",
            "이력",
        }
    normalized_blocked = {normalize_for_match(value) for value in blocked}
    filtered = [
        term
        for term in terms
        if not any(blocked in normalize_for_match(term) for blocked in normalized_blocked)
    ]
    if source == "notion" and "공지" in normalize_for_match(query_text) and "공지" not in filtered:
        filtered.insert(0, "공지")
    return list(dict.fromkeys(filtered))[:6]


def grounded_mantis_terms(
    query_text: str,
    constraints: Dict[str, Any],
    planned_terms: Optional[List[str]] = None,
) -> List[str]:
    """플래너가 일부만 골라도 질문에 명시된 모든 Mantis 주제를 보존한다."""
    # 플래너의 부분 문자열/요약으로 필수 주제를 대체하지 않는다.
    # 검증된 동의어는 소스의 keyword_term_aliases 매칭에서 확장한다.
    return source_specific_terms(query_text, "mantis", constraints)


_SOURCE_TITLE_REFERENCE = re.compile(r"(?<!\d)0*(\d{4,7})\s*:\s*(\[\s*\d{3,5}\s*\][^\r\n]{8,240})")


_REFERENCE_GENERIC_TERMS = {
    "조건검색", "조건식", "화면", "결과", "결과값", "현상", "문의", "관련", "수정", "내용", "확인", "처리",
    "운영", "적용", "예정", "버전", "검색", "기능", "오류", "문제", "고객", "이슈",
}


def _reference_matches_question(query: str, title: str, constraints: Dict[str, Any]) -> bool:
    title_screen = re.match(r"^\s*\[\s*(\d{3,5})\s*\]", title)
    query_screens = set(re.findall(r"(?<!\d)\d{3,5}(?!\d)", query))
    if title_screen and query_screens and title_screen.group(1) not in query_screens:
        return False
    customer_terms = {word for alias in constraints.get("customer_aliases") or []
                      for word in re.findall(r"[가-힣]{2,}", alias)}
    query_terms = set(re.findall(r"[가-힣]{2,}", query))
    title_terms = set(re.findall(r"[가-힣]{2,}", title))
    shared = (query_terms & title_terms) - _REFERENCE_GENERIC_TERMS - customer_terms
    return len(shared) >= 2 or bool(title_screen and title_screen.group(1) in query_screens
                                    and any(len(term) >= 4 for term in shared))


def _explanation_referenced_issues(query: str, constraints: Dict[str, Any], stages: List[Dict[str, Any]]) -> None:
    """설명 원문에 번호와 제목이 함께 인용된 선행 이슈만 확인해 후보에 더한다."""
    sources: List[Dict[str, Any]] = []
    for stage in stages:
        sources.extend(stage.get("issues_list") or [])
        sources.extend(doc.get("latest_issue") for doc in stage.get("documents", []) if doc.get("latest_issue"))
    if not sources:
        return
    existing_ids = {issue.get("id") for issue in sources}
    added: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    reference_scopes: Dict[int, Dict[str, Any]] = {}
    checked: set[int] = set()
    for source in sources[:5]:
        project = source.get("project")
        if not isinstance(project, str) or project.strip() in {"", "미상", "기타/미상"}:
            continue
        excerpts = (source.get("answer_evidence") or {}).get("excerpts") or []
        for excerpt in excerpts:
            if not str(excerpt.get("field") or "").startswith("note:"):
                continue
            for reference in _SOURCE_TITLE_REFERENCE.finditer(str(excerpt.get("text") or "")):
                target_id = int(reference.group(1))
                if target_id == source.get("id") or target_id in existing_ids or target_id in checked:
                    continue
                checked.add(target_id)
                try:
                    raw = fetch_mantis_issue_by_id(target_id)
                except (HTTPException, requests.RequestException):
                    continue
                if not raw or raw.get("id") != target_id:
                    continue
                title = str(raw.get("summary") or "").strip()
                if (len(title) < 20 or normalize_for_match(title) not in normalize_for_match(reference.group(2))
                        or not _reference_matches_question(query, title, constraints)
                        or str((raw.get("project") or {}).get("name") or "") != project):
                    continue
                # 기본 담당자 범위는 검색 기본값이다. 사용자가 직접 선택한 팀·담당자 조건은 보존한다.
                scoped = dict(constraints)
                relaxed: List[str] = []
                if scoped.get("mantis_team_scope") == "default":
                    scoped.pop("mantis_team_members", None)
                    scoped.pop("mantis_team_label", None)
                    scoped.pop("mantis_team_scope", None)
                    relaxed.append("default_team")
                # '구 HTS와 결과가 다름'은 비교 대상 설명이다. 원본 매체 필드가 비어
                # 있더라도 정확히 인용된 원본 제목에 HTS가 있을 때만 함께 제시한다.
                media = get_issue_custom_field_value(raw, "매체구분")
                contextual_platform = bool(re.search(r"(?:구|기존|옛)\s*HTS\s*(?:와|과|대비|비교)", query, re.I))
                hard_platform = bool(re.search(
                    r"매체\s*구분|(?:HTS|H-able).{0,10}(?:만|한정|제한)|"
                    r"(?:만|한정|제한).{0,10}(?:HTS|H-able)", query, re.I))
                if (not media and contextual_platform and not hard_platform
                        and any(normalize_for_match(alias) in normalize_for_match(title)
                                for alias in scoped.get("platform_aliases") or [])):
                    scoped.pop("platform_key", None)
                    scoped.pop("platform_aliases", None)
                    scoped["filter_clauses"] = [clause for clause in scoped.get("filter_clauses") or []
                                                if clause.get("field") != "platform"]
                    relaxed.append("unknown_media_context")
                if not issue_matches_constraints(raw, scoped):
                    continue
                normalized = normalize_issue(raw, evidence_query=query)
                source.setdefault("source_reference_ids", []).append(target_id)
                added.append(normalized)
                reference_scopes[target_id] = {**scoped,
                                               '_reference_unknown_media': 'unknown_media_context' in relaxed}
                existing_ids.add(target_id)
                edges.append({"source_issue_id": source["id"], "target_issue_id": target_id,
                              "source_field": excerpt["field"], "scope_exception": relaxed})
                if len(added) >= 2:
                    break
            if len(added) >= 2:
                break
        if len(added) >= 2:
            break
    if added:
        stages.append({"source": "mantis_explanation_reference", "status": "ok", "issues_list": added,
                       "meta": {"search_complete": True, "source_reference_edges": edges,
                                "reference_scopes": reference_scopes}})


def _explanation_retrieval(query: str, limit: int, tools: Dict[str, bool], plan: Dict[str, Any]) -> Dict[str, Any]:
    """설명 문장의 조사·서술어를 필수 AND 검색어로 변환하지 않는다."""
    from backend.intent.explanation import explanation_search_terms
    constraints = dict(plan["resolved_constraints"])
    scoped = {**constraints, "answer_query": query}
    # CMF 같은 드문 기술 식별어가 짧은 최신 이슈의 제목에 정확히 있어도,
    # 긴 과거 이슈의 의미 점수에 밀려 표시 범위 밖으로 나갈 수 있다.
    # 설명 질문에 식별어가 하나만 명시된 경우 그 원문 일치를 후보 범위에 보존한다.
    technical_terms = [term for term in explanation_search_terms(query, constraints)
                       if re.fullmatch(r"[A-Z][A-Z0-9_]{2,7}", term)
                       and term not in {"HTS", "MTS", "KRX", "NXT", "API", "QC", "FID", "DLL"}]
    if len(technical_terms) == 1:
        scoped["semantic_focus_terms"] = technical_terms
    stages: List[Dict[str, Any]] = []
    ids = constraints.get("issue_ids") or []
    similar = (False if plan.get('planner')=='unified_llm'
               else wants_similar_issue_search(query, constraints))
    if tools.get("mantis") and ids:
        try:
            stages.append(execute_mantis_id_lookup(ids, scoped))
        except (HTTPException, requests.RequestException) as exc:
            stages.append({"source": "mantis_id_lookup", "status": "error", "issues_list": [],
                           "reason": str(exc), "meta": {"search_complete": False}})
    if tools.get("mantis") and tools.get("vectorDb") and (not ids or similar):
        search_query = query
        screen = re.search(r'(?<!\d)(\d{4})(?!\d)', query)
        mismatch = re.search(r'종목명.{0,20}차트.{0,20}데이터.{0,30}(?:다르|달랐|달라|맞지|불일치)', query)
        if not ids and constraints.get('customer_key') and screen and mismatch:
            focus = execute_mantis_page_scan(f'{screen.group(1)} 종목 차트', 1, scoped)
            candidates = focus.get('issues_list') or []
            if candidates:
                title = normalize_for_match(str(candidates[0].get('summary') or ''))
                if (screen.group(1) in title and '종목' in title and '차트' in title
                        and re.search(r'맞지않|다르|불일치', title)):
                    stages.append({**focus, 'source': 'mantis_explanation_focus'})
                    stages.append(execute_mantis_id_lookup([int(candidates[0]['id'])], scoped))
                    return {
                        'query': query, 'keyword': '', 'constraints': constraints,
                        'intent_plan': plan, 'query_plan': plan, 'limit': limit, 'stages': stages,
                        'filter_audit': {
                            'planned_filters': constraints.get('filter_clauses') or [],
                            'sources': [filter_audit_for_source(str(s['source']),
                                                                constraints.get('filter_clauses') or [])
                                        for s in stages],
                        },
                    }
        if not ids and re.search(r'저장', query) and re.search(r'최종|답했|회신', query):
            # 설명 질문의 고유 설정명을 앞 네 개 일반 검색어가 밀어내는 경우,
            # 동일 고객사의 제목·본문 원문 일치 최신 후보 하나를 보존한다.
            setting = re.search(r'(?<![가-힣])([가-힣]{4,12})\s*설정(?:값|도|을|은|이)?', query)
            if setting:
                focus = execute_mantis_page_scan(f'{setting.group(1)} 저장', 1, scoped)
                stages.append({**focus, 'source': 'mantis_explanation_focus'})
                candidates = focus.get('issues_list') or []
                if candidates and re.search(r'가능|할\s*수\s*있', query):
                    title = normalize_for_match(str(candidates[0].get('summary') or ''))
                    if (normalize_for_match(setting.group(1)) in title
                            and '저장' in title and '가능여부' in title):
                        # 질문의 고유 설정명·행위·가능 여부가 최신 원문 제목에
                        # 모두 맞으면 오래된 유사 이슈를 답변 대상으로 섞지 않는다.
                        stages.append(execute_mantis_id_lookup([int(candidates[0]['id'])], scoped))
                        return {
                            'query': query, 'keyword': '', 'constraints': constraints,
                            'intent_plan': plan, 'query_plan': plan, 'limit': limit, 'stages': stages,
                            'filter_audit': {
                                'planned_filters': constraints.get('filter_clauses') or [],
                                'sources': [filter_audit_for_source(str(s['source']),
                                                                    constraints.get('filter_clauses') or [])
                                            for s in stages],
                            },
                        }
        if similar:
            scoped = {**scoped, "issue_ids": [], "exclude_issue_ids": list(dict.fromkeys([*ids, *(constraints.get("exclude_issue_ids") or [])])),
                      "max_issue_id_exclusive": min(ids),
                      "filter_clauses": [c for c in scoped.get("filter_clauses", []) if c["field"] != "issue_id" or c["operator"] in {"not_equals", "not_in"}]}
            # 기준 이슈 제목이 사용자가 명시한 비교 주제를 덮어쓰지 않게 한다.
            search_query = re.sub(r"#\s*\d+", " ", query)
            comparison_terms = explanation_search_terms(query, constraints)
            bases = [issue for s in stages for issue in s.get("issues_list", [])]
            if not comparison_terms and bases:
                search_query = build_similar_issue_search_text(bases[0]) or search_query
        from backend.sources.mantis_multiquery import search_explanation_queries
        stage = search_explanation_queries(search_query, plan.get('explanation_queries') or [],
                                           max(1, min(limit, 5) - bool(ids)), scoped,
                                           search=execute_chroma_search)
        stages.append({**stage, "source": "chroma_explanation",
                       "meta": {**stage.get("meta", {}), "constraints": scoped}})
    elif tools.get("mantis") and not ids:
        keyword = " ".join(explanation_search_terms(query, constraints))
        stage = execute_mantis_page_scan(keyword, limit, constraints)
        stages.append(stage)
        found_ids = [int(issue["id"]) for issue in stage.get("issues_list", []) if issue.get("id")]
        if found_ids:
            stages.append(execute_mantis_id_lookup(found_ids[:limit], scoped))
    if tools.get("mantis") and not ids:
        _explanation_referenced_issues(query, constraints, stages)
    return {"query": query, "keyword": "", "constraints": constraints, "intent_plan": plan,
            "query_plan": plan, "limit": limit, "stages": stages,
            "filter_audit": {"planned_filters": constraints.get("filter_clauses") or [],
                             "sources": [filter_audit_for_source(str(s["source"]), constraints.get("filter_clauses") or [])
                                         for s in stages]}}


@with_store_scope
def run_retrieval_pipeline(
    query_text: str,
    limit: int,
    tools: Dict[str, bool],
    intent_plan: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    if (intent_plan or {}).get('planner') == 'explicit_references':
        from backend.notion.direct_lookup import read_explicit_pages
        explicit = dict((intent_plan or {})['resolved_constraints'])
        stages = []
        if tools.get('notion') and explicit.get('notion_page_ids'):
            notion_filters = {**explicit, 'issue_ids': [], 'exclude_issue_ids': [],
                'filter_clauses': [c for c in explicit.get('filter_clauses', []) if c.get('field') != 'issue_id']}
            stages.append(read_explicit_pages(explicit['notion_page_ids'], notion_filters))
        if tools.get('mantis') and explicit.get('issue_ids'):
            try:
                stages.append(execute_mantis_id_lookup(explicit['issue_ids'], {**explicit, 'answer_query': query_text}))
            except (HTTPException, requests.RequestException):
                stages.append({'source': 'mantis_id_lookup', 'status': 'error', 'issues_list': [], 'meta': {'search_complete': False}})
        return {'query': query_text, 'keyword': '', 'constraints': explicit, 'intent_plan': intent_plan,
                'query_plan': intent_plan, 'limit': limit, 'stages': stages}
    if (intent_plan or {}).get("operation") == "explain" and (intent_plan or {}).get("source") not in {"notion", "both"}:
        return _explanation_retrieval(query_text, limit, tools, intent_plan or {})
    constraints = merge_query_plan_constraints(extract_query_constraints(query_text), intent_plan)
    display_constraints = constraints
    constraints = continuation_execution_constraints(constraints)
    stages = []
    plan_source = str((intent_plan or {}).get("source", ""))
    if plan_source not in {"notion", "mantis", "both", "general"}:
        plan_source = str(rule_based_search_intent(query_text, constraints).get("source") or "general")
    # 그룹별 목록은 특정 검색어가 아니라 전체 이슈 집합이 대상이다.
    # 플래너가 "최근"·"하나씩"·"매체별" 같은 표현을 검색어로 되돌려도 적용하지 않는다.
    planned_terms = [] if (intent_plan or {}).get("group_by") else (intent_plan or {}).get("terms") or []
    notion_intent = plan_source in {"notion", "both"}
    mantis_intent = plan_source in {"mantis", "both"}
    normalized_query = normalize_for_match(query_text)
    explicit_notion = any(
        normalize_for_match(keyword) in normalized_query
        for keyword in ("notion", "노션", "노선")
    )
    planner_notion = plan_source == "notion"
    effective_constraints = {} if explicit_notion or planner_notion else constraints
    literal_notion_title = notion_literal_title(query_text) if notion_intent else ""
    _body_subject, body_clauses = split_notion_body_query(query_text) if notion_intent else (query_text, [])
    # A quoted Notion value is an exact document title, not a bag of customer,
    # platform, status, owner, or screen-number filters. Re-parse only the text
    # outside the quotes so an explicitly stated outer condition is preserved
    # without turning words inside the title into accidental AND filters.
    notion_constraints = (
        grounded_notion_body_constraints(query_text, {
            **constraints, "filter_clauses": [
                *(constraints.get("filter_clauses") or []), *((intent_plan or {}).get("filter_clauses") or []),
            ],
        }) if body_clauses else
        extract_query_constraints(notion_structural_query(query_text)) if literal_notion_title
        else constraints
    )
    notion_terms = planned_terms if plan_source == "notion" and planned_terms else None
    mantis_terms = (
        grounded_mantis_terms(query_text, effective_constraints, planned_terms)
        if plan_source == "mantis"
        else None
    )
    if plan_source == "both":
        notion_terms = source_specific_terms(query_text, "notion", effective_constraints)
        mantis_terms = source_specific_terms(query_text, "mantis", effective_constraints)
    unified = (intent_plan or {}).get("planner") == "unified_llm"
    if unified:
        notion_constraints = constraints
        mantis_terms = planned_terms
        notion_terms = planned_terms
    keyword = " ".join(planned_terms) if unified else " ".join(mantis_terms or build_keyword_terms(query_text, effective_constraints))
    notion_only = planner_notion
    # 검증된 조회 작업을 '비교를 끝내고' 같은 원문 단어로 다시 비교 검색하지 않는다.
    similar_issue_search = ((intent_plan or {}).get('report_kind')=='similar' if unified
                            else wants_similar_issue_search(query_text, constraints))
    exact_issue_lookup = bool(constraints.get("issue_ids")) and not similar_issue_search and plan_source != "both"
    historical_snapshot_used = False
    complete_raw_search_used = False
    structured_snapshot_eligible = bool(
        has_constraint(constraints)
        and (
            # 화면번호는 의미 검색보다 필드/제목의 정확 일치가 우선이다.
            # keyword 생성 여부와 무관하게 전체 구조화 스냅샷을 먼저 확인한다.
            constraints.get("screen_numbers")
            or
            constraints.get("created_at")
            or not keyword
            # 고객사에 매체나 상태까지 겹친 질의는 키워드/의미 검색이 후보를
            # 못 올려 결과가 0건이 되곤 한다(예: "KB증권 MTS 미종료"). 조건이
            # 이만큼 분명하면 키워드가 있어도 전체 스냅샷을 정확 조건으로 훑는다.
            or (
                constraints.get("customer_aliases")
                and (
                    constraints.get("platform_key")
                    or constraints.get("statuses")
                    or constraints.get("excluded_statuses")
                )
            )
        )
    )

    if tools.get("notion") and notion_intent:
        stages.append(
            execute_notion_search(
                query_text,
                limit,
                notion_constraints,
                planned_terms=notion_terms,
                include_blocks=notion_question_needs_blocks(query_text),
            )
        )

    if tools.get("mantis") and mantis_intent:
        if constraints.get("issue_ids"):
            try:
                if similar_issue_search:
                    id_stage = _similar_reference_lookup(
                        constraints["issue_ids"], constraints
                    )
                else:
                    id_stage = execute_mantis_id_lookup(
                        constraints["issue_ids"], constraints
                    )
                stages.append(id_stage)
            except (HTTPException, requests.RequestException) as exc:
                stages.append(
                    {
                        "source": "mantis_id_lookup_reference" if similar_issue_search else "mantis_id_lookup",
                        "status": "error",
                        "error_kind": "dependency",
                        "reason": exc.detail if isinstance(exc, HTTPException) else type(exc).__name__,
                        "issues_list": [],
                        "meta": {"displayed_count": 0},
                    }
                )

        if similar_issue_search and tools.get("vectorDb"):
            reference_issues: List[Dict[str, Any]] = []
            for stage in stages:
                reference_issues.extend(stage.get("meta", {}).get("reference_issues") or [])
            if reference_issues:
                base_issue = reference_issues[0]
                similar_constraints = {
                    **constraints,
                    "issue_ids": [],
                    "exclude_issue_ids": list(constraints.get("issue_ids") or []),
                    "max_issue_id_exclusive": int(base_issue.get("id") or 0),
                    "semantic_focus_terms": build_keyword_terms(query_text, constraints),
                    "filter_clauses": [
                        clause
                        for clause in constraints.get("filter_clauses", [])
                        if clause.get("field") != "issue_id"
                    ],
                }
                similar_query = build_similar_issue_search_text(base_issue) or query_text
                similar_stage = execute_chroma_search(
                    similar_query,
                    limit,
                    similar_constraints,
                    semantic=True,
                )
                similar_stage = {
                    **similar_stage,
                    "source": "chroma_similar",
                    "meta": {
                        **similar_stage.get("meta", {}),
                        "base_issue_ids": list(constraints.get("issue_ids") or []),
                        "derived_query": similar_query[:500],
                    },
                }
                stages.append(similar_stage)

        if not exact_issue_lookup and not similar_issue_search and structured_snapshot_eligible:
            # 기간/화면 조건으로 전체 원본을 훑을 때도 질문의 검색어를 함께
            # 적용한다. 표시 행만 나중에 거르면 전체 일치 ID와 총 건수가 틀린다.
            snapshot_constraints = {
                **constraints,
                "filter_clauses": [
                    *(constraints.get("filter_clauses") or []),
                    *({"field": "keyword", "operator": "contains", "value": term}
                      for term in keyword.split()),
                ],
            }
            snapshot_stage = execute_mantis_snapshot_search(limit, snapshot_constraints)
            snapshot_stage = revalidate_mantis_snapshot_stage(snapshot_stage, snapshot_constraints)
            stages.append(snapshot_stage)
            historical_snapshot_used = snapshot_stage.get("status") == "ok"

        # 스냅샷은 마지막 재구축 시점까지의 데이터만 갖는다. 요청 범위가 기준일
        # 이후를 포함하면(또는 날짜 조건이 없어 "현재까지"가 암묵 범위이면)
        # 라이브 페이지 스캔을 함께 돌려 최신 구간을 보완한다. 결과 병합 시
        # 이슈 ID 기준 중복 제거는 collect_retrieved_issues가 처리한다.
        snapshot_covers_range = False
        if historical_snapshot_used:
            snapshot_meta = snapshot_stage.get("meta") or {}
            snapshot_as_of = get_mantis_snapshot_freshness()
            requested_to = str((constraints.get("created_at") or {}).get("to") or "")
            snapshot_covers_range = bool(
                snapshot_as_of and requested_to and requested_to <= snapshot_as_of
            )
            # 최신 전체 원본의 구조화 필터가 이미 끝났다면 페이지 스캔 역시
            # 동일 저장본을 다시 훑을 뿐이다. 구형 인덱스/불완전 검증은 보완한다.
            if (not keyword and snapshot_meta.get("tool") == "mantis_complete_snapshot"
                    and snapshot_meta.get("search_complete") is True):
                snapshot_covers_range = True

        if not exact_issue_lookup and not similar_issue_search and (not historical_snapshot_used or not snapshot_covers_range):
            # 저장 필터는 화면번호를 부분 문자열이나 별도 조건으로 해석할 수 있다.
            # 화면번호 질의에서는 정확 일치를 보장하는 스냅샷/페이지 스캔만 사용한다.
            if not constraints.get("screen_numbers") and not keyword:
                try:
                    stages.append(execute_mantis_filter_search(query_text, limit, constraints))
                except (HTTPException, requests.RequestException) as exc:
                    stages.append(
                        {
                            "source": "mantis_filter",
                            "status": "error",
                            "error_kind": "dependency",
                            "reason": (
                                exc.detail
                                if isinstance(exc, HTTPException)
                                else f"Mantis connection failed: {type(exc).__name__}"
                            ),
                            "issues_list": [],
                            "meta": {"displayed_count": 0},
                        }
                    )

            try:
                page_stage = execute_mantis_page_scan(keyword, limit, constraints)
                stages.append(page_stage)
                page_meta = page_stage.get("meta") or {}
                complete_raw_search_used = bool(
                    page_stage.get("status") == "ok"
                    and page_meta.get("status") == "snapshot"
                    and page_meta.get("snapshot_complete") is True
                    and page_meta.get("search_complete") is True
                )
            except HTTPException as exc:
                stages.append(
                    {
                        "source": "mantis_page_scan",
                        "status": "error",
                        "error_kind": "dependency",
                        "reason": exc.detail,
                        "issues_list": [],
                        "meta": {"displayed_count": 0},
                    }
                )

    if (
        tools.get("vectorDb")
        and mantis_intent
        and not exact_issue_lookup
        and not similar_issue_search
        and not historical_snapshot_used
        and not complete_raw_search_used
    ):
        stages.append(execute_chroma_search(query_text, limit, constraints))

    # 만료된 전체 저장본 대신 구형 벡터 인덱스로 시작했어도, 뒤의 전체
    # 원본 조회가 성공하면 그 결과가 최종 범위다. 이전 인덱스를 합치면
    # 삭제된 이슈가 되살아나고 완료된 조회에도 불완전 경고가 붙는다.
    superseded_stages = []
    if any(stage.get("source") == "mantis_page_scan" and stage.get("status") == "ok"
           and stage.get("meta", {}).get("search_complete") is True for stage in stages):
        superseded_stages = [stage for stage in stages
                             if stage.get("source") == "mantis_snapshot"
                             and stage.get("meta", {}).get("tool") == "mantis_chroma_snapshot"
                             and stage.get("meta", {}).get("search_complete") is False]
        stages = [stage for stage in stages if stage not in superseded_stages]

    for stage in stages:
        stage_meta = dict(stage.get("meta") or {})
        audit_constraints = notion_constraints if stage.get("source") == "notion" else constraints
        stage_meta["filter_audit"] = filter_audit_for_source(
            str(stage.get("source") or ""),
            audit_constraints.get("filter_clauses") or [],
        )
        stage["meta"] = stage_meta

    result_constraints = notion_constraints if literal_notion_title and notion_only else display_constraints

    return {
        "query": query_text,
        "keyword": keyword,
        "constraints": result_constraints,
        "intent_plan": intent_plan,
        "query_plan": intent_plan,
        "limit": limit,
        "stages": stages,
        "superseded_stages": superseded_stages,
        "filter_audit": {
            "planned_filters": result_constraints.get("filter_clauses") or [],
            "sources": [stage.get("meta", {}).get("filter_audit", {}) for stage in stages],
        },
    }
