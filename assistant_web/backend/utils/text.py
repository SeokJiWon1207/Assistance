"""입출력 없이 문자열과 검색 제어 표현을 처리한다."""
import re
from typing import List, Optional
from backend.settings.values import KOREAN_COUNT_WORDS, STOPWORDS


def compact_text(value: str, max_chars: int = 520) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def quoted_value_span(text: str) -> Optional[tuple[int, int]]:
    """Return the outermost quoted span that starts earliest in ``text``.

    Straight quotes use the last matching delimiter so titles that themselves
    contain apostrophes (for example ``'[1892] '일중' 신규조건'``) keep the
    complete outer title instead of being truncated at the inner quote.
    """
    candidates: List[tuple[int, int]] = []
    for opening, closing in (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'")):
        start = text.find(opening)
        if start < 0:
            continue
        end = text.rfind(closing)
        if end <= start:
            continue
        candidates.append((start, end + len(closing)))
    return min(candidates, key=lambda span: span[0]) if candidates else None


def extract_quoted_value(text: str) -> str:
    span = quoted_value_span(text)
    if not span:
        return ""
    start, end = span
    return text[start + 1 : end - 1].strip()


def remove_quoted_value(text: str) -> str:
    """Remove the outer quoted value while preserving surrounding intent text."""
    span = quoted_value_span(text)
    if not span:
        return text
    start, end = span
    return f"{text[:start]} {text[end:]}"


def normalize_for_match(value: str) -> str:
    return re.sub(r"\s+", "", value).casefold()


def normalize_search_token(token: str) -> str:
    normalized = token.strip()
    # 아래 글자가 조사처럼 보여도 단어 자체의 일부인 업무 용어다.
    if normalize_for_match(normalized) in {"로그인", "디자인", "온라인", "승인", "원인"}:
        return normalized
    for suffix in (
        "으로부터",
        "로부터",
        "한테서",
        "에게서",
        "한테",
        "에게",
        "에서",
        "으로",
        "로",
        "에는",
        "에",
        "은",
        "는",
        "인",
        "을",
        "를",
        "이",
        "가",
        "의",
        "들",
        "와",
        "과",
    ):
        if len(normalized) > len(suffix) + 1 and normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


# "1개만", "하나만", "세건"처럼 표시 개수에 붙은 토큰은 검색어가 아니다. 이
# 값이 라이브 페이지 스캔에 AND 키워드로 들어가면 조건에 맞는 최신 이슈가
# 있어도 0건이 되고, 본문에 그 낱말이 우연히 들어간 이슈만 근거로 남는다.
# "한 건만"처럼 수사와 단위가 띄어 쓰이면 토큰이 갈라지므로 수사와 단위를
# 각각 단독으로도 인정한다. 둘 다 없으면 빈 문자열이라 다른 낱말은 걸리지 않는다.
COUNT_EXPRESSION_PATTERN = re.compile(
    r"(?:\d+|"
    + "|".join(sorted(KOREAN_COUNT_WORDS, key=len, reverse=True))
    + r")?(?:개|건|명|페이지|가지)?(?:만|씩)?"
)


# "3개월", "2주일", "10일간"처럼 기간을 통째로 가리키는 토큰도 검색어가 아니다.
# 연·월·일 단독 표기는 아래 개별 패턴이 이미 맡고 있다.
PERIOD_EXPRESSION_PATTERN = re.compile(r"\d+(?:개월|달|주일|주|년|일)간?")


LOOKUP_REQUEST_PATTERN = re.compile(
    r"(?:보여|알려|찾아|뽑아|조회해|검색해)"
    r"(?:줘(?:요)?|주세요|줄래(?:요)?|주실래(?:요)?|줄\s*수\s*있(?:어|나요|을까)|주실\s*수\s*있나요)"
)


def has_lookup_request(text: str) -> bool:
    return bool(LOOKUP_REQUEST_PATTERN.search(normalize_for_match(text)))


def is_control_search_term(term: str) -> bool:
    normalized = normalize_for_match(term)
    if not normalized:
        return True
    if normalized in {normalize_for_match(word) for word in STOPWORDS}:
        return True
    return bool(
        COUNT_EXPRESSION_PATTERN.fullmatch(normalized)
        or LOOKUP_REQUEST_PATTERN.fullmatch(normalized)
        or normalized == "좀"
        or PERIOD_EXPRESSION_PATTERN.fullmatch(normalized)
        or re.fullmatch(r"\d{4}년?", normalized)
        or re.fullmatch(r"\d{1,2}월", normalized)
        or re.fullmatch(r"\d{1,2}일(?:부터|까지)?", normalized)
        or re.fullmatch(r"\d+건기준", normalized)
    )


def text_matches_any_alias(text: str, aliases: List[str]) -> bool:
    normalized_text = normalize_for_match(text)
    return any(normalize_for_match(alias) in normalized_text for alias in aliases)
