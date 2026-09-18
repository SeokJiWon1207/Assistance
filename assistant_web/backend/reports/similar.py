"""유사사례 해결책 리포트 — 비슷한 과거 이슈 + 그때의 조치 요약(상위 계층).

기준 이슈와 의미가 비슷한 과거 이슈를 ChromaDB 임베딩으로 찾은 뒤, 각 사례가 실제로
어떻게 처리됐는지(상태·마지막 노트·상태 변경 이력)를 요약해 함께 보여준다. 단순히
'비슷한 이슈 목록'을 넘어 축적된 해결 지식을 재사용하도록 돕는다.

execute_chroma_search(semantic=True)는 후보 문서를 라이브 Mantis로 refresh해
latest_issue(notes/history 포함)를 채워주므로, 여기서는 추가 조회 없이 그 결과에서
해결 맥락을 뽑아낸다.

의존: chroma(유사도 검색) + mantis(기준 이슈 조회/정규화) + understanding(제약/유사질의).
"""

from typing import Any, Dict, List, Optional

from backend.common import compact_text, normalize_for_match
from backend.sources.chroma import execute_chroma_search
from backend.sources.mantis import (
    build_sources_from_issue_rows,
    fetch_mantis_issue_by_id,
    normalize_issue,
)
from backend.understanding import (
    build_keyword_terms,
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
NOTE_SNIPPET_CHARS = 220     # 사례별 조치 요약 길이
_GENERIC_SIMILAR_FOCUS_TERMS = frozenset({
    "찾아", "찾아서", "원인", "해결", "내용", "비교", "비교해줘", "과거", "사례",
})


def _wants_resolution(text: str) -> bool:
    normalized = normalize_for_match(text)
    return any(normalize_for_match(marker) in normalized for marker in RESOLUTION_MARKERS)


def _summarize_resolution(detail: Dict[str, Any]) -> str:
    """정규화된 이슈에서 '그때 어떻게 처리했는지' 한두 줄 요약을 만든다."""
    status = str(detail.get("status") or "상태 미상")

    # 상태 변경 이력 중 마지막으로 상태가 바뀐 사건을 조치 시점으로 본다.
    resolved_event = None
    for item in detail.get("history") or []:
        change = str(item.get("change") or "")
        field = str(item.get("field") or "")
        if "=>" in change and ("상태" in field or "status" in field.casefold()):
            resolved_event = item

    # 마지막으로 의미 있는 노트를 조치 내용으로 쓴다(보통 처리 결과가 담긴다).
    last_note_text = ""
    for note in reversed(detail.get("notes") or []):
        note_text = str(note.get("text") or "").strip()
        if note_text and not note_text.startswith("이슈노트 추가됨"):
            last_note_text = note_text
            break

    parts = [f"상태 {status}"]
    if resolved_event:
        when = str(resolved_event.get("created_at") or "")[:10]
        change = str(resolved_event.get("change") or "").replace("=>", "→")
        parts.append(f"상태 변경 {change}{f' ({when})' if when else ''}")
    if last_note_text:
        parts.append(f"마지막 조치: {compact_text(last_note_text, NOTE_SNIPPET_CHARS)}")
    if not resolved_event and not last_note_text:
        parts.append("기록된 처리 노트가 없습니다.")
    return " · ".join(parts)


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
    return {
        "title": f"{base_summary} 대응",
        "symptom": symptom,
        "cause": "",
        "resolution": "\n".join(resolution_lines),
        "verification": "",
        "tags": tags,
        "sourceIssueIds": [
            issue_id
            for issue_id in [base_id, *[int(item.get("id") or 0) for item in examples]]
            if issue_id > 0
        ],
    }


def build_similar_resolution_report(text: str) -> Optional[Dict[str, Any]]:
    """'#12345랑 비슷한 이슈 어떻게 해결했어?'류 질문에 사례+조치로 답한다."""
    constraints = extract_query_constraints(text)
    if not wants_similar_issue_search(text, constraints):
        return None
    if not _wants_resolution(text):
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
                            "meta": {"base_id": base_id}}],
            },
            "raw": {"mode": "deterministic_similar_resolution"},
        }

    base_issue = normalize_issue(raw_base)
    similar_query = build_similar_issue_search_text(base_issue) or text
    focus_terms = [
        term
        for term in build_keyword_terms(text, constraints)
        if normalize_for_match(term) not in {
            normalize_for_match(value) for value in _GENERIC_SIMILAR_FOCUS_TERMS
        }
    ]
    similar_constraints = {
        **constraints,
        "issue_ids": [],
        "exclude_issue_ids": base_ids,
        "max_issue_id_exclusive": base_id,
        # "원인과 해결 내용을 비교"의 원인·해결은 출력 형식이지 유사도 주제가
        # 아니다. 이런 제어어로 후보를 거르면 오래되고 무관한 이슈만 남는다.
        "semantic_focus_terms": focus_terms,
        "filter_clauses": [
            clause
            for clause in constraints.get("filter_clauses", [])
            if clause.get("field") != "issue_id"
        ],
    }
    result = execute_chroma_search(similar_query, SIMILAR_TOP_N, similar_constraints, semantic=True)
    if result.get("status") not in {"ok"}:
        reason = result.get("reason") or "유사도 검색을 사용할 수 없습니다."
        return {
            "content": (
                f"#{base_id}과 비슷한 과거 사례를 찾는 중 문제가 있었어요.\n\n{reason}"
            ),
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_similar_resolution", "status": "search_failed",
                            "reason": reason}],
            },
            "raw": {"mode": "deterministic_similar_resolution"},
        }

    documents = [
        doc for doc in (result.get("documents") or [])
        if int(doc.get("issue_id") or 0) != base_id
    ]

    header = (
        f"🔍 #{base_id} \"{compact_text(base_issue.get('summary', ''), 60)}\"와 비슷한 "
        f"과거 사례와 그때의 조치를 정리했어요."
    )
    if not documents:
        return {
            "content": header + "\n\n지금 조건으로는 비슷한 과거 사례를 찾지 못했어요.",
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_similar_resolution", "status": "no_results",
                            "meta": {"base_id": base_id, "derived_query": similar_query[:300]}}],
            },
            "raw": {"mode": "deterministic_similar_resolution"},
        }

    lines = [header, ""]
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
            f"({detail.get('project', '미상')})",
            f"   - 그때 처리: {_summarize_resolution(detail)}",
        ])

    lines.extend([
        "",
        "※ 유사도는 제목·증상 기준이며, 조치 요약은 각 이슈의 최신 상태·노트에서 뽑았습니다.",
    ])

    sources = build_sources_from_issue_rows(example_rows)
    retrieval = {
        "query": text,
        "constraints": constraints,
        "stages": [{
            "source": "mantis_similar_resolution",
            "status": "ok",
            "issues_list": example_rows,
            "meta": {
                "base_id": base_id,
                "derived_query": similar_query[:300],
                "displayed_count": len(example_rows),
            },
        }],
    }
    return {
        "content": "\n".join(lines),
        "sources": sources,
        "sourcesForFollowUps": sources,
        "playbookCandidate": _build_playbook_candidate(base_issue, example_rows),
        "retrieval": retrieval,
        "raw": {"mode": "deterministic_similar_resolution"},
    }
