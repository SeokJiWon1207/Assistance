"""고객사·플랫폼·담당자·상태·식별자를 추출한다."""

import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List

from backend.common import (
    derive_customer_aliases_from_project,
    is_valid_handler_name,
    is_valid_person_name,
    normalize_for_match,
    strip_korean_particle,
    text_contains_person_name,
    text_matches_customer_aliases,
)
from backend.config import (
    CHROMA_DB_PATH,
    CUSTOMER_ALIASES,
    NON_CLOSED_STATUS_PATTERNS,
    PERSON_ROLE_ALIASES,
    STATUS_ALIASES,
    STOPWORDS,
)
from backend.intent.constants import (
    CUSTOMER_LIKE_PATTERN,
    GENERIC_CUSTOMER_WORDS,
    SIMPLE_GREETING_TERMS,
)
from backend.intent.dates import remove_date_field_phrases
from backend.intent.people import (
    ROLE_FOLLOWING_NOUNS,
    build_people,
    plausible_explicit_person_name,
    roster_person_origin,
)
from backend.query_filters import add_filter_clause


def extract_mantis_keyword(text: str) -> str:
    cleaned = re.sub(r"[#,:;!?()\[\]{}\"']", " ", text)
    cleaned = re.sub(r"(?:개발\s*)?문의\s*건?\s*포함(?:해서|하여|해)?", " ", cleaned)
    cleaned = re.sub(r"\d+\s*(개|건)\s*만?", " ", cleaned)
    cleaned = re.sub(r"\d+\s*(개|건)", " ", cleaned)
    cleaned = re.sub(r"최신\s*\d+\s*건\s*기준(?:으로)?", " ", cleaned)
    tokens = []
    for token in cleaned.split():
        normalized = token.strip()
        if not normalized:
            continue
        if normalized.lower() in STOPWORDS or normalized in STOPWORDS:
            continue
        if normalized.isdigit():
            continue
        tokens.append(normalized)
    return " ".join(tokens[:4]) or text


def _snapshot_cache_key() -> tuple[str, int, int]:
    try:
        stat = CHROMA_DB_PATH.stat()
        return str(CHROMA_DB_PATH.resolve()), stat.st_mtime_ns, stat.st_size
    except OSError:
        return str(CHROMA_DB_PATH.resolve()), 0, 0


@lru_cache(maxsize=8)
def _load_known_people_cached(
    database_path: str,
    _mtime_ns: int,
    _size: int,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    path = Path(database_path)
    if not path.exists():
        return tuple(), tuple()

    try:
        with closing(sqlite3.connect(path)) as connection, connection:
            handlers = set(_distinct_chroma_header_values(connection, "담당자"))
            reporters = set(_distinct_chroma_header_values(connection, "요청자"))
    except sqlite3.Error:
        return tuple(), tuple()

    handlers = {
        name
        for name in handlers
        if name not in {"미상", "담당자 미지정"} and is_valid_handler_name(name)
    }
    reporters = {name for name in reporters if name != "미상" and is_valid_person_name(name)}

    return tuple(sorted(handlers)), tuple(sorted(reporters))


def load_known_people() -> tuple[tuple[str, ...], tuple[str, ...]]:
    return _load_known_people_cached(*_snapshot_cache_key())


def _distinct_chroma_header_values(
    connection: sqlite3.Connection,
    field_name: str,
) -> List[str]:
    """Extract distinct ``- field: value`` headers without loading full docs into Python."""
    marker = f"- {field_name}:"
    rows = connection.execute(
        """
        SELECT DISTINCT trim(
            CASE
                WHEN instr(rest, char(10)) > 0
                THEN substr(rest, 1, instr(rest, char(10)) - 1)
                ELSE rest
            END
        ) AS value
        FROM (
            SELECT ltrim(substr(string_value, instr(string_value, ?) + length(?))) AS rest
            FROM embedding_metadata
            WHERE key = 'chroma:document'
              AND instr(string_value, ?) > 0
        )
        WHERE value <> ''
        """,
        (marker, marker, marker),
    )
    return [str(row[0]).strip() for row in rows if row[0]]


@lru_cache(maxsize=8)
def _load_project_alias_groups_cached(
    database_path: str,
    _mtime_ns: int,
    _size: int,
) -> tuple[tuple[str, ...], ...]:
    path = Path(database_path)
    if not path.exists():
        return tuple()

    try:
        with closing(sqlite3.connect(path)) as connection, connection:
            projects = set(_distinct_chroma_header_values(connection, "프로젝트(고객사)"))
    except sqlite3.Error:
        return tuple()

    return tuple(tuple(derive_customer_aliases_from_project(project)) for project in projects)


def load_project_alias_groups() -> tuple[tuple[str, ...], ...]:
    return _load_project_alias_groups_cached(*_snapshot_cache_key())


@lru_cache(maxsize=8)
def _get_customer_alias_groups_cached(
    database_path: str,
    mtime_ns: int,
    size: int,
) -> tuple[tuple[str, ...], ...]:
    groups = [
        list(dict.fromkeys([key, *aliases]))
        for key, aliases in CUSTOMER_ALIASES.items()
    ]

    for dynamic_aliases in _load_project_alias_groups_cached(database_path, mtime_ns, size):
        normalized_dynamic = {normalize_for_match(alias) for alias in dynamic_aliases}
        merged = False
        for group in groups:
            normalized_group = {normalize_for_match(alias) for alias in group}
            if normalized_dynamic & normalized_group:
                group.extend(alias for alias in dynamic_aliases if alias not in group)
                merged = True
                break
        if not merged:
            groups.append(list(dynamic_aliases))

    return tuple(tuple(dict.fromkeys(group)) for group in groups)


def get_customer_alias_groups() -> List[List[str]]:
    return [list(group) for group in _get_customer_alias_groups_cached(*_snapshot_cache_key())]


def extract_status_constraints(text: str) -> List[str]:
    normalized_text = normalize_for_match(remove_date_field_phrases(text))
    statuses = []
    for status_key, aliases in STATUS_ALIASES.items():
        if any(normalize_for_match(alias) in normalized_text for alias in aliases):
            statuses.append(status_key)
    if any(pattern.search(normalized_text) for pattern in NON_CLOSED_STATUS_PATTERNS):
        statuses = [status for status in statuses if status != "closed"]
        # "종료 안 된 할당 이슈"처럼 더 구체적인 미종료 상태가 함께 있으면
        # 그 상태를 유지하고, 구체 상태가 없을 때만 전체 미종료(open)로 넓힌다.
        if not statuses:
            statuses.append("open")
    return statuses


def extract_issue_ids(text: str) -> List[int]:
    issue_ids = set()
    for pattern in (
        r"(?:이슈\s*)?#\s*(\d{3,})",
        r"(?:이슈\s*ID|id)\s*:?\s*#?\s*(\d{3,})",
        r"^\s*0*(\d{3,7})\s*(?:번\s*)?(?:이슈\s*)?(?:알려\s*줘|알려주세요|보여\s*줘|보여주세요|조회(?:해\s*줘|해주세요)?)\s*[?.!]?\s*$",
        r"\b0*(\d{5,7})\b",
        r"\b(30\d{3}|29\d{3}|28\d{3}|27\d{3}|26\d{3})\b",
    ):
        for match in re.finditer(pattern, text, re.IGNORECASE):
            issue_ids.add(int(match.group(1)))
    return sorted(issue_ids, reverse=True)


def extract_excluded_issue_ids(text: str) -> List[int]:
    """Return issue IDs explicitly followed by an exclusion expression."""
    excluded = set()
    for match in re.finditer(
        r"(?:이슈\s*)?#?\s*(\d{3,})\s*(?:번\s*)?(?:은|는|을|를)?\s*제외",
        text,
        re.IGNORECASE,
    ):
        excluded.add(int(match.group(1)))
    return sorted(excluded, reverse=True)


def extract_screen_numbers(text: str) -> List[str]:
    screen_numbers = set()
    for match in re.finditer(r"\[(\d{3,5})\]", text):
        screen_numbers.add(match.group(1))
    for match in re.finditer(r"(?:화면번호|화면|screen)\s*#?:?\s*(\d{3,5})", text, re.IGNORECASE):
        screen_numbers.add(match.group(1))
    return sorted(screen_numbers)


def infer_person_role(text: str) -> str:
    normalized_text = normalize_for_match(text)
    if any(
        marker in normalized_text
        for marker in (
            "한테할당",
            "에게할당",
            "한테배정",
            "에게배정",
            "할당된",
            "배정된",
        )
    ):
        return "handler"
    for role, aliases in PERSON_ROLE_ALIASES.items():
        if any(normalize_for_match(alias) in normalized_text for alias in aliases):
            return role
    return "any"


def expression_is_excluded(text: str, value: str) -> bool:
    """값 주변의 일반적인 한국어 제외 표현을 판별한다.

    필드마다 ``MTS는 빼고`` 같은 예외 정규식을 복제하지 않도록 고객사,
    플랫폼, 상태, 키워드가 함께 사용하는 최소 공통 판별기다.
    """
    normalized_text = normalize_for_match(text)
    normalized_value = normalize_for_match(value)
    if not normalized_value:
        return False
    particle = r"(?:은|는|을|를|이|가)?"
    bridge = r"(?:된|인)?(?:것|건|것도|건도|것은|건은)?"
    negative = r"(?:제외|빼고|빼줘|빼주세요|말고|아닌|아니고|없이|제외한|제외하고|미포함)"
    return bool(
        re.search(re.escape(normalized_value) + particle + bridge + negative, normalized_text)
        or re.search(r"(?:제외|미포함)" + particle + re.escape(normalized_value), normalized_text)
    )


def extract_explicit_keyword_filter_clauses(text: str) -> List[Dict[str, Any]]:
    """제목/본문/키워드 문맥에서 명시한 포함·제외 단어만 구조화한다."""
    if not re.search(r"제목|본문|내용|키워드", text, re.IGNORECASE):
        return []
    clauses: List[Dict[str, Any]] = []
    token = r"([가-힣A-Za-z0-9_.-]{2,40}?)"
    for match in re.finditer(token + r"(?:은|는|이|가)?\s*(?:없(?:는|고)?|제외|빼고|미포함)", text):
        add_filter_clause(clauses, "keyword", "not_contains", match.group(1))
    for match in re.finditer(token + r"(?:은|는|이|가)?\s*(?:있(?:는|고)?|포함)", text):
        if not expression_is_excluded(text, match.group(1)):
            add_filter_clause(clauses, "keyword", "contains", match.group(1))
    return clauses


# "나한테 배정된"에서 "나한테"가 이름으로 잡히면 존재하지 않는 담당자로
# 검색해 결과가 0건이 된다. 화자를 가리키는 말은 이름 후보에서 뺀다.
# (로그인 사용자로 치환하는 일은 상위 계층이 담당한다.)
_FIRST_PERSON_TOKENS = frozenset({
    "나", "내", "저", "제", "본인",
    "나한테", "내게", "나에게", "저한테", "저에게", "본인에게",
    "내가", "제가", "나의", "저의",
})

_EXPLICIT_HANDLER_PATTERN = re.compile(
    r"(?:담당자(?:가|는|를|도)?\s*[:=]?\s*|담당\s*[:=]\s*)"
    r"([가-힣]{2,4}|[A-Za-z][A-Za-z0-9_.-]{1,31})",
    re.IGNORECASE,
)


def extract_people_constraints(text: str) -> Dict[str, Any]:
    # ``담당자별로`` and similar phrases specify result presentation, not a
    # person named "별로" nor the preceding object (for example ``항목을``).
    # Remove those phrases before applying person-name patterns.
    person_text = re.sub(
        r"(?:담당자|담당)\s*(?:별(?:로)?|기준(?:으)?로|마다)",
        " ",
        text,
    )
    handlers, reporters = load_known_people()
    known_people = sorted(set(handlers) | set(reporters), key=len, reverse=True)
    pairs: List[tuple[str, str]] = []
    for person in known_people:
        if person and is_valid_handler_name(person) and text_contains_person_name(person_text, person):
            pairs.append((person, roster_person_origin(person)))

    explicit_handler = _EXPLICIT_HANDLER_PATTERN.search(person_text)
    if explicit_handler:
        name = explicit_handler.group(1).strip()
        korean_candidate = bool(re.fullmatch(r"[가-힣]{2,4}", name))
        if (
            is_valid_handler_name(name)
            and name not in ROLE_FOLLOWING_NOUNS
            and (not korean_candidate or plausible_explicit_person_name(name))
        ):
            known_origin = next(
                (origin for known_name, origin in pairs if normalize_for_match(known_name) == normalize_for_match(name)),
                None,
            )
            pairs.append((name, "known" if known_origin == "known" else "explicit_unverified"))

    if not build_people(pairs, infer_person_role(person_text))["names"]:
        for match in re.finditer(
            r"([가-힣]{2,4})\s*(?:선임|차장|과장|대리|주임|수석|매니저|팀장|부장|사원|한테|에게|담당|할당|할당된|배정|배정된)",
            person_text,
        ):
            name = match.group(1).strip()
            stem = strip_korean_particle(name)
            if name in _FIRST_PERSON_TOKENS or stem in _FIRST_PERSON_TOKENS:
                continue
            if not plausible_explicit_person_name(name):
                continue
            if not is_valid_person_name(name) or not is_valid_person_name(stem):
                continue
            pairs.append((name, "inferred"))

    return build_people(pairs, infer_person_role(person_text))


def is_simple_greeting(text: str) -> bool:
    normalized = normalize_for_match(re.sub(r"[!?.,~]+", "", str(text or "").strip()))
    return normalized in {normalize_for_match(term) for term in SIMPLE_GREETING_TERMS}


def extract_unknown_customers(text: str) -> List[str]:
    """질문이 가리키는 고객사 중 별칭 목록에 없는 이름을 찾는다.

    미등록 고객사를 조용히 버리면 조건 없는 검색이 되어 전혀 다른 고객사
    이슈가 답으로 나간다. 이름을 따로 남겨 "찾지 못했다"고 답하게 한다."""
    unknown: List[str] = []
    for match in CUSTOMER_LIKE_PATTERN.finditer(str(text or "")):
        name = match.group(0).strip()
        if normalize_for_match(name) in {
            normalize_for_match(word) for word in GENERIC_CUSTOMER_WORDS
        }:
            continue
        if any(
            text_matches_customer_aliases(name, aliases)
            for aliases in get_customer_alias_groups()
        ):
            continue
        if name not in unknown:
            unknown.append(name)
    return unknown


__all__ = [
    "extract_mantis_keyword",
    "load_known_people",
    "load_project_alias_groups",
    "get_customer_alias_groups",
    "extract_status_constraints",
    "extract_issue_ids",
    "extract_excluded_issue_ids",
    "extract_screen_numbers",
    "infer_person_role",
    "expression_is_excluded",
    "extract_explicit_keyword_filter_clauses",
    "extract_people_constraints",
    "is_simple_greeting",
    "extract_unknown_customers",
]
