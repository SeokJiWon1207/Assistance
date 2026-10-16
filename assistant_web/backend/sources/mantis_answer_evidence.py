"""최종 설명에 쓸 원문 발췌: 위치·노트 시각·누락 범위를 보존한다."""
from collections import Counter
import re
from typing import Any

from backend.sources.mantis_chunks import search_tokens, split_issue


_DECISION_CHANGE = re.compile(
    r'롤백|취소|(?:반영|배포|적용)[^\n]{0,40}(?:보류|제외)|'
    r'(?:보류|제외)[^\n]{0,40}(?:반영|배포|적용)', re.I)
_MAX_DECISION_ANCHORS = 8


def build_answer_evidence(issue: dict[str, Any], query: str, *, max_chars: int = 18000) -> dict[str, Any]:
    chunks = list(split_issue(issue))
    notes = {f"note:{n.get('id', i)}": n for i, n in enumerate(issue.get("notes") or [])}
    # 겹친 구간은 선택 뒤 병합한다. 원문의 총 길이는 중복 없이 계산한다.
    lengths: dict[str, int] = {}
    for chunk in chunks:
        lengths[chunk["field"]] = chunk["end"]
    tokens = [set(search_tokens(c["text"])) for c in chunks]
    frequency = Counter(token for group in tokens for token in group)
    wanted = set(search_tokens(query))
    scores = [sum((3 if t.startswith("w") else 1) / frequency[t] for t in group & wanted) for group in tokens]
    ordered = sorted(range(len(chunks)), key=lambda i: scores[i], reverse=True)
    # 마지막 결론·취소·반영 보류를 관련도 점수만으로 탈락시키지 않는다.
    latest = sorted(notes, key=lambda f: (str(notes[f].get("created_at") or ""), int(notes[f].get("id") or 0)))[-4:]
    anchors = [next(i for i, c in enumerate(chunks) if c["field"] == f) for f in reversed(latest)
               if any(c["field"] == f for c in chunks)]
    # 과거의 롤백·취소·반영 보류가 질문 낱말과 덜 겹쳐도 완료 판단에서 빠지면 안 된다.
    # 최신 사건부터 노트당 한 구간만 우선하며, 질문 상위 두 구간·마지막 네 노트 뒤에
    # 최대 여덟 구간을 예약한다. 긴 노트 전체를 확장하거나 기존 문자 예산을 늘리지 않는다.
    decision_candidates = [i for i, c in enumerate(chunks)
                           if c['field'] in notes and _DECISION_CHANGE.search(c['text'])]
    decision_candidates.sort(key=lambda i: (
        str(notes[chunks[i]['field']].get('created_at') or ''),
        int(notes[chunks[i]['field']].get('id') or 0), -chunks[i]['start'],
    ), reverse=True)
    decision_anchors: list[int] = []
    decision_fields: set[str] = set()
    for i in decision_candidates:
        if chunks[i]['field'] in decision_fields:
            continue
        decision_anchors.append(i)
        decision_fields.add(chunks[i]['field'])
        if len(decision_anchors) >= _MAX_DECISION_ANCHORS:
            break
    starts = [i for i, c in enumerate(chunks) if c["start"] == 0 and not c["field"].startswith("note:")]
    priority = list(dict.fromkeys([*ordered[:2], *anchors, *decision_anchors, *starts, *ordered]))
    ranges: dict[str, list[tuple[int, int]]] = {}
    used = 0
    for i in priority:
        chunk = chunks[i]
        field, start, end = chunk["field"], chunk["start"], chunk["end"]
        existing = ranges.get(field, [])
        overlap = sum(max(0, min(end, b) - max(start, a)) for a, b in existing)
        extra = end - start - overlap
        if used + extra > max_chars:
            continue
        merged = sorted([*existing, (start, end)])
        joined: list[tuple[int, int]] = []
        for a, b in merged:
            if joined and a <= joined[-1][1]:
                joined[-1] = (joined[-1][0], max(joined[-1][1], b))
            else:
                joined.append((a, b))
        ranges[field] = joined
        used += extra
    excerpts = []
    for field in lengths:
        field_chunks = [c for c in chunks if c["field"] == field]
        # 겹친 구간을 원문 그대로 복원하며 생략 기호를 원문에 섞지 않는다.
        original = field_chunks[0]["text"]
        for chunk in field_chunks[1:]:
            original += chunk["text"][max(0, len(original) - chunk["start"]):]
        for start, end in ranges.get(field, []):
            item = {"field": field, "start": start, "end": end, "text": original[start:end]}
            if field in notes:
                item.update(note_id=notes[field].get("id"), created_at=notes[field].get("created_at", ""),
                            updated_at=notes[field].get("updated_at", ""),
                            reporter=(notes[field].get("reporter") or {}).get("real_name")
                            or (notes[field].get("reporter") or {}).get("name", ""))
            excerpts.append(item)
    return {"complete": used == sum(lengths.values()), "total_chars": sum(lengths.values()),
            "provided_chars": used, "total_notes": len(notes),
            "provided_notes": len({e["note_id"] for e in excerpts if "note_id" in e}), "excerpts": excerpts}
