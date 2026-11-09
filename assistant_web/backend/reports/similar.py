"""유사사례 해결책 리포트 — 비슷한 과거 이슈 + 그때의 조치 요약(상위 계층).

기준 이슈와 의미가 비슷한 과거 이슈를 ChromaDB 임베딩으로 찾은 뒤, 각 사례가 실제로
어떻게 처리됐는지(상태·마지막 노트·상태 변경 이력)를 요약해 함께 보여준다. 단순히
'비슷한 이슈 목록'을 넘어 축적된 해결 지식을 재사용하도록 돕는다.

execute_chroma_search(semantic=True)는 후보 문서를 라이브 Mantis로 refresh해
latest_issue(notes/history 포함)를 채워주므로, 여기서는 추가 조회 없이 그 결과에서
해결 맥락을 뽑아낸다.

의존: chroma(유사도 검색) + mantis(기준 이슈 조회/정규화) + understanding(제약/유사질의).
"""

import re
from copy import deepcopy
from typing import Any, Dict, List, Optional

from backend.display import customer_name, status_name
from backend.utils.text import compact_text, normalize_for_match
from backend.sources.chroma import execute_chroma_search
from backend.sources.chroma_lexical import lexical_ranking_scope
from backend.sources.mantis_hybrid import hybrid_filter_scope
from backend.sources.mantis_refresh import short_refresh_scope
from backend.template_questions import attach_korean_particles
from backend.sources.mantis_fields import build_sources_from_issue_rows, normalize_issue
from backend.sources.mantis_client import fetch_mantis_issue_by_id
from backend.understanding import (
    build_similar_issue_search_text,
    extract_query_constraints,
    wants_similar_issue_search,
)


# '어떻게 해결했는지'를 함께 원하는 신호. 유사검색 의도에 이게 더해질 때만 동작한다.
RESOLUTION_MARKERS = (
    "해결", "어떻게", "조치", "처리방법", "처리 방법", "대응", "원인",
    "고쳤", "고친", "수정방법", "픽스", "fix",
)

SIMILAR_TOP_N = 3            # 조치까지 정리해 보여줄 유사 사례 수
# '원인'이라는 표제가 없어도 원문에 명시된 구현 불일치·제약은 보존한다.
# 인사말과 '○○ 과장입니다' 같은 자기소개. 직급이 붙은 경우만 지워 '원인은 …입니다' 같은 내용은 남긴다.


def _search_similar_candidates(query: str, constraints: dict, resource: bool) -> dict:
    symptom_query='리소스 증가 메모리 누수 사용자 개체 타이머 해제 누락'
    with (lexical_ranking_scope([query,symptom_query] if resource else [query]),
          hybrid_filter_scope(), short_refresh_scope() as refresh):
        result=execute_chroma_search(query,8 if resource else SIMILAR_TOP_N,constraints,semantic=True)
        if result.get('status')!='ok':
            return result
        searches=[result.get('meta') or {}]
        documents=list(result.get('documents') or [])
        if resource:
            extra=execute_chroma_search(symptom_query,8,constraints,semantic=True)
            searches.append(extra.get('meta') or {})
            pool=[*documents,*(extra.get('documents') or [])] if extra.get('status')=='ok' else documents
            seen=set(constraints.get('exclude_issue_ids') or [])
            documents=[]
            for document in pool:
                issue_id=int(document.get('issue_id') or 0)
                detail=document.get('latest_issue') or {}
                if (issue_id in seen or '[중복제외]' in str(detail.get('summary') or '')
                        or not _issue_resource_symptom(detail)):
                    continue
                seen.add(issue_id)
                documents.append(document)
            documents=documents[:SIMILAR_TOP_N]
        else:
            documents = [doc for doc in documents
                         if '[중복제외]' not in str((doc.get('latest_issue') or {}).get('summary') or '')]
        return {**result,'documents':documents,'searches':searches,
                'refresh_budget': {'seconds': refresh.total_seconds, 'spent_seconds': round(refresh.spent_seconds, 3),
                                   'cache_hits': refresh.cache_hits}}


def _wants_resolution(text: str) -> bool:
    normalized = normalize_for_match(text)
    return any(normalize_for_match(marker) in normalized for marker in RESOLUTION_MARKERS)


def _comparison_lines(base_issue: Dict[str, Any], examples: List[Dict[str, Any]]) -> List[str]:
    """기준 이슈와 사례의 원인·해결을 표로 나란히 놓고, 기록 범위와 겹치는 단서만 짚는다."""
    from backend.reports.comparison_history import treatment_history

    def action_cell(issue, text, origin):
        if not text and treatment_history(issue):
            return '완료된 기술 수정은 확인되지 않음. 처리 이력은 아래 참조'
        return _table_cell(text, origin, has_evidence=bool(_resolution_notes(issue) or issue.get('description')))

    base_cause, base_cause_origin = _recorded_cause(base_issue)
    base_fix, base_fix_origin = _recorded_fix(base_issue)
    rows = [f"| 기준 #{base_issue.get('id')} | {_table_cell(base_cause, base_cause_origin, has_evidence=bool(_resolution_notes(base_issue)))} | "
            f"{action_cell(base_issue, base_fix, base_fix_origin)} |"]
    case_causes = []
    for item in examples:
        cause, cause_origin = _recorded_cause(item)
        fix, fix_origin = _recorded_fix(item)
        case_causes.append((item, cause))
        rows.append(f"| #{item.get('id')} | {_table_cell(cause, cause_origin, has_evidence=bool(_resolution_notes(item)))} | {action_cell(item, fix, fix_origin)} |")

    recorded = sum(1 for _, cause in case_causes if cause)
    lines = [
        "**원인·해결 비교**",
        "",
        "| 이슈 | 원인 | 해결·조치 |",
        "|---|---|---|",
        *rows,
        "",
        f"- 원인 기록: 기준 이슈 {'있음' if base_cause else ('미확인' if _resolution_notes(base_issue) else '없음')}, 과거 사례 {recorded}/{len(examples)}건.",
    ]
    for item in [base_issue, *examples]:
        cause_text = _recorded_cause(item)[0]
        uncertain = _unconfirmed_cause(item)
        # 같은 노트의 서로 다른 원인 범위는 함께 남긴다. 이후 확정 필드가
        # 있는 경우 예전 다른 노트의 추측을 현재 미확정 원인으로 되살리지 않는다.
        shared_note = set(re.findall(r'\[노트 (\d+)\]', cause_text)) & set(re.findall(r'\[노트 (\d+)\]', uncertain))
        if uncertain and (not cause_text or shared_note) and normalize_for_match(uncertain) != normalize_for_match(cause_text):
            lines.append(f"- #{item.get('id')}: 미확정 원인 — {uncertain}")
        if caveat := _latest_resolution_caveat(item):
            lines.append(f"- #{item.get('id')}: 후속 상태 — {caveat} (노트).")
    for item in [base_issue, *examples]:
        lines.extend(_history_lines(item))
    base_terms = _clue_terms(f"{base_cause} {base_issue.get('summary') or ''}")
    for item, cause in case_causes:
        shared = sorted(base_terms & _clue_terms(cause)) if cause else []
        if shared:
            lines.append(f"- #{item.get('id')}: 기준 이슈와 겹치는 원인 단서 {', '.join(shared[:4])}. 같은 원인인지는 재현으로 확인하세요.")
    lines.append(
        "- 원인을 기록에서 확인하지 못한 사례는 해결·조치 내용으로만 비교했고, 같은 원인이라고 단정하지 않았습니다."
        if recorded < len(examples) else
        "- 기록된 원인끼리 비교했으며, 같은 원인인지는 재현으로 확인해야 합니다."
    )
    return lines


def _build_playbook_candidate(
    base_issue: Dict[str, Any], examples: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """현재 답변을 검수 가능한 플레이북 초안으로 넘길 구조를 만든다."""
    base_id = int(base_issue.get("id") or 0)
    base_summary = compact_text(base_issue.get("summary") or "이슈 대응", 160)
    description = compact_text(base_issue.get("description") or "", 1200)
    symptom = f"#{base_id} {base_summary}" if base_id else base_summary
    if description:
        symptom += f"\n\n{description}"
    resolution_lines = [
        f"- #{int(item.get('id') or 0)} {_summarize_resolution(item)}"
        for item in examples
        if int(item.get("id") or 0) > 0
    ]
    tags: List[str] = []
    for value in (
        base_issue.get("project"),
        base_issue.get("screen_number"),
        base_issue.get("platform"),
    ):
        tag = str(value or "").strip()
        if tag and tag not in tags:
            tags.append(tag)
    cause_lines = [
        f"- #{int(item.get('id') or 0)} {cause}"
        for item in [base_issue, *examples]
        if int(item.get("id") or 0) > 0 and (cause := _recorded_cause(item)[0])
    ]
    return {
        "title": f"{base_summary} 대응",
        "symptom": symptom,
        "cause": "\n".join(cause_lines),
        "resolution": "\n".join(resolution_lines),
        "verification": "",
        "tags": tags,
        "sourceIssueIds": [
            issue_id
            for issue_id in [base_id, *[int(item.get("id") or 0) for item in examples]]
            if issue_id > 0
        ],
    }


def build_similar_resolution_report(text: str, *, validated_plan: bool = False, model=None, api_key=None,
                                     topic_terms=None, constraints: Optional[dict] = None, chat=None) -> Optional[Dict[str, Any]]:
    """'#12345랑 비슷한 이슈 어떻게 해결했어?'류 질문에 사례+조치로 답한다."""
    constraints = deepcopy(constraints) if constraints is not None else extract_query_constraints(text)
    confirmed=validated_plan and constraints.get('unified_plan_validated') is True
    if not confirmed and not wants_similar_issue_search(text, constraints):
        return None
    if not confirmed and not _wants_resolution(text):
        return None
    base_ids = list(constraints.get("issue_ids") or [])
    if not base_ids:
        return None

    base_id = int(base_ids[0])
    raw_base = fetch_mantis_issue_by_id(base_id)
    if not raw_base:
        return {
            "content": f"기준 이슈 #{base_id}를 찾지 못했어요. 이슈 번호를 다시 확인해 주세요.",
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_similar_resolution", "status": "base_not_found",
                            "meta": {"base_id": base_id, "base_issue_ids": [base_id]}}],
            },
            "raw": {"mode": "deterministic_similar_resolution"},
        }

    base_issue = normalize_issue(raw_base, evidence_query=text)
    similar_query = build_similar_issue_search_text(base_issue) or text
    topic_text = ' '.join(str(term) for term in (topic_terms or [])) if confirmed else text
    cpu_focus = bool(re.search(r'(?<![A-Za-z])CPU(?![A-Za-z])', similar_query + ' ' + topic_text, re.I))
    if cpu_focus and 'cpu' not in similar_query.lower():
        similar_query = 'CPU ' + similar_query
    similar_constraints = {
        **constraints,
        "issue_ids": [],
        "exclude_issue_ids": base_ids,
        "max_issue_id_exclusive": base_id,
        # 비교 주제는 검증된 기준 원문에서 만든다. 원시 질문의 '더 오래된' 같은
        # 지시어를 다시 필수 검색어로 해석하지 않는다. 명시 조건은 아래 절을 유지한다.
        "semantic_focus_terms": ['CPU'] if cpu_focus else [],
        "require_semantic_focus": True,
        "resolution_evidence_query": similar_query + ' 원인 실제 수정 조치 완료 미해결',
        "filter_clauses": [
            clause
            for clause in constraints.get("filter_clauses", [])
            if clause.get("field") != "issue_id"
        ],
    }
    result = _search_similar_candidates(similar_query, similar_constraints,
                                        _issue_resource_symptom(base_issue))
    if result.get("status") not in {"ok"}:
        reason = result.get("reason") or "유사도 검색을 사용할 수 없습니다."
        return {
            "content": (
                attach_korean_particles(f"#{base_id}와(과) 비슷한 과거 사례를 찾는 중 문제가 있었어요.\n\n{reason}")
            ),
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_similar_resolution", "status": "search_failed",
                            "reason": reason, "meta": {"base_id": base_id, "base_issue_ids": [base_id]}}],
            },
            "raw": {"mode": "deterministic_similar_resolution"},
        }

    documents = [
        doc for doc in (result.get("documents") or [])
        if int(doc.get("issue_id") or 0) != base_id
    ]
    searches = result.get('searches') or []

    header = attach_korean_particles(
        f"🔍 #{base_id} \"{compact_text(base_issue.get('summary', ''), 60)}\"와(과) 비슷한 "
        f"과거 사례의 원인과 그때의 조치를 정리했어요."
    )
    if not documents:
        return {
            "content": '\n'.join([header, '', '지금 조건으로는 비슷한 과거 사례를 찾지 못했어요.',
                                    *_history_lines(base_issue)]),
            "sources": build_sources_from_issue_rows([base_issue]),
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_similar_resolution", "status": "no_results", "issues_list":[base_issue],
                            "meta": {"base_id": base_id, "base_issue_ids": [base_id], "displayed_count":0,
                                     "derived_query": similar_query[:300],"searches":searches,
                                     "refresh_budget": result.get('refresh_budget')}}],
            },
            "raw": {"mode": "deterministic_similar_resolution"},
        }

    lines = [header, ""]
    snapshot_fallback = sum(doc.get('latest_source') in {'mantis_snapshot', 'chroma_snapshot'} for doc in documents)
    if snapshot_fallback:
        saved_at = next((str(search.get('fetchedAt')) for search in searches if search.get('fetchedAt')), '수집 시각 미확인')
        lines.extend([f'일부 사례는 원문 갱신이 지연되어 저장본 근거로 비교했습니다. 저장본 수집 시각: {saved_at}', ''])
    example_rows: List[Dict[str, Any]] = []
    for index, doc in enumerate(documents, start=1):
        latest = doc.get("latest_issue")
        if latest:
            detail = latest
        else:
            # 라이브 refresh가 안 된 경우(스냅샷 폴백) 최소 정보라도 보여준다.
            detail = {
                "id": doc.get("issue_id"),
                "summary": "제목 미상",
                "project": "미상",
                "status": "상태 미상(스냅샷)",
                "notes": [],
                "history": [],
            }
        example_rows.append(detail)
        lines.extend([
            f"{index}. **#{detail.get('id')} {detail.get('summary', '제목 없음')}** "
            f"({customer_name(detail.get('project'))} · {status_name(detail.get('status'))})",
        ])

    from backend.reports.comparison_facts import review_comparison_facts
    comparison_review = review_comparison_facts([base_issue, *example_rows], model, api_key, **({'chat': chat} if chat else {}))
    from backend.reports.comparison_narrative import summarize_comparison
    narrative, summary_meta = summarize_comparison(text, [base_issue, *example_rows], model, api_key, **({'chat': chat} if chat else {}))
    if narrative:
        lines[2:2] = [narrative, '']
    elif summary_meta['status'] in {'unavailable', 'rejected'}:
        lines[2:2] = ['요약을 확정하지 못해 확인된 사실과 원문 근거 비교를 표시했습니다.', '']
    lines.extend(["", *_comparison_lines(base_issue, example_rows)])
    if comparison_review['status'] in {'unavailable', 'rejected'}:
        lines.append('- 일부 원문 근거의 추가 분류를 확정하지 못해 확인 가능한 기록만 표시했습니다.')
    lines.extend([
        "",
        "※ 유사도는 제목·증상 기준입니다. 원인·해결은 Mantis의 '사유 및 원인'·'조치 및 개선'을 먼저 쓰고, "
        "비어 있으면 본문·노트에서 확인한 내용을 출처와 함께 표시했습니다. QC·전달은 실제 기술 조치와 구분합니다.",
    ])

    sources = build_sources_from_issue_rows([base_issue, *example_rows])
    follow_up_sources = build_sources_from_issue_rows(example_rows)
    retrieval = {
        "query": text,
        "constraints": constraints,
        "stages": [{
            "source": "mantis_similar_resolution",
            "status": "ok",
            "issues_list": [base_issue, *example_rows],
            "meta": {
                "base_id": base_id,
                # 응답 검증이 머리글의 기준 이슈 번호를 근거 없는 번호로 보지 않게 한다.
                "base_issue_ids": [base_id],
                "derived_query": similar_query[:300],
                "displayed_count": len(example_rows),
                "searches": searches,
                "refresh_budget": result.get('refresh_budget'),
                "snapshot_fallback_count": snapshot_fallback,
                "comparison_review": comparison_review,
                "comparison_summary": summary_meta,
            },
        }],
    }
    return {
        "content": "\n".join(lines),
        "sources": sources,
        "sourcesForFollowUps": follow_up_sources,
        "playbookCandidate": _build_playbook_candidate(base_issue, example_rows),
        "retrieval": retrieval,
        "raw": {"mode": "deterministic_similar_resolution"},
    }

from backend.reports.comparison_common import (
    NOTE_SNIPPET_CHARS as NOTE_SNIPPET_CHARS,
    COMPARE_CELL_CHARS as COMPARE_CELL_CHARS,
    CAUSE_FIELD as CAUSE_FIELD,
    FIX_FIELD as FIX_FIELD,
    _BODY_DIAGNOSIS as _BODY_DIAGNOSIS,
    _CAUSE_SENTENCE as _CAUSE_SENTENCE,
    _SVN_FILE as _SVN_FILE,
    _NOTE_METADATA as _NOTE_METADATA,
    _QC_SUCCESS as _QC_SUCCESS,
    _GREETING as _GREETING,
    _UNCONFIRMED as _UNCONFIRMED,
    _CAUSE_NEGATION as _CAUSE_NEGATION,
    _FIX_NOT_DONE as _FIX_NOT_DONE,
    _COMPLETED_NOTE as _COMPLETED_NOTE,
    _PENDING_OUTCOME as _PENDING_OUTCOME,
    _COMPARE_STOPWORDS as _COMPARE_STOPWORDS,
    _NON_CAUSAL_CONTEXT as _NON_CAUSAL_CONTEXT,
    _CAUSE_EFFECT as _CAUSE_EFFECT,
    _CAUSE_LIMITATION as _CAUSE_LIMITATION,
    _OPERATIONAL_ACTION as _OPERATIONAL_ACTION,
    _NON_TECHNICAL_CHANGE as _NON_TECHNICAL_CHANGE,
    _RESOURCE as _RESOURCE,
    _RESOURCE_SYMPTOM as _RESOURCE_SYMPTOM,
    _issue_resource_symptom as _issue_resource_symptom,
    _recorded_section as _recorded_section,
    _resource_symptom as _resource_symptom,
    _summarize_resolution as _summarize_resolution,
    _without_access_lines as _without_access_lines,
    _clean_note as _clean_note,
    _unconfirmed as _unconfirmed,
    _note_sentences as _note_sentences,
    _cause_sentences as _cause_sentences,
    _is_experiment as _is_experiment,
    _is_consequence as _is_consequence,
    _unresolved_cause_note as _unresolved_cause_note,
    _recorded_cause as _recorded_cause,
    _note_reference as _note_reference,
    _progress_evidence as _progress_evidence,
    _recorded_fix as _recorded_fix,
    _latest_resolution_caveat as _latest_resolution_caveat,
    _unconfirmed_cause as _unconfirmed_cause,
    _resolution_notes as _resolution_notes,
    _table_cell as _table_cell,
    _clue_terms as _clue_terms,
)
from backend.reports.comparison_history import (
    _history_lines as _history_lines,
)
