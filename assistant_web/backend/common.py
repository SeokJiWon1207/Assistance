"""순수 헬퍼 함수 모음(Layer 1).

외부 I/O 없이 문자열·숫자·날짜를 다루거나, config의 상수 테이블만 참조하는
함수들이다. backend.config 외의 backend 모듈은 참조하지 않는다(순환 방지).
"""

import re
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from backend.config import (
    AMBIGUOUS_DYNAMIC_CUSTOMER_ALIASES,
    CUSTOMER_ALIASES,
    KOREAN_COUNT_WORDS,
    PERSON_BLOCKLIST,
    PERSON_ROLE_ALIASES,
    PLATFORM_ALIASES,
    STATUS_ALIASES,
    STOPWORDS,
)


def parse_date(value: str) -> Optional[date]:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def clamp_float(value: Optional[float], minimum: float, maximum: float) -> Optional[float]:
    if value is None:
        return None
    return max(minimum, min(float(value), maximum))


def clamp_int(value: Optional[int | str], minimum: int, maximum: int) -> Optional[int]:
    if value is None:
        return None
    return max(minimum, min(int(value), maximum))


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
        or re.fullmatch(r"\d{1,2}일", normalized)
        or re.fullmatch(r"\d+건기준", normalized)
    )


def text_matches_any_alias(text: str, aliases: List[str]) -> bool:
    normalized_text = normalize_for_match(text)
    return any(normalize_for_match(alias) in normalized_text for alias in aliases)


def text_matches_customer_aliases(text: str, aliases: List[str]) -> bool:
    """Match customer aliases without short ASCII aliases leaking into words."""
    normalized_text = normalize_for_match(text)
    boundary_text = str(text or "").casefold()
    for alias in aliases:
        normalized_alias = normalize_for_match(alias)
        if not normalized_alias:
            continue
        if re.fullmatch(r"[a-z0-9]+", normalized_alias) and len(normalized_alias) <= 3:
            if re.search(
                rf"(?<![a-z]){re.escape(normalized_alias)}(?![a-z])",
                boundary_text,
            ):
                return True
            continue
        if normalized_alias in normalized_text:
            return True
    return False


def add_customer_alias(aliases: set[str], value: str) -> None:
    normalized = value.strip()
    if not normalized:
        return
    aliases.add(normalized)
    for part in re.split(r"[,/·]", normalized) if "(" not in normalized and ")" not in normalized else []:
        part = part.strip()
        if part and part != normalized:
            add_customer_alias(aliases, part)
    without_project_suffix = re.sub(r"_Project$", "", normalized)
    aliases.add(without_project_suffix)
    without_number = re.sub(r"^\d+", "", without_project_suffix)
    if without_number:
        aliases.add(without_number)

    parenthetical_parts = re.findall(r"\(([^)]+)\)", without_project_suffix)
    for part in parenthetical_parts:
        add_customer_alias(aliases, part)

    outside_parentheses = re.sub(r"\([^)]*\)", "", without_project_suffix).strip()
    if outside_parentheses:
        aliases.add(outside_parentheses)
        aliases.add(re.sub(r"^\d+", "", outside_parentheses))

    for suffix in ("투자증권", "증권", "선물", "금융투자"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 1:
            aliases.add(normalized[: -len(suffix)])


def derive_customer_aliases_from_project(project_name: str) -> List[str]:
    aliases: set[str] = set()
    add_customer_alias(aliases, project_name)
    blocked = {normalize_for_match(alias) for alias in AMBIGUOUS_DYNAMIC_CUSTOMER_ALIASES}
    return sorted(
        alias
        for alias in aliases
        if alias and normalize_for_match(alias) not in blocked
    )


def extract_chroma_field(text: str, field_name: str) -> str:
    match = re.search(rf"{re.escape(field_name)}:\s*([^\n\r]+)", text)
    if match:
        return match.group(1).strip()
    return ""


def extract_chroma_date(text: str) -> str:
    match = re.search(r"등록일:\s*(\d{4}-\d{2}-\d{2}|미상)", text)
    if match:
        return match.group(1)
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s+메일\s+수신", text)
    if match:
        return match.group(1)
    return ""


def format_mantis_custom_date(value: Any) -> str:
    raw_value = str(value or "").strip()
    if not raw_value or raw_value in {"0", "-"}:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw_value):
        return raw_value
    if re.fullmatch(r"\d{8}", raw_value):
        return f"{raw_value[:4]}-{raw_value[4:6]}-{raw_value[6:8]}"
    if re.fullmatch(r"\d{10,13}", raw_value):
        timestamp = int(raw_value)
        if timestamp > 10_000_000_000:
            timestamp //= 1000
        try:
            # Mantis date-only custom fields are stored as UTC timestamps whose
            # UTC calendar date is the value shown in the Mantis UI. Applying
            # the server's KST offset here shifts the displayed date by one day.
            return datetime.utcfromtimestamp(timestamp).strftime("%Y-%m-%d")
        except (OSError, OverflowError, ValueError):
            return raw_value
    return raw_value


def extract_issue_id_from_text(text: str) -> int:
    match = re.search(r"이슈 ID:\s*#?(\d+)", text)
    if match:
        return int(match.group(1))
    return 0


def extract_title_from_chroma_text(text: str) -> str:
    match = re.search(r"\[제목\]\s*([\s\S]*?)(\n\n|\[고객사 본문 요청 내용\])", text)
    if match:
        return match.group(1).strip()
    return "제목 없음"


def extract_project_from_chroma_text(text: str) -> str:
    match = re.search(r"프로젝트\(고객사\):\s*(.*)", text)
    if match:
        return match.group(1).strip()
    return "프로젝트 미상"


def extract_from_names(text: str) -> List[str]:
    names = set()
    for match in re.finditer(r"From\.\s*([가-힣A-Za-z]{2,6})", text, re.IGNORECASE):
        name = match.group(1).strip()
        if is_valid_person_name(name):
            names.add(name)
    for match in re.finditer(r"([가-힣]{2,4})\s*(?:선임|차장|과장|대리|주임|수석|매니저|팀장|부장|사원)", text):
        name = match.group(1).strip()
        if is_valid_person_name(name):
            names.add(name)
    return sorted(names)


def is_valid_person_name(name: str) -> bool:
    normalized = name.strip()
    if not re.fullmatch(r"[가-힣]{2,4}|[A-Za-z]{2,20}", normalized):
        return False
    lowered = normalized.lower()
    if normalized in STOPWORDS or lowered in STOPWORDS or normalized in PERSON_BLOCKLIST:
        return False
    for aliases in ([key, *values] for key, values in CUSTOMER_ALIASES.items()):
        if any(normalize_for_match(normalized) == normalize_for_match(alias) for alias in aliases):
            return False
    for aliases in PLATFORM_ALIASES.values():
        if any(normalize_for_match(normalized) == normalize_for_match(alias) for alias in aliases):
            return False
    for aliases in STATUS_ALIASES.values():
        if any(normalize_for_match(normalized) == normalize_for_match(alias) for alias in aliases):
            return False
    for aliases in PERSON_ROLE_ALIASES.values():
        if any(normalize_for_match(normalized) == normalize_for_match(alias) for alias in aliases):
            return False
    return True


def is_valid_handler_name(name: str) -> bool:
    normalized = name.strip()
    if not re.fullmatch(r"[가-힣]{2,4}|[A-Za-z][A-Za-z0-9_.-]{1,31}", normalized):
        return False
    return normalized not in STOPWORDS and normalized not in PERSON_BLOCKLIST


MANTIS_STATUS_DISPLAY_LABELS = {
    "new": "신규 이슈",
    "feedback": "정보 부족",
    "acknowledged": "이슈 검토",
    "confirmed": "확인된 이슈",
    "assigned": "할당된 이슈",
    "developed": "개발완료",
    "resolved": "해결된 이슈",
    "closed": "종료된 이슈",
    # 검색 결과에서만 사용하는 묶음 상태다.
    "open": "미종료",
}


def display_mantis_status_label(status: Any) -> str:
    """Mantis 내부 상태 코드를 사용자용 한국어 라벨로 변환한다."""
    normalized = str(status or "").strip()
    return MANTIS_STATUS_DISPLAY_LABELS.get(
        normalized.casefold(), normalized or "상태 미상"
    )


# known_people(과거 이슈에서 추출한 실제 담당자/보고자 이름) 캐시엔 "파트너매니저"
# 같은 화면/시스템 이름이 "파트너"+"매니저(직함)"로 잘못 쪼개져 섞여 들어갈 수 있다.
# 단순 substring 포함 매칭(normalize_for_match 방식)을 쓰면 "파트너사업팀"처럼
# 이름이 더 큰 복합어의 일부로 들어있을 때도 인물로 오인한다. 뒤에 한글 조사나
# 비한글 문자/문자열 끝이 올 때만 "독립된 단어"로 인정해 이런 오탐을 막는다.
KOREAN_PARTICLE_PATTERN = "가|이|는|은|을|를|와|과|도|만|의|에게|한테|랑|이랑|께|에서|으로|로"


def strip_korean_particle(name: str) -> str:
    """조사가 붙어 있으면 떼어낸 어간을, 없으면 원래 값을 돌려준다.

    "이슈의 담당자", "제목과 담당자로"처럼 일반 명사에 조사가 붙은 말이
    이름 후보로 잡혔는지 판별할 때 쓴다.
    """
    normalized = str(name or "").strip()
    match = re.fullmatch(rf"([가-힣]{{2,}})(?:{KOREAN_PARTICLE_PATTERN})", normalized)
    return match.group(1) if match else normalized


def text_contains_person_name(text: str, name: str) -> bool:
    pattern = re.compile(
        rf"{re.escape(name)}(?:{KOREAN_PARTICLE_PATTERN})?(?=[^가-힣]|$)",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))


def normalize_chroma_issue_document(text: str) -> Optional[Dict[str, Any]]:
    issue_id = extract_issue_id_from_text(text)
    if not issue_id:
        return None
    created_at = extract_chroma_date(text)
    return {
        "id": issue_id,
        "project": extract_project_from_chroma_text(text),
        "summary": extract_title_from_chroma_text(text),
        "status": extract_chroma_field(text, "현재 상태") or "상태 미상",
        "media_type": extract_chroma_field(text, "매체구분") or "기타/미상",
        "screen_number": extract_chroma_field(text, "화면번호") or "미상",
        "work_subject": extract_chroma_field(text, "작업주체") or "미상",
        "effort_fields": {},
        "date_fields": {},
        "reporter": "미상",
        "handler": extract_chroma_field(text, "담당자") or "담당자 미지정",
        "created_at": created_at,
        "updated_at": "",
        "description": text[:800],
    }
