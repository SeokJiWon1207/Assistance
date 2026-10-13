"""본문과 전체 노트에서 검색어가 실제로 나온 구절만 추출한다."""

import re
from typing import Any

from backend.intent.search_terms import keyword_term_aliases


def constraint_evidence_terms(constraints: dict[str, Any]) -> list[str]:
    terms = list(constraints.get("inherited_terms") or [])
    for clause in constraints.get("filter_clauses") or []:
        if clause.get("field") in {"keyword", "title"} and clause.get("operator") in {"equals", "in", "contains"}:
            value = clause.get("value")
            terms.extend(str(item) for item in (value if isinstance(value, list) else [value]) if item)
    return list(dict.fromkeys(terms))


def matching_issue_evidence(issue: dict[str, Any], terms: list[str]) -> list[dict[str, Any]]:
    if not terms:
        return []
    fields: list[tuple[str, dict[str, Any], str]] = [(field, {}, str(issue.get(field) or "")) for field in
              ("summary", "description", "additional_information", "steps_to_reproduce")]
    fields.extend(("note", {"note_id": note.get("id"), "created_at": note.get("created_at")},
                   str(note.get("text") or "")) for note in issue.get("notes") or [])
    evidence = []
    seen = set()
    for term in dict.fromkeys(terms):
        # 원문 위치를 보존하면서 VBScript / VB Scripts 같은 공백 차이를 허용한다.
        compact = re.sub(r"\s+", "", term)
        if not compact:
            continue
        variants = [re.sub(r"\s+", "", alias) for alias in keyword_term_aliases(term)]
        pattern = re.compile("|".join(r"\s*".join(re.escape(char) for char in alias)
                                      for alias in variants if alias), re.IGNORECASE)
        for field, metadata, text in fields:
            match = pattern.search(text)
            if not match:
                continue
            key = (field, metadata.get("note_id"), match.start())
            if key in seen:
                continue
            seen.add(key)
            start, end = max(0, match.start() - 140), min(len(text), match.end() + 320)
            excerpt = ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")
            evidence.append({"field": field, **metadata, "text": excerpt})
            if len(evidence) >= 6:
                return evidence
    return evidence
