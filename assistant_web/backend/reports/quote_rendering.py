"""검증된 인용과 출처를 답변으로 표시한다."""
import re
from typing import Any
from backend.evidence.excerpts import safe_excerpts, _MAX_CORE_CHARS, _NOTE_BOILERPLATE
from backend.evidence.references import _label, source_label
from backend.evidence.stages import _QC_DONE, _confirmed_qc_done
from backend.evidence.quote_context import _issues, TOPIC_TITLES, UNANSWERED_TEXT
from backend.reports.quote_conclusions import _conclusion_lines

from backend.reports.quote_selection import _core_choice, _NOTE_STATE

from backend.evidence.stages import _DELIVERED as _DELIVERED


_OBSERVED_FAILURE = re.compile(r'조회.{0,20}(?:안되|않)|빈\s*화면|표시.{0,20}(?:안되|않)')


_SPECULATIVE = re.compile(r'왠지|추정|필요할\s*것|같(?:은데|아요)')


def _milestone_choice(rows: list[dict[str, Any]], pattern: re.Pattern[str]) -> dict[str, Any] | None:
    matches = [row for row in rows if len(row["text"]) <= _MAX_CORE_CHARS
               and pattern.search(row["text"])
               and (pattern is not _QC_DONE or _confirmed_qc_done(row["text"]))
               and not _NOTE_BOILERPLATE.fullmatch(row["text"].strip())]
    return matches[-1] if matches else None


def _whole_note_choice(rows: list[dict[str, Any]], issue: dict[str, Any]) -> dict[str, Any] | None:
    """짧은 대조 설명이 빈 줄로 쪼개졌으면 동일 원문 노트를 통째로 인용한다."""
    if not rows:
        return None
    newest = max(rows, key=lambda row: (str(row.get('created_at') or ''),
                                        int(str(row.get('note_id'))) if str(row.get('note_id')).isdigit() else 0))
    same_source = [row for row in rows if row['field'] == newest['field']]
    if len(same_source) < 2 or not newest['field'].startswith('note:'):
        return None
    for excerpt in safe_excerpts(issue):
        source = excerpt.get('text', '').replace('\r\n', '\n').strip()
        if (excerpt.get('field') == newest['field'] and len(source) <= _MAX_CORE_CHARS
                and re.search(r'이어야\s*하지만|그러나|반면|대신', source)
                and all(row['text'].strip() in source for row in same_source)):
            return {**newest, 'text': source}
    return None


def _append_evidence(lines: list[str], title: str, evidence_rows: list[dict[str, Any]],
                     issues: dict[int, dict[str, Any]], selected_ids: list[int],
                     *, unanswered: bool = False,
                     rendered_quotes: set[tuple[int, str, str]] | None = None,
                     deduplicate: bool = False,
                     initial_sources: set[tuple[int, str]] | None = None) -> bool:
    if title:
        lines.extend([f"### {title}", ""])
    if unanswered or not evidence_rows:
        lines.extend([UNANSWERED_TEXT, ""])
    groups: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for evidence in evidence_rows:
        issue_id = evidence["issue_id"]
        if issue_id not in selected_ids:
            return False
        group = groups.setdefault((issue_id, evidence["field"]), [])
        if not any(row["text"] == evidence["text"] for row in group):
            group.append(evidence)
    previous_issue_id = None
    for (issue_id, _field), group in groups.items():
        evidence = group[0]
        if issue_id != previous_issue_id:
            issue = issues.get(issue_id, {})
            issue_title = issue.get("summary") or issue.get("title") or ""
            lines.extend([f"**#{issue_id}**" + (f" {_label(issue_title)}" if issue_title else ""), ""])
            previous_issue_id = issue_id
        metadata = []
        if initial_sources and (issue_id, evidence["field"]) in initial_sources:
            metadata.append("초기 기록")
        metadata.append(source_label(evidence))
        if evidence.get("created_at"):
            metadata.append(f"기록 시각: {_label(evidence['created_at'])}")
        if evidence.get("reporter"):
            metadata.append(f"작성자: {_label(evidence['reporter'])}")
        lines.extend([" · ".join(metadata), ""])
        referenced = False
        for row in group:
            text = row["text"]
            key = (issue_id, row["field"], text)
            if (deduplicate and rendered_quotes is not None
                    and any(other_issue == issue_id and other_field == row['field'] and text in other_text
                            for other_issue, other_field, other_text in rendered_quotes)):
                if not referenced:
                    lines.extend(["위에서 인용한 원문 참조", ""])
                    referenced = True
                continue
            # 원문의 Markdown/코드도 문자 그대로 보이며 따옴표 인용으로 구분한다.
            fence = "`" * max(3, 1 + max((len(m.group()) for m in re.finditer(r"`+", text)), default=0))
            lines.append(f"> {fence}text")
            lines.extend("> " + line for line in text.split("\n"))
            lines.extend([f"> {fence}", ""])
            if rendered_quotes is not None:
                rendered_quotes.add(key)
    return True


def render_quote_answer(validated: dict[str, Any], grounding: dict[str, Any], *, question: str = "",
                        previous_questions: list[str] | None = None) -> str:
    """검증된 인용만 고정 제목과 출처 표시로 렌더한다. 자유 생성 요약은 읽지 않는다."""
    if not validated.get("valid"):
        return ""
    issues = _issues(grounding)
    lines: list[str] = ['### 핵심 근거', '']
    notices = ['완료·QC·전달·운영 반영 여부는 각 인용에 적힌 범위와 시점에서만 확인됩니다.', '']
    if any(len(safe_excerpts(issues.get(issue_id, {}))) < len(issues.get(issue_id, {}).get('answer_evidence', {}).get('excerpts', []))
           for issue_id in validated.get('selected_issue_ids', [])):
        notices.extend(['접속정보 또는 다른 이슈 내용이 섞인 노트·필드는 대상 범위만 인용했습니다.', ''])
    if any(issues.get(issue_id, {}).get('answer_evidence', {}).get('complete') is False
           for issue_id in validated.get('selected_issue_ids', [])):
        notices.extend(['긴 원문에서 선택된 근거를 확인한 답변이며, 전체 이력이 모두 포함된 것은 아닙니다.', ''])
    plans: list[tuple[str, list[dict[str, Any]], list[dict[str, Any]], bool]] = []
    for section in validated.get("sections", []):
        topic = section.get("topic")
        if topic not in TOPIC_TITLES:
            return ""
        evidence_rows = section.get("evidence", [])
        if topic == "timeline":
            evidence_rows = sorted(evidence_rows, key=lambda row: (
                str(row.get("created_at") or ""),
                int(str(row.get("note_id"))) if str(row.get("note_id")).isdigit() else 0,
            ))
        unique_rows = []
        seen_rows = set()
        for row in evidence_rows:
            key = (row["issue_id"], row["field"], row["text"])
            if key not in seen_rows:
                unique_rows.append(row)
                seen_rows.add(key)
        evidence_rows = unique_rows
        preview: list[dict[str, Any]] = []
        if topic not in ("timeline", "background") and evidence_rows:
            for issue_id in validated.get("selected_issue_ids", []):
                rows = [row for row in evidence_rows if row["issue_id"] == issue_id]
                if not rows or len(preview) >= 3:
                    continue
                newest = max(rows, key=lambda row: (
                    str(row.get("created_at") or ""),
                    int(str(row.get("note_id"))) if str(row.get("note_id")).isdigit() else 0,
                ))
                source = (newest["issue_id"], newest["field"])
                preview.extend(row for row in rows if (row["issue_id"], row["field"]) == source
                               and len(preview) < 3)
        detail = [row for row in evidence_rows if row not in preview]
        plans.append((topic, preview, detail, section.get("unanswered", False)))
    selected_ids = validated.get("selected_issue_ids", [])
    from backend.reports.direct_conclusion import direct_conclusion
    conclusion = direct_conclusion(question, plans, selected_ids, issues, previous_questions,
                                   validated.get('answer_items'))
    if not conclusion:
        conclusion = _conclusion_lines(plans, selected_ids, issues)
        if question and not re.search(r'운영|반영|배포', question):
            conclusion = [line for line in conclusion if line != '선택된 근거만으로 운영 반영 완료는 확정할 수 없습니다.']
    lines = conclusion + lines
    rendered_quotes: set[tuple[int, str, str]] = set()
    initial_sources: set[tuple[int, str]] = set()
    dated_by_issue: dict[int, list[dict[str, Any]]] = {}
    for _topic, preview, detail, _unanswered in plans:
        for row in preview + detail:
            if row.get("created_at"):
                dated_by_issue.setdefault(row["issue_id"], []).append(row)
    for issue_rows in dated_by_issue.values():
        ordered = sorted(issue_rows, key=lambda row: (
            str(row["created_at"]),
            int(str(row.get("note_id"))) if str(row.get("note_id")).isdigit() else 0,
        ))
        if ordered[0]["created_at"] != ordered[-1]["created_at"] or (
                ordered[0].get("note_id") != ordered[-1].get("note_id")):
            initial_sources.add((ordered[0]["issue_id"], ordered[0]["field"]))
    timeline = next((detail for topic, _preview, detail, _unanswered in plans if topic == "timeline"), [])
    dated_timeline = [row for row in timeline if row.get("created_at")]
    notes_by_issue: dict[int, list[dict[str, Any]]] = {}
    for row in dated_timeline:
        notes_by_issue.setdefault(row["issue_id"], []).append(row)
    latest_by_issue: dict[int, dict[str, Any]] = {}
    for issue_id, rows in notes_by_issue.items():
        last = rows[-1]
        same_note = [row for row in rows if row["field"] == last["field"]
                     and row["created_at"] == last["created_at"]]
        observed_rows = [row for row in same_note if len(row['text']) <= _MAX_CORE_CHARS
                         and _OBSERVED_FAILURE.search(row['text']) and not _SPECULATIVE.search(row['text'])]
        status_rows = [row for row in same_note if len(row["text"]) <= _MAX_CORE_CHARS
                       and _NOTE_STATE.search(row["text"])
                       and (not _NOTE_BOILERPLATE.search(row["text"]) or "\n" in row["text"])]
        other_rows = [row for row in same_note if len(row["text"]) <= _MAX_CORE_CHARS
                      and not _NOTE_BOILERPLATE.search(row["text"])]
        if observed_rows or status_rows or other_rows:
            latest_by_issue[issue_id] = (observed_rows or status_rows or other_rows)[-1]
    core: list[tuple[str, dict[str, Any]]] = []
    core.extend(("마지막 기록에서 확인되는 상태", row) for row in latest_by_issue.values())
    for wanted in ("cause",):
        for topic, preview, detail, _unanswered in plans:
            if topic == wanted:
                rows = preview + detail
                chosen = _whole_note_choice(rows, issues.get(rows[0]['issue_id'], {})) if rows else None
                chosen = chosen or _core_choice(rows)
                if chosen:
                    core.append((f"{TOPIC_TITLES[topic]} 근거", chosen))
    qc = _milestone_choice(timeline, _QC_DONE)
    delivery = _milestone_choice(timeline, _DELIVERED)
    if qc:
        core.append(("QC 확인 기록", qc))
    if delivery:
        core.append(("전달 기록", delivery))
    technical_rows = [row for topic, preview, detail, _unanswered in plans
                      if topic in ('action', 'technical') for row in preview + detail]
    technical_rows = [row for row in technical_rows if re.search(r'\b(?:FID|TR)\b|\b\d{3,4}\b|구분자|매매구분', row['text'], re.I)]
    specific_fields = [row for row in technical_rows
                       if re.search(r'\(\d{2,4}\)|\b(?:FID|TR)\s*[:=]', row['text'], re.I)]
    technical_choice = _core_choice(specific_fields) or _core_choice(technical_rows)
    if technical_choice:
        core.append(('구분·구현 근거', technical_choice))
    for wanted in ("action", "technical", "conditions", "actors"):
        for topic, preview, detail, _unanswered in plans:
            if topic == wanted:
                chosen = _core_choice(preview + detail)
                if chosen:
                    core.append((f"{TOPIC_TITLES[topic]} 근거", chosen))
    seen_core: set[tuple[int, str, str]] = set()
    for label, row in core:
        key = (row["issue_id"], row["field"], row["text"])
        if key in seen_core or any(issue_id == row['issue_id'] and field == row['field']
                                   and row['text'] in text for issue_id, field, text in seen_core):
            continue
        seen_core.add(key)
        lines.extend([f"**{label}**", ""])
        if not _append_evidence(lines, "", [row], issues, selected_ids,
                                rendered_quotes=rendered_quotes, initial_sources=initial_sources):
            return ""
        if len(seen_core) == 4:
            break
    lines.extend(notices)
    for topic, preview, _detail, unanswered in plans:
        if topic in ("timeline", "background"):
            continue
        if not _append_evidence(lines, TOPIC_TITLES[topic], preview, issues, selected_ids,
                                unanswered=unanswered, rendered_quotes=rendered_quotes,
                                deduplicate=True, initial_sources=initial_sources):
            return ""
    # Each displayed passage also carries issue and note metadata, so count
    # that visible overhead when deciding whether the full history needs folding.
    collapse_details = sum(len(row['text']) + 90 for _topic, _preview, detail, _unanswered in plans
                           for row in detail) > 800
    if collapse_details:
        lines.extend(['<details>', '<summary>전체 이력과 원문 근거 보기</summary>', ''])
    for topic, _preview, detail, unanswered in plans:
        if topic not in ("timeline", "background") and not detail:
            continue
        title = TOPIC_TITLES[topic] if topic in ("timeline", "background") else f"{TOPIC_TITLES[topic]} 전체 근거"
        if not _append_evidence(lines, title, detail, issues, selected_ids,
                                unanswered=unanswered and topic in ("timeline", "background"),
                                rendered_quotes=rendered_quotes, deduplicate=True,
                                initial_sources=initial_sources):
            return ""
    if collapse_details:
        lines.extend(['</details>', ''])
    return "\n".join(lines).strip()
