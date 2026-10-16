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
from typing import Any, Dict, List, Optional, Tuple

from backend.display import customer_name, status_name
from backend.common import compact_text, normalize_for_match
from backend.sources.chroma import execute_chroma_search
from backend.template_questions import attach_korean_particles
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
COMPARE_CELL_CHARS = 220      # 원인·해결 비교표 한 칸 길이
CAUSE_FIELD = "사유 및 원인"
FIX_FIELD = "조치 및 개선"
# '원인'이라는 표제가 없어도 원문에 명시된 구현 불일치·제약은 보존한다.
_CAUSE_SENTENCE = re.compile(
    r"원인|때문|(?:으)?로\s*인해|(?:문제|장애|오류|누락|버그)(?:로|가\s*있어)|"
    r"실제로는|(?:되어야|돼야).*(?:되고|되어\s*있)|"
    r"(?:구조상|기술적인?\s*한계)|(?:데이터|이력).*(?:저장|보관).*(?:않|안\s*하|당일)"
)
_SVN_FILE = re.compile(r"^[AMUD]\s+(\S+\.[A-Za-z0-9]+)\s*$", re.M)
_NOTE_METADATA = re.compile(
    r"^(?:svn\s|리비전\s*[:：]|작성자\s*[:：]|날짜\s*[:：]|메시지\s*[:：]|"
    r"[a-z][a-z0-9+.-]*://|[A-Za-z]:\\)", re.I
)
_QC_SUCCESS = re.compile(r"QC\s*결과.*(?:정상|성공|통과|OK)", re.I)
# 인사말과 '○○ 과장입니다' 같은 자기소개. 직급이 붙은 경우만 지워 '원인은 …입니다' 같은 내용은 남긴다.
_GREETING = re.compile(
    r"안녕하세요[.!~]*\s*|[^.\n]{0,25}(?:부장|차장|과장|대리|주임|선임|책임|수석|사원|팀장|매니저|이사|대표)(?:님)?입니다[.!~]*\s*"
)
_UNCONFIRMED = re.compile(
    r"요청(?:드|합|함|하|해|$)|부탁|예정|계획|검토\s*중|진행\s*중|분석\s*중|확인\s*중|"
    r"추정|의심|가능성|미상|불명|모름|미확인|필요|무엇인지|때문인지|"
    r"미완료|미해결|"
    r"(?:수정|조치|반영|적용|패치|배포|조사)\s*중|하겠|모르|"
    r"것\s*같|인\s*듯|인가요|입니까|일까요|맞나요|(?:할|알)\s*수\s*없|"
    r"해야|바랍|주세요|해줘"
)
_CAUSE_NEGATION = re.compile(r"(?:원인|때문)[^.\n]*(?:아닙|아니었|아닌)")
_FIX_NOT_DONE = re.compile(r"불가|한계|실패|않|못했|못하|미(?:반영|적용|배포|수정|처리)|안\s*(?:됨|됐|되|했|하)|이면|하면|한다면|된다면|되면|했으면")
_COMPLETED_NOTE = re.compile(
    r"완료|종료(?:합니다|함|했|\s*$)|"
    r"(?:해결|수정|조치|반영|변경|패치|개선|배포|조정)"
    r"(?:\s*(?:함|했|하였|됨|되었|해두)|해서|하여.*(?:푸시|전달|반영|완료)|\s*$)|"
    r"추가\s*(?:했|하였|함|완료|반영|됨)|적용\s*(?:했|하였|되었)|전달\s*(?:했|하였|드렸|드립니다)|"
    r"(?:패치|파일|고객사).{0,40}전달\s*(?:됨|함|$)"
)
_PENDING_OUTCOME = re.compile(
    r"(?:배포|반영|적용|수정|해결|조치|전달).*(?:예정|미완료|미해결|않|못했|못하|진행\s*중)|"
    r"미(?:적용|반영|배포|수정|해결)|(?:배포|수정|조치|반영|적용)\s*중"
)
_COMPARE_STOPWORDS = frozenset({"현상", "문제", "오류", "수정", "요청", "확인", "관련", "발생", "처리", "원인", "화면", "경우"})
_GENERIC_SIMILAR_FOCUS_TERMS = frozenset({
    "찾아", "찾아서", "원인", "해결", "내용", "비교", "비교해줘", "과거", "사례",
})


def _wants_resolution(text: str) -> bool:
    normalized = normalize_for_match(text)
    return any(normalize_for_match(marker) in normalized for marker in RESOLUTION_MARKERS)


def _summarize_resolution(detail: Dict[str, Any]) -> str:
    """정규화된 이슈에서 '그때 어떻게 처리했는지' 한두 줄 요약을 만든다."""
    status = status_name(detail.get("status"))

    # 상태 변경 이력 중 마지막으로 상태가 바뀐 사건을 조치 시점으로 본다.
    resolved_event = None
    for item in detail.get("history") or []:
        change = str(item.get("change") or "")
        field = str(item.get("field") or "")
        if "=>" in change and ("상태" in field or "status" in field.casefold()):
            resolved_event = item

    parts = [f"상태 {status}"]
    if resolved_event:
        when = str(resolved_event.get("created_at") or "")[:10]
        change = str(resolved_event.get("change") or "").replace("=>", "→")
        parts.append(f"상태 변경 {change}{f' ({when})' if when else ''}")
    recorded_fix, origin = _recorded_fix(detail, max_chars=NOTE_SNIPPET_CHARS)
    if recorded_fix:
        label = "기록된 조치" if origin == "field" else "마지막 조치"
        parts.append(f"{label}: {recorded_fix}")
    else:
        parts.append("기록에서 완료된 해결·조치를 확인하지 못했습니다.")
    if caveat := _latest_resolution_caveat(detail):
        parts.append(f"후속 상태: {caveat}")
    return " · ".join(parts)


def _clean_note(text: str, detail: Dict[str, Any]) -> str:
    """노트 앞의 인사말·'0029941: [제목]' 반복을 걷어내 실제 내용만 남긴다."""
    cleaned = _GREETING.sub("", str(text or ""))
    issue_id = str(detail.get("id") or "")
    summary = str(detail.get("summary") or "").strip()
    if issue_id:
        # 메일 회신에 붙는 '0029941: [제목]' 반복은 노트 어디에 있어도 걷어낸다.
        title = re.escape(summary) if summary else ""
        cleaned = re.sub(rf"0*{re.escape(issue_id)}\s*[:：]\s*{title}", " ", cleaned)
    if summary:
        cleaned = cleaned.replace(summary, " ")
    return re.sub(r"[^\S\n]+", " ", cleaned).strip(" .-~\n")


def _unconfirmed(text: str, *, cause: bool = False) -> bool:
    # '예외를 처리하지 않아 발생한 오류'는 원인이다. 완료 조치의 부정과 구분한다.
    return bool(_UNCONFIRMED.search(text) or (_CAUSE_NEGATION if cause else _FIX_NOT_DONE).search(text))


def _note_sentences(text: str, *, join_wrapped_lines: bool = False) -> List[str]:
    """파일명·메서드·소수점은 유지하고 문장 끝의 구두점만 나눈다."""
    if join_wrapped_lines:
        text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    return [part.strip(" .-~·:") for part in re.split(r"[。!?\n]+|\.(?=\s|$)", text)
            if part.strip(" .-~·:")]


def _recorded_cause(detail: Dict[str, Any]) -> Tuple[str, str]:
    """확인 가능한 원인 원문과 출처. 빈 값은 추출 실패이며 기록 부재와 다르다."""
    field = str((detail.get("resolution_fields") or {}).get(CAUSE_FIELD) or "").strip()
    if field and not _unconfirmed(field, cause=True):
        return compact_text(field, COMPARE_CELL_CHARS), "field"
    for note in reversed(_resolution_notes(detail)):
        cleaned = _clean_note(str(note.get("text") or ""), detail)
        for sentence in _note_sentences(cleaned, join_wrapped_lines=True):
            if len(sentence) >= 8 and _CAUSE_SENTENCE.search(sentence) and not _unconfirmed(sentence, cause=True):
                return compact_text(sentence, COMPARE_CELL_CHARS), "note"
    return "", ""


def _recorded_fix(detail: Dict[str, Any], *, max_chars: int = COMPARE_CELL_CHARS) -> Tuple[str, str]:
    """확인된 조치와 QC/전달 단계만 기록하며 전달을 운영 배포로 바꾸지 않는다."""
    field = str((detail.get("resolution_fields") or {}).get(FIX_FIELD) or "").strip()
    if field and not _unconfirmed(field):
        return compact_text(field, max_chars), "field"
    delivery = ""
    for note in reversed(_resolution_notes(detail)):
        raw = str(note.get("text") or "").strip()
        if not raw or raw.startswith("이슈노트 추가됨"):
            continue
        cleaned = _clean_note(raw, detail)
        if re.search(r"\[\s*고객사\s*전달\s*\]", cleaned) and not _unconfirmed(cleaned):
            delivery = "고객사 전달 기록 있음"
        # SVN 변경 링크 자체에는 조치 의미가 없다. 실제 변경 파일만 제한적으로 표시한다.
        changed_files = _SVN_FILE.findall(cleaned)
        if changed_files and re.search(r"svn\s|리비전\s*:", cleaned, re.I):
            change = "소스 변경 기록: " + ", ".join(changed_files[:3])
            return compact_text(". ".join(filter(None, [change, delivery])), max_chars), "note"
        text = "\n".join(line for line in cleaned.splitlines()
                         if not _NOTE_METADATA.search(line.strip())
                         and not re.fullmatch(r"\[\s*고객사\s*전달\s*\].*", line.strip()))
        confirmed = [part for part in _note_sentences(text)
                     if (_COMPLETED_NOTE.search(part) or _QC_SUCCESS.search(part)) and not _unconfirmed(part)]
        if confirmed:
            action = ". ".join(confirmed)
            if delivery and "전달" not in action:
                action += ". " + delivery
            return compact_text(action, max_chars), "note"
    return (delivery, "note") if delivery else ("", "")


def _latest_resolution_caveat(detail: Dict[str, Any]) -> str:
    """과거 완료 기록을 최신 미해결·배포 예정 상태로 오해하지 않도록 한정한다."""
    for note in reversed(_resolution_notes(detail)):
        sentences = _note_sentences(_clean_note(str(note.get("text") or ""), detail))
        pending = [part for part in sentences if _PENDING_OUTCOME.search(part)]
        if pending:
            return compact_text(". ".join(pending), NOTE_SNIPPET_CHARS)
        if any(_COMPLETED_NOTE.search(part) and not _unconfirmed(part) for part in sentences):
            break
    return ""


def _resolution_notes(detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    # 원문 발췌는 표시용 노트의 길이 제한 밖에 있는 근거를 보완한다.
    excerpts = [item for item in [*(detail.get("matching_evidence") or []),
                                  *((detail.get("answer_evidence") or {}).get("excerpts") or [])]
                if str(item.get("field") or "").split(":", 1)[0] == "note"]
    notes: Dict[str, Dict[str, Any]] = {}
    for index, item in enumerate([*excerpts, *(detail.get("notes") or [])]):
        note_id = str(item.get("id") or item.get("note_id") or
                      str(item.get("field") or "").partition(":")[2])
        key = note_id or f"position:{index}"
        text = str(item.get("text") or "")
        if key in notes:
            previous = str(notes[key].get("text") or "")
            if text in previous:
                text = previous
            elif previous not in text:
                text = previous + "\n\n" + text
        notes[key] = {**notes.get(key, {}), **item, "id": note_id, "text": text}
    # Mantis ID는 시간순이다. ID 없는 입력은 원래 순서를 유지한다.
    return sorted(notes.values(), key=lambda item: int(str(item.get("id") or "0"))
                  if str(item.get("id") or "0").isdigit() else 0)


def _table_cell(text: str, origin: str, *, has_evidence: bool = False) -> str:
    if not text:
        return "기록에서 확인되지 않음" if has_evidence else "기록 없음"
    cell = re.sub(r"\s+", " ", text).replace("|", "/")
    return f"{cell} (노트)" if origin == "note" else cell


def _clue_terms(text: str) -> set[str]:
    return {
        token for token in re.findall(r"[가-힣A-Za-z0-9]{2,}", text)
        if token not in _COMPARE_STOPWORDS and not token.isdigit()
    }


def _comparison_lines(base_issue: Dict[str, Any], examples: List[Dict[str, Any]]) -> List[str]:
    """기준 이슈와 사례의 원인·해결을 표로 나란히 놓고, 기록 범위와 겹치는 단서만 짚는다."""
    base_cause, base_cause_origin = _recorded_cause(base_issue)
    base_fix, base_fix_origin = _recorded_fix(base_issue)
    rows = [f"| 기준 #{base_issue.get('id')} | {_table_cell(base_cause, base_cause_origin, has_evidence=bool(_resolution_notes(base_issue)))} | "
            f"{_table_cell(base_fix, base_fix_origin, has_evidence=bool(_resolution_notes(base_issue)))} |"]
    case_causes = []
    for item in examples:
        cause, cause_origin = _recorded_cause(item)
        fix, fix_origin = _recorded_fix(item)
        case_causes.append((item, cause))
        rows.append(f"| #{item.get('id')} | {_table_cell(cause, cause_origin, has_evidence=bool(_resolution_notes(item)))} | {_table_cell(fix, fix_origin, has_evidence=bool(_resolution_notes(item)))} |")

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
        if caveat := _latest_resolution_caveat(item):
            lines.append(f"- #{item.get('id')}: 후속 상태 — {caveat} (노트).")
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
                            "meta": {"base_id": base_id, "base_issue_ids": [base_id]}}],
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

    header = attach_korean_particles(
        f"🔍 #{base_id} \"{compact_text(base_issue.get('summary', ''), 60)}\"와(과) 비슷한 "
        f"과거 사례의 원인과 그때의 조치를 정리했어요."
    )
    if not documents:
        return {
            "content": header + "\n\n지금 조건으로는 비슷한 과거 사례를 찾지 못했어요.",
            "sources": [],
            "retrieval": {
                "query": text,
                "constraints": constraints,
                "stages": [{"source": "mantis_similar_resolution", "status": "no_results",
                            "meta": {"base_id": base_id, "base_issue_ids": [base_id], "derived_query": similar_query[:300]}}],
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
            f"({customer_name(detail.get('project'))})",
            f"   - 그때 처리: {_summarize_resolution(detail)}",
        ])

    lines.extend(["", *_comparison_lines(base_issue, example_rows)])
    lines.extend([
        "",
        "※ 유사도는 제목·증상 기준입니다. 원인·해결은 Mantis의 '사유 및 원인'·'조치 및 개선'을 먼저 쓰고, "
        "비어 있으면 노트에서 뽑아 (노트)로 표시했습니다.",
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
                # 응답 검증이 머리글의 기준 이슈 번호를 근거 없는 번호로 보지 않게 한다.
                "base_issue_ids": [base_id],
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
