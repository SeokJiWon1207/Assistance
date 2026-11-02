"""모델이 선택한 인용을 이슈·필드 원문에 결속하고 검증한다.

이는 인용의 출처와 표시 문맥을 보호한다. 관련 근거의 완전성이나 원문 자체의
사실성을 보증하지 않으며, 검색 범위 밖의 문맥은 복원할 수 없다.
"""
from typing import Any


def _match_evidence(item: dict[str, Any], issue: dict[str, Any]) -> list[dict[str, Any]]:
    quote = item["quote"].replace("\r\n", "\n")
    matched = []
    for excerpt in safe_excerpts(issue):
        if not isinstance(excerpt, dict) or excerpt.get("field") != item["field"]:
            continue
        text = excerpt.get("text")
        if not isinstance(text, str):
            continue
        for context in _paragraphs(text.replace("\r\n", "\n"), quote):
            # 문맥을 다시 자르면 부정/요청이 탈락할 수 있으므로 긴 문단은 거부한다.
            if len(context) > _MAX_CONTEXT_CHARS:
                return []
            row = {"issue_id": item["issue_id"], "field": item["field"], "quote": quote, "text": context}
            for key in ("note_id", "created_at", "reporter"):
                if excerpt.get(key) not in (None, ""):
                    row[key] = excerpt[key]
            if row not in matched:
                matched.append(row)
    return matched


def validate_quote_answer(payload: dict[str, Any], grounding: dict[str, Any], *,
                          allow_multiple: bool = False) -> dict[str, Any]:
    """모델 선택값을 검증하고 렌더에 사용할 원문과 원문 메타데이터만 반환한다."""
    errors: list[str] = []
    result: dict[str, Any] = {"valid": False, "errors": errors, "selected_issue_ids": [], "sections": []}
    if not isinstance(payload, dict):
        errors.append("invalid_payload")
        return result
    issues = _issues(grounding)
    selected = payload.get("selected_issue_ids")
    if (not isinstance(selected, list) or not selected
            or any(type(issue_id) is not int or issue_id not in issues for issue_id in selected)):
        errors.append("invalid_selected_issue_ids")
        return result
    if len(set(selected)) != len(selected) or (not allow_multiple and len(selected) != 1):
        errors.append("invalid_issue_count")
        return result
    result["selected_issue_ids"] = list(selected)
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        errors.append("invalid_sections")
        return result
    seen_topics: set[str] = set()
    for section in sections:
        if not isinstance(section, dict):
            errors.append("invalid_section")
            continue
        topic = section.get("topic")
        if not isinstance(topic, str) or topic not in TOPIC_TITLES or topic in seen_topics:
            errors.append("invalid_topic")
            continue
        seen_topics.add(topic)
        unanswered, evidence = section.get("unanswered"), section.get("evidence")
        if not isinstance(unanswered, bool) or not isinstance(evidence, list) or (not unanswered and not evidence):
            errors.append("invalid_section_evidence")
            continue
        checked: list[dict[str, Any]] = []
        for item in evidence:
            if (not isinstance(item, dict) or type(item.get("issue_id")) is not int
                    or item["issue_id"] not in selected or not isinstance(item.get("field"), str)
                    or not isinstance(item.get("quote"), str) or not item["quote"].strip()):
                errors.append("invalid_evidence")
                continue
            matches = _match_evidence(item, issues[item["issue_id"]])
            if not matches:
                errors.append("quote_not_found_or_context_too_long")
            for match in matches:
                if match not in checked:
                    checked.append(match)
        result["sections"].append({"topic": topic, "evidence": checked, "unanswered": unanswered})
    result['answer_items'] = []
    items = payload.get('answer_items', [])
    if not isinstance(items, list) or len(items) > 5:
        errors.append('invalid_question_answers')
    else:
        for item in items:
            if (not isinstance(item, dict) or not isinstance(item.get('question_part'), str)
                    or not 0 < len(item['question_part']) <= 120):
                errors.append('invalid_question_part')
                continue
            proof = validate_quote_answer({'selected_issue_ids': selected, 'sections': [{
                'topic': 'technical', 'unanswered': item.get('unanswered'), 'evidence': item.get('evidence'),
            }]}, grounding, allow_multiple=allow_multiple)
            if not proof['valid']:
                errors.append('invalid_question_answer_evidence')
            else:
                result['answer_items'].append({'question_part': item['question_part'],
                    'unanswered': item['unanswered'], 'evidence': proof['sections'][0]['evidence'],
                    **({'draft_answer': item['answer']} if 'answer' in item else {})})
    result["valid"] = not errors
    return result

from backend.evidence.excerpts import (
    _ACCESS_ASSIGNMENT as _ACCESS_ASSIGNMENT,
    _NOTE_BOILERPLATE as _NOTE_BOILERPLATE,
    _NOTE_ISSUE_HEADER as _NOTE_ISSUE_HEADER,
    _SHARED_ISSUES as _SHARED_ISSUES,
    _SHARED_CONTINUATION as _SHARED_CONTINUATION,
    _PRIOR_ISSUE_REFERENCE as _PRIOR_ISSUE_REFERENCE,
    _MAX_CORE_CHARS as _MAX_CORE_CHARS,
    _numbered_issue_heading as _numbered_issue_heading,
    _shared_package_cautions as _shared_package_cautions,
    safe_excerpts as safe_excerpts,
)
from backend.evidence.stages import (
    _QC_DONE as _QC_DONE,
    _confirmed_qc_done as _confirmed_qc_done,
    _confirmation_statement as _confirmation_statement,
)
from backend.evidence.references import (
    _label as _label,
)
from backend.evidence.quote_context import (
    _issues as _issues,
    _paragraphs as _paragraphs,
    _MAX_CONTEXT_CHARS as _MAX_CONTEXT_CHARS,
    TOPIC_TITLES as TOPIC_TITLES,
    UNANSWERED_TEXT as UNANSWERED_TEXT,
)
from backend.reports.quote_selection import (
    _core_choice as _core_choice,
    _NOTE_STATE as _NOTE_STATE,
)
from backend.reports.quote_conclusions import (
    _conclusion_lines as _conclusion_lines,
)
from backend.reports.quote_rendering import (
    _DELIVERED as _DELIVERED,
    _OBSERVED_FAILURE as _OBSERVED_FAILURE,
    _SPECULATIVE as _SPECULATIVE,
    _milestone_choice as _milestone_choice,
    _whole_note_choice as _whole_note_choice,
    _append_evidence as _append_evidence,
    render_quote_answer as render_quote_answer,
)
