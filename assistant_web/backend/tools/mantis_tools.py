"""Mantis 이슈 검색·상세 조회 도구. 기존 설명 검색 함수를 확정 조건으로 감싼다."""

from copy import deepcopy
from typing import Annotated, Any, Dict, List, Literal

from pydantic import ConfigDict, Field

from backend.display import customer_name
from backend.evidence.excerpts import safe_excerpts
from backend.intent.explanation import explanation_search_terms
from backend.mantis_status import display_mantis_status_label
from backend.sources.chroma import execute_chroma_search
from backend.sources.mantis import execute_mantis_id_lookup, execute_mantis_page_scan
from backend.sources.mantis_multiquery import search_explanation_queries
from backend.tools.registry import ToolContext, ToolSpec, error_result
from backend.tools.query_conditions import QueryArguments
from backend.utils.text import compact_text


class SearchMantisArgs(QueryArguments):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=200,
                       description="Mantis 이슈를 찾을 핵심어. 화면·기능·증상·오류명·기술명 위주로 3~8단어")
    limit: int = Field(default=5, ge=1, le=5, description="받을 이슈 수")


class MantisDetailArgs(QueryArguments):
    model_config = ConfigDict(extra="forbid")
    issue_id: int = Field(ge=1, le=99_999_999, description="이번 요청의 검색 결과나 관계에서 확인한 이슈 번호")
    projection: Literal["detail", "title"] = Field(default="detail", description="번호와 제목만 요청하면 title")


class MantisDetailsArgs(QueryArguments):
    model_config = ConfigDict(extra="forbid")
    issue_ids: list[Annotated[int, Field(ge=1, le=99_999_999)]] = Field(
        min_length=1, max_length=5, description="조회 결과·관계에서 확인한 이슈 번호 최대 5개")


def _scoped(context: ToolContext) -> Dict[str, Any]:
    return {**deepcopy(context.conditions["mantis"].constraints), "answer_query": context.question}


def _without_keyword_window(stage: Dict[str, Any]) -> Dict[str, Any]:
    """페이지 스캔의 전체 스캔·일치 목록은 그 검색어에만 유효하다. 다른 조회 결과를 지우지 않게 뺀다."""
    meta = dict(stage.get("meta") or {})
    meta["scanned_issue_count"] = len(meta.pop("scanned_issue_ids", []) or [])
    meta.pop("matched_issue_ids", None)
    return {**stage, "meta": meta}


def issue_view(row: Dict[str, Any], *, detail: bool = False) -> Dict[str, Any]:
    issue = row.get("detail") or {}
    evidence = issue.get("answer_evidence") or {}
    view: Dict[str, Any] = {
        "ref": f"M#{row.get('id')}", "id": row.get("id"), "title": row.get("summary"),
        "customer": customer_name(row.get("project")),
        "status": issue.get("status_label") or display_mantis_status_label(row.get("status")),
        "created_at": row.get("created_at"), "screen_number": issue.get("screen_number", ""),
        "media_type": row.get("media_type"), "evidence_complete": evidence.get("complete") is True,
        "related_issue_ids": list(issue.get("related_issue_ids") or [])[:5],
        "source_reference_ids": list(issue.get("source_reference_ids") or [])[:5],
    }
    if not detail:
        view["snippets"] = [compact_text(str(item.get("text") or ""), 220)
                            for item in (issue.get("matching_evidence") or [])[:2] if isinstance(item, dict)]
        if not view["snippets"]:
            view["snippets"] = [compact_text(str(issue.get("description") or row.get("preview") or ""), 220)]
        return view
    view.update(
        handler=row.get("handler"), description=compact_text(str(issue.get("description") or ""), 620),
        resolution_fields={key: compact_text(str(value), 400)
                           for key, value in (issue.get("resolution_fields") or {}).items() if value},
        date_fields=issue.get("date_fields") or {},
        excerpts=[{"field": item.get("field"), "created_at": item.get("created_at"),
                   "text": compact_text(str(item.get("text") or ""), 700)} for item in safe_excerpts(issue)[:8]],
    )
    return view


def _register(context: ToolContext, stages: List[Dict[str, Any]]) -> None:
    conditions = context.conditions["mantis"]
    for stage in stages:
        context.ledger.add("mantis", stage, grade=conditions.grade, unapplied=conditions.unapplied,
                           call_id=context.call_id)


def _envelope(tool: str, context: ToolContext, results: List[Dict[str, Any]], **extra: Any) -> Dict[str, Any]:
    conditions = context.conditions["mantis"]
    return {"ok": True, "tool": tool, "untrusted_content": True, "evidence_grade": conditions.grade,
            "applied_conditions": list(conditions.applied), "unapplied_conditions": list(conditions.unapplied),
            "results": results, **extra}


def search_mantis_issues(args: SearchMantisArgs, context: ToolContext) -> Dict[str, Any]:
    from backend.services.retrieval_pipeline import _explanation_referenced_issues
    conditions = context.conditions["mantis"]
    scoped = _scoped(context)
    target_ids = scoped.get("issue_ids") or []
    if len(target_ids) == 1:
        # 확정 번호는 키워드 순위 밖에 있어도 동일 필터로 원문을 확인할 수 있다.
        stage = execute_mantis_id_lookup(target_ids, scoped)
        stages = [{**stage, "source": "mantis_explanation_focus"}]
    elif context.enabled_sources.get("vectorDb"):
        stage = search_explanation_queries(args.query, [], args.limit, scoped, search=execute_chroma_search)
        stages = [{**stage, "source": "chroma_explanation", "meta": {**(stage.get("meta") or {}), "constraints": scoped}}]
    else:
        keyword = " ".join(explanation_search_terms(args.query, conditions.constraints))
        scan = execute_mantis_page_scan(keyword, args.limit, conditions.constraints)
        found = [int(issue["id"]) for issue in scan.get("issues_list", []) if issue.get("id")]
        stages = [_without_keyword_window(scan)]
        if found:
            stages.append(execute_mantis_id_lookup(found[:args.limit], scoped))
    # 원문 노트에 번호와 제목이 함께 인용된 선행 이슈는 기존 검증 규칙으로만 더한다.
    _explanation_referenced_issues(context.question, conditions.constraints, stages)
    _register(context, stages)
    seen = {int(issue.get("id") or 0) for stage in stages for issue in stage.get("issues_list") or []}
    seen.update(int(document.get("issue_id") or 0) for stage in stages for document in stage.get("documents") or [])
    rows = [row for row in context.ledger.issue_rows() if int(row.get("id") or 0) in seen][:args.limit + 2]
    complete = all((stage.get("meta") or {}).get("search_complete") is True for stage in stages)
    failed = any(stage.get("status") in {"error", "partial_error"} for stage in stages)
    return _envelope("search_mantis_issues", context, [issue_view(row) for row in rows],
                     search_complete=complete and not failed, source_error=failed,
                     note="결과 없음이나 조회 오류는 해당 사실이 없다는 뜻이 아닙니다." if not rows or failed else "")


def _known_issue(context: ToolContext, issue_id: int) -> bool:
    return (context.ledger.issue_row(issue_id) is not None or issue_id in context.confirmed_issue_ids
            or issue_id in context.ledger.referenced_issue_ids())


def get_mantis_issue_detail(args: MantisDetailArgs, context: ToolContext) -> Dict[str, Any]:
    issue_id = args.issue_id
    if not _known_issue(context, issue_id):
        return error_result("get_mantis_issue_detail", "not_in_scope",
                            "이번 요청의 조회 결과·확정 조건·원문 관계에서 확인되지 않은 이슈 번호입니다.")
    row = context.ledger.issue_row(issue_id)
    cached = bool(row and (row.get("detail") or {}).get("answer_evidence"))
    contract = context.query_contracts.get('mantis')
    if contract and contract.purpose == 'investigate':
        # 다른 단계에서 읽은 원문도 이번 단계 필터로 원천에서 다시 대조한다.
        cached = False
    stage: Dict[str, Any]
    if cached and row is not None:
        # 이미 확보한 원문을 다시 조회하지 않고 집중 근거로만 표시한다.
        stage = {"source": "mantis_explanation_focus", "status": "ok", "issues_list": [deepcopy(row["detail"])],
                 "meta": {"search_complete": True, "agent_reused": True}}
    else:
        stage = execute_mantis_id_lookup([issue_id], _scoped(context))
        if not stage.get("issues_list"):
            meta: Dict[str, Any] = stage.get("meta") or {}
            missing = issue_id in (meta.get("missing_ids") or [])
            return error_result("get_mantis_issue_detail", "not_found" if missing else "conditions_mismatch",
                                "이슈를 찾지 못했습니다." if missing else "확정 조회 조건과 맞지 않는 이슈라 열람하지 않았습니다.")
        stage = {**stage, "source": "mantis_explanation_focus"}
    if args.projection == "title":
        stage = {**stage, "meta": {**(stage.get("meta") or {}), "title_lookup_id": issue_id}}
    else:
        stage = {**stage, "meta": {**(stage.get("meta") or {}), "detail_read_id": issue_id}}
    _register(context, [stage])
    detail_row = context.ledger.issue_row(issue_id)
    if args.projection == "title" and detail_row:
        return _envelope("get_mantis_issue_detail", context, [{"id": issue_id, "ref": f"M#{issue_id}",
                         "title": detail_row["summary"]}], reused=cached)
    return _envelope("get_mantis_issue_detail", context, [issue_view(detail_row, detail=True)] if detail_row else [],
                     reused=cached)


def get_mantis_issue_details(args: MantisDetailsArgs, context: ToolContext) -> Dict[str, Any]:
    """동일한 인자·필터·시간 검증 안에서 소수의 본문을 읽는다. 원천 요청은 순차 실행한다."""
    identifiers = list(dict.fromkeys(args.issue_ids))
    if any(not _known_issue(context, identifier) for identifier in identifiers):
        return error_result('get_mantis_issue_details', 'not_in_scope',
                            '모든 이슈 번호가 이번 조회 결과·확정 조건·원문 관계에서 확인되어야 합니다.')
    results, failures = [], []
    for identifier in identifiers:
        result = get_mantis_issue_detail(MantisDetailArgs(issue_id=identifier), context)
        if result.get('ok'):
            results.extend(result['results'])
        else:
            failures.append({'issue_id': identifier, 'error': result['error']})
    return _envelope('get_mantis_issue_details', context, results, failures=failures,
                     complete=not failures and all(r['evidence_complete'] for r in results))


SPECS = [
    ToolSpec("search_mantis_issues",
             "Mantis 고객사 이슈를 의미 검색한다. 확정 조회 조건은 서버가 자동 적용한다.",
             SearchMantisArgs, "mantis", search_mantis_issues),
    ToolSpec("get_mantis_issue_detail",
             "검색 결과·관계로 확인한 Mantis 이슈 한 건의 원문 발췌(본문·노트·조치 필드)를 읽는다.",
             MantisDetailArgs, "mantis", get_mantis_issue_detail),
]

BATCH_SPECS = [ToolSpec('get_mantis_issue_details',
    '확인된 Mantis 이슈 최대 5개의 본문·노트를 한 번에 읽는다. 다건 문서 작성에 사용한다. 각 이슈의 조건과 원문 근거를 별도로 유지한다.',
    MantisDetailsArgs, 'mantis', get_mantis_issue_details)]
