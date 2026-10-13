"""명시적인 검색 범위 변경·해제와 대화에서 생략한 연도를 해석한다."""

import re


SCOPE_FIELDS = {
    "customer": r"고객사|프로젝트", "owner": r"담당자|담당",
    "platform": r"플랫폼|매체구분|매체", "created_at": r"기간|등록일",
    "category": r"분류|카테고리",
}
SCOPE_CONTROL_TERM = re.compile(
    r"(?:고객사|프로젝트|담당자?|플랫폼|매체(?:구분)?|분류|카테고리|기간|등록일|조건|필터|제한|범위|나머지)"
    r"(?:은|는|이|가|을|를|도|만|와|과)?"
)


def _unquoted(text: str) -> str:
    return re.sub(r'''["'“‘][^"'”’]*["'”’]''', " ", text)


SCOPE_CLEAR_PATTERNS = {
    field: re.compile(
        rf"(?:{labels})(?:은|는|을|를|도)?\s*"
        r"(?:조건|필터|제한|범위)(?:은|는|을|를|도)?\s*"
        r"(?:빼(?:고|줘|주세요)|(?:제거|해제)(?!\s*하?지)(?:해|하|하고)?|없이|없애(?:고|줘))"
        r"|(?:" + labels + r")(?:은|는)?\s*(?:무관|상관\s*없이)"
    ) for field, labels in SCOPE_FIELDS.items()
}
SCOPE_CLEAR_PATTERNS["category"] = re.compile(
    SCOPE_CLEAR_PATTERNS["category"].pattern
    + r"|(?:분류|카테고리)(?:은|는)?\s*[:=]?\s*전체(?:로|으로)?"
    r"(?!\s*(?:바꾸|변경하|수정하)지)(?=\s|$|[.!?])"
)
SCOPE_KEEP_PATTERN = re.compile(
    r"(?:분류|카테고리)(?:은|는)?\s*(?:(?:조건|필터)(?:은|는)?\s*)?"
    r"(?:전체(?:로|으로)?\s*바꾸|해제하|제거하|변경하|수정하)지\s*말고"
)


def cleared_scope_fields(text: str) -> set[str]:
    text = _unquoted(text)
    return {field for field, pattern in SCOPE_CLEAR_PATTERNS.items() if pattern.search(text)}


def without_scope_clear_phrases(text: str) -> str:
    # 명시적 제목/키워드의 부정 표현은 기존 검증에 남긴다.
    quoted_spans = [(m.start(), m.end()) for m in re.finditer(r'''["'“‘][^"'”’]*["'”’]''', text)]
    spans = [(m.start(), m.end()) for pattern in [*SCOPE_CLEAR_PATTERNS.values(), SCOPE_KEEP_PATTERN] for m in pattern.finditer(text)
             if not any(start <= m.start() < end for start, end in quoted_spans)]
    for start, end in sorted(spans, reverse=True):
        text = text[:start] + " " * (end - start) + text[end:]
    return text


def has_scope_edit(text: str) -> bool:
    text = _unquoted(text)
    if re.fullmatch(
        r"\s*(?:20\d{2}\s*년\s*)?(?:1[0-2]|0?[1-9])\s*월(?:으로|로)?\s*"
        r"(?:바꿔|변경해|수정해)(?:줘|주세요)[.!?]*\s*", text,
    ):
        return True
    if re.search(r"(?<![가-힣A-Za-z0-9_])(?:HTS|MTS|모바일|PC|iOS|Android)(?:으로|로)?\s*(?:바꿔(?!달라|야)|변경해|수정해)", text, re.I):
        return True
    return bool(cleared_scope_fields(text) or re.search(
        r"(?:고객사|프로젝트|담당자?|플랫폼|매체(?:구분)?|분류|카테고리|기간)(?:은|는|을|를|만)?"
        r"[^.!?]{0,60}(?:바꿔(?!달라|야)|변경해|수정해)", text,
    ))


def followup_created_at(previous: dict, question: str) -> dict | None:
    """월만 바꾸면 직전 단일 연도를 유지하고 명시한 새 연도는 우선한다."""
    period = previous.get("created_at") or {}
    year = str(period.get("from") or "")[:4]
    if (not year or year != str(period.get("to") or "")[:4]
            or re.search(r"20\d{2}|올해|금년|작년|전년|지난\s*해|이번\s*해|지난\s*년도|이번\s*년도", question)
            or not re.search(r"(?<!\d)(?:1[0-2]|0?[1-9])\s*월", question)):
        return None
    from backend.query_planning import extract_created_at_range
    qualified = re.sub(r"(?<!\d)((?:1[0-2]|0?[1-9])\s*월)", rf"{year}년 \1", question, count=1)
    return extract_created_at_range(qualified)
