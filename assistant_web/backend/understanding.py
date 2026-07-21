"""질의 이해 계층 — 제약조건 추출·매칭·검색 의도 판단(Layer 2).

자연어 질문에서 고객사/상태/담당자/날짜/이슈ID/화면번호 등 제약조건을 뽑아내고
(extract_*_constraints), 이슈·Chroma 문서 텍스트가 그 제약에 부합하는지 판정하며
(text_matches_*, chroma_text_matches_*), 검색 실행 여부와 도구 라우팅을 결정한다
(should_run_*, rule_based_search_intent, plan_search_intent_with_model).

config/common/llm/query_planning 등 하위 계층만 참조한다(순환 방지).
app.py는 `from backend.understanding import *`로 이 이름들을 그대로 재노출한다.
"""

import json
import os
import re
import sqlite3
from datetime import date
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

import requests

from backend.common import (
    derive_customer_aliases_from_project,
    extract_chroma_date,
    extract_chroma_field,
    extract_issue_id_from_text,
    extract_project_from_chroma_text,
    extract_title_from_chroma_text,
    format_mantis_custom_date,
    is_control_search_term,
    is_valid_handler_name,
    is_valid_person_name,
    normalize_for_match,
    normalize_search_token,
    strip_korean_particle,
    text_contains_person_name,
    text_matches_any_alias,
    text_matches_customer_aliases,
)
from backend.config import (
    CHROMA_DB_PATH,
    CUSTOMER_ALIASES,
    MANTIS_DATE_FIELD_ALIASES,
    NON_CLOSED_STATUS_PATTERNS,
    PERSON_ROLE_ALIASES,
    PLATFORM_ALIASES,
    STATUS_ALIASES,
    STATUS_MATCH_ALIASES,
    STOPWORDS,
)
from backend.audit import load_audit_sources
from backend.llm import extract_model_content, post_model_chat
from backend.schemas import ChatMessage
from backend.query_planning import (
    extract_created_at_range,
    extract_group_by,
    infer_operation,
    plan_created_at_matches,
)
from backend.query_filters import add_filter_clause, normalize_filter_clauses


# 검색 의도/도메인 라우팅에 쓰는 키워드·마커 테이블(질의 이해 계층 전용).
SIMPLE_GREETING_TERMS = {
    "hi",
    "hello",
    "하이",
    "헬로",
    "안녕",
    "안녕하세요",
    "반가워",
    "반가워요",
}

RETRIEVAL_INTENT_KEYWORDS = {
    "이슈",
    "건",
    "접수",
    "요청",
    "문의",
    "오류",
    "장애",
    "버그",
    "수정",
    "개선",
    "확인",
    "담당",
    "담당자",
    "고객사",
    "화면",
    "화면번호",
    "차트",
    "mantis",
    "맨티스",
    "멘티스",
    "chroma",
    "chromadb",
    "vector",
    "벡터",
    "notion",
    "노션",
    "주요이슈",
    "주요 이슈",
    "결재요청",
    "결재 요청",
    "팀내 공지",
    "공지",
}

NOTION_INTENT_KEYWORDS = {
    "notion",
    "노션",
    "노선",
    "주요이슈",
    "주요 이슈",
    "결재요청",
    "결재 요청",
    "결재완료",
    "등록됨",
    "상태별",
    "타입별",
    "팀내 공지",
    "공지",
    "휴가",
    "회식",
    "송년회",
    "신년회",
    "팀빌딩",
    "일정",
    "기한",
}

# 조회할 데이터 소스를 가리키는 말. 실제 프로젝트 목록에 999Mantis가 있어
# 고객사 별칭으로도 잡히므로, 고객사 후보에서 걸러낼 때 쓴다.
SOURCE_NAME_ALIASES = {
    normalize_for_match(name)
    for name in ("mantis", "맨티스", "맨티스에서", "notion", "노션")
}

NOTION_QUERY_STOPWORDS = {
    "notion",
    "노션",
    "노선",
    "주요이슈",
    "주요 이슈",
    "주요이슈현황",
    "이슈리스트",
    "테스트",
    "상태별",
    "타입별",
    "팀내 공지",
    "공지",
    "항목",
    "문서",
    "자료",
    "기록",
    "페이지",
    "건은",
    "몇개야",
    "몇건이야",
    "된",
    "상태",
    "상태인",
    # 존재/의문 어미가 검색어로 남으면 실제 문서 본문/제목에 거의 등장하지 않아
    # AND 매칭이 항상 실패한다. "메모리 릭 있어?" 같은 자연스러운 질문이
    # 매칭되도록 필러성 어미를 제거한다.
    "있어",
    "있어요",
    "있나요",
    "있나",
    "있는지",
    "없어",
    "없나요",
    "없나",
}

GENERAL_INTENT_KEYWORDS = {
    "모델",
    "누구",
    "너는",
    "넌",
    "사용법",
    "도움말",
    "안녕",
    "뭐할수",
    "뭐 할 수",
    "기능",
}

EXPLICIT_NOTION_MARKERS = ("notion", "노션", "노선")

EXPLICIT_MANTIS_MARKERS = ("mantis", "멘티스", "맨티스")

COMBINED_SOURCE_MARKERS = (
    "둘다",
    "둘 다",
    "두소스",
    "두 소스",
    "양쪽",
    "함께",
    "통합",
    "교차",
    "비교",
)

MANTIS_DOMAIN_MARKERS = (
    "고객사",
    "프로젝트",
    "화면번호",
    "hts",
    "mts",
    "장애",
    "오류",
    "버그",
    "제도개선",
    "거래시간",
    "공수",
    "개발완료",
    "qc완료",
)

NOTION_DOMAIN_MARKERS = (
    "공지",
    "회식",
    "휴가",
    "송년회",
    "신년회",
    "팀빌딩",
    "결재요청",
    "결재완료",
    "주요이슈현황",
    "업무공유",
)


ISSUE_LIST_INTENT_KEYWORDS = {
    "최근",
    "최신",
    "목록",
    "리스트",
    "보여줘",
    "알려줘",
    "뽑아줘",
    "찾아줘",
    "조회",
    "검색",
    "이슈",
    "건",
    "접수",
    "문의",
    "요청",
}

ISSUE_ANALYSIS_INTENT_KEYWORDS = {
    "분석",
    "흐름",
    "추이",
    "경향",
    "패턴",
    "원인",
    "이유",
    "리스크",
    "영향",
    "정리해줘",
    "요약해줘",
    "비교",
    "분류",
}


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


@lru_cache(maxsize=1)
def load_known_people() -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not CHROMA_DB_PATH.exists():
        return tuple(), tuple()

    try:
        with sqlite3.connect(CHROMA_DB_PATH) as connection:
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


@lru_cache(maxsize=1)
def load_project_alias_groups() -> tuple[tuple[str, ...], ...]:
    if not CHROMA_DB_PATH.exists():
        return tuple()

    try:
        with sqlite3.connect(CHROMA_DB_PATH) as connection:
            projects = set(_distinct_chroma_header_values(connection, "프로젝트(고객사)"))
    except sqlite3.Error:
        return tuple()

    return tuple(tuple(derive_customer_aliases_from_project(project)) for project in projects)


@lru_cache(maxsize=1)
def get_customer_alias_groups() -> List[List[str]]:
    groups = [
        list(dict.fromkeys([key, *aliases]))
        for key, aliases in CUSTOMER_ALIASES.items()
    ]

    for dynamic_aliases in load_project_alias_groups():
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

    return [list(dict.fromkeys(group)) for group in groups]


def extract_date_field_constraints(text: str) -> Dict[str, str]:
    constraints: Dict[str, str] = {}
    date_pattern = (
        r"(20\d{2})(?:[./-]|년)\s*"
        r"(1[0-2]|0?[1-9])(?:[./-]|월)\s*"
        r"(3[01]|[12]\d|0?[1-9])일?(?!\d)"
    )
    for field_name, aliases in MANTIS_DATE_FIELD_ALIASES.items():
        for alias in sorted(aliases, key=len, reverse=True):
            match = re.search(
                rf"{re.escape(alias)}\s*(?:이|가|은|는|을|를|:|=)?\s*{date_pattern}",
                text,
                re.IGNORECASE,
            )
            if not match:
                continue
            year, month, day = match.group(1), int(match.group(2)), int(match.group(3))
            constraints[field_name] = f"{year}-{month:02d}-{day:02d}"
            break
    return constraints


def remove_date_field_phrases(text: str) -> str:
    cleaned = text
    for aliases in MANTIS_DATE_FIELD_ALIASES.values():
        for alias in sorted(aliases, key=len, reverse=True):
            cleaned = re.sub(re.escape(alias), " ", cleaned, flags=re.IGNORECASE)
    return cleaned


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
    people = [
        person
        for person in known_people
        if person and is_valid_handler_name(person) and text_contains_person_name(person_text, person)
    ]

    if not people:
        explicit_handler = re.search(
            r"(?:담당자|담당)\s*[:=]?\s*([A-Za-z][A-Za-z0-9_.-]{1,31})",
            person_text,
            re.IGNORECASE,
        )
        if explicit_handler and is_valid_handler_name(explicit_handler.group(1)):
            people.append(explicit_handler.group(1))

    if not people:
        for match in re.finditer(r"([가-힣]{2,4})\s*(?:선임|차장|과장|대리|주임|수석|매니저|팀장|부장|사원|한테|에게|담당|할당|할당된|배정|배정된)", person_text):
            name = match.group(1).strip()
            # "그 이슈의 담당자", "제목과 담당자로 정리해줘"처럼 조사가 붙은 일반
            # 명사가 이름으로 잡히지 않도록 조사를 뗀 어간까지 함께 검증한다.
            if not is_valid_person_name(name):
                continue
            if not is_valid_person_name(strip_korean_particle(name)):
                continue
            people.append(name)

    people = list(dict.fromkeys(people))
    return {
        "names": people,
        "role": infer_person_role(person_text) if people else None,
    }


def is_simple_greeting(text: str) -> bool:
    normalized = normalize_for_match(re.sub(r"[!?.,~]+", "", str(text or "").strip()))
    return normalized in {normalize_for_match(term) for term in SIMPLE_GREETING_TERMS}


def extract_query_constraints(text: str) -> Dict[str, Any]:
    # "하이"는 하이투자증권의 현업 약칭이기도 하지만, 단독 입력은 인사로 취급한다.
    # "하이 이슈 보여줘"처럼 검색 의도가 붙은 문장은 기존 고객사 별칭 검색을 유지한다.
    if is_simple_greeting(text):
        return {
            "customer_key": None,
            "customer_aliases": [],
            "platform_key": None,
            "platform_aliases": [],
            "people": {"names": [], "role": None},
            "statuses": [],
            "issue_ids": [],
            "screen_numbers": [],
            "date_fields": {},
            "created_at": None,
        }
    normalized_text = normalize_for_match(text)
    customer_key = None
    customer_aliases: List[str] = []
    customer_exact = None
    customer_matches: List[tuple[int, str, List[str]]] = []
    excluded_customer_matches: List[tuple[int, str, List[str]]] = []
    for aliases in get_customer_alias_groups():
        for alias in aliases:
            normalized_alias = normalize_for_match(alias)
            if normalized_alias and text_matches_customer_aliases(text, [alias]):
                target = excluded_customer_matches if expression_is_excluded(text, alias) else customer_matches
                target.append((len(normalized_alias), alias, aliases))

    # "Mantis와 Notion에서 찾아줘"의 Mantis/Notion은 조회할 데이터 소스 이름이지
    # 고객사가 아니다. 다만 실제 프로젝트 목록에 999Mantis가 있어 고객사 별칭으로
    # 잡히고, 별칭이 길어 진짜 고객사(KB증권 등)까지 밀어내 버린다.
    # 프로젝트를 명시적으로 가리킬 때만 고객사 후보로 남긴다.
    if not any(
        normalize_for_match(marker) in normalized_text
        for marker in ("999Mantis", "고객사", "프로젝트")
    ):
        customer_matches = [
            item
            for item in customer_matches
            if normalize_for_match(item[1]) not in SOURCE_NAME_ALIASES
        ]

    if customer_matches:
        _, matched_alias, matched_group = max(customer_matches, key=lambda item: item[0])
        customer_key = matched_alias
        customer_aliases = matched_group
        if re.match(r"^\d+", matched_alias) and "(" in matched_alias and ")" in matched_alias:
            customer_exact = matched_alias

    # "고객사별 ... 하나씩"에서 "하나"는 고객사명이 아니라 그룹별 개수를
    # 나타내는 말이다. 고객사별/프로젝트별 요청은 단일 고객사 필터로 좁히지 않는다.
    if extract_group_by(text) == "customer":
        customer_key = None
        customer_aliases = []
        customer_exact = None

    # KRX is both a legacy project/customer alias and a market-domain keyword.
    # In questions such as "KRX 제도개선" it describes the subject, not 119KRX.
    # Keep the customer constraint only when the user explicitly points to a
    # project/customer or uses a project-specific alias.
    if customer_aliases and any(
        normalize_for_match(alias) == "krx" for alias in customer_aliases
    ):
        krx_domain_query = any(
            normalize_for_match(marker) in normalized_text
            for marker in ("제도개선", "거래시간", "시장제도", "한국거래소", "장시간")
        )
        explicit_customer_context = any(
            normalize_for_match(marker) in normalized_text
            for marker in ("고객사", "프로젝트", "119krx", "라우스", "캄보디아")
        )
        if krx_domain_query and not explicit_customer_context:
            customer_key = None
            customer_aliases = []
            customer_exact = None

    platform_key = None
    platform_aliases: List[str] = []
    excluded_platform_keys: List[str] = []
    excluded_platform_aliases: List[str] = []
    for key, aliases in PLATFORM_ALIASES.items():
        if any(normalize_for_match(alias) in normalized_text for alias in [key, *aliases]):
            matched_alias = next(
                alias
                for alias in [key, *aliases]
                if normalize_for_match(alias) in normalized_text
            )
            if expression_is_excluded(text, matched_alias):
                excluded_platform_keys.append(key)
                excluded_platform_aliases.extend(aliases)
            elif not platform_key:
                platform_key = key
                platform_aliases = aliases

    people = extract_people_constraints(remove_date_field_phrases(text))
    # "담당자가 없는 건만", "담당자 미지정"처럼 담당자가 비어 있는 이슈만 찾는 요청.
    unassigned_people = bool(
        re.search(
            r"담당자?\s*(?:가|는|를|도)?\s*"
            r"(?:없는|없이|없음|미지정|미배정|안\s*정해진|지정\s*안\s*된|할당\s*안\s*된|배정\s*안\s*된)",
            text,
        )
    )
    if people.get("names") and customer_aliases:
        explicit_handler_context = bool(re.search(r"(?:담당자|담당)\s*[:=]?\s*", text, re.IGNORECASE))
        if explicit_handler_context and people.get("role") == "handler":
            customer_key = None
            customer_aliases = []
            customer_exact = None
        else:
            customer_alias_norms = {normalize_for_match(alias) for alias in customer_aliases}
            people["names"] = [
                name
                for name in people["names"]
                if normalize_for_match(name) not in customer_alias_norms
            ]
            if not people["names"]:
                people["role"] = None

    statuses = extract_status_constraints(text)
    excluded_statuses: List[str] = []
    for status_key, aliases in STATUS_ALIASES.items():
        if any(
            normalize_for_match(alias) in normalized_text and expression_is_excluded(text, alias)
            for alias in [status_key, *aliases]
        ):
            excluded_statuses.append(status_key)
    if excluded_statuses:
        statuses = [status for status in statuses if status not in excluded_statuses]
    if people.get("names") and people.get("role") == "handler":
        statuses = [status for status in statuses if status != "assigned"]

    excluded_issue_ids = extract_excluded_issue_ids(text)
    issue_ids = [issue_id for issue_id in extract_issue_ids(text) if issue_id not in excluded_issue_ids]

    filter_clauses: List[Dict[str, Any]] = []
    if customer_key:
        add_filter_clause(filter_clauses, "customer", "equals", customer_key)
    for _, excluded_customer, _ in sorted(excluded_customer_matches, reverse=True):
        add_filter_clause(filter_clauses, "customer", "not_equals", excluded_customer)
    if platform_key:
        add_filter_clause(filter_clauses, "platform", "equals", platform_key)
    for excluded_platform in excluded_platform_keys:
        add_filter_clause(filter_clauses, "platform", "not_equals", excluded_platform)
    if statuses:
        add_filter_clause(filter_clauses, "status", "in", statuses)
    if excluded_statuses:
        add_filter_clause(filter_clauses, "status", "not_in", excluded_statuses)
    if issue_ids:
        add_filter_clause(filter_clauses, "issue_id", "in", issue_ids)
    if excluded_issue_ids:
        add_filter_clause(filter_clauses, "issue_id", "not_in", excluded_issue_ids)
    screen_numbers = extract_screen_numbers(text)
    if screen_numbers:
        add_filter_clause(filter_clauses, "screen_number", "in", screen_numbers)
    created_at = extract_created_at_range(text)
    if created_at:
        add_filter_clause(
            filter_clauses,
            "created_at",
            "between",
            [created_at["from"], created_at["to"]],
        )
    filter_clauses.extend(extract_explicit_keyword_filter_clauses(text))

    return {
        "customer_key": customer_key,
        "customer_aliases": customer_aliases,
        "customer_exact": customer_exact,
        "platform_key": platform_key,
        "platform_aliases": platform_aliases,
        "excluded_platform_keys": list(dict.fromkeys(excluded_platform_keys)),
        "excluded_platform_aliases": list(dict.fromkeys(excluded_platform_aliases)),
        "excluded_customer_keys": list(
            dict.fromkeys(match[1] for match in excluded_customer_matches)
        ),
        "excluded_customer_aliases": list(
            dict.fromkeys(alias for _, _, aliases in excluded_customer_matches for alias in aliases)
        ),
        "people": people,
        "statuses": statuses,
        "excluded_statuses": excluded_statuses,
        "issue_ids": issue_ids,
        "exclude_issue_ids": excluded_issue_ids,
        "screen_numbers": screen_numbers,
        "date_fields": extract_date_field_constraints(text),
        "created_at": created_at,
        "unassigned_people": unassigned_people,
        "filter_clauses": filter_clauses,
    }


def has_constraint(constraints: Dict[str, Any]) -> bool:
    return bool(
        constraints.get("customer_key")
        or constraints.get("platform_key")
        or constraints.get("excluded_customer_keys")
        or constraints.get("excluded_platform_keys")
        or constraints.get("statuses")
        or constraints.get("issue_ids")
        or constraints.get("screen_numbers")
        or constraints.get("date_fields")
        or constraints.get("created_at")
        or constraints.get("people", {}).get("names")
        or constraints.get("unassigned_people")
        or constraints.get("filter_clauses")
    )


def legacy_filters_to_clauses(filters: Any) -> List[Dict[str, Any]]:
    """기존 ``filters`` 객체를 공통 조건식으로 변환한다."""
    if isinstance(filters, list):
        return normalize_filter_clauses(filters)
    if not isinstance(filters, dict):
        return []
    clauses: List[Dict[str, Any]] = []
    for field, legacy_key in (
        ("customer", "customer"),
        ("platform", "platform"),
        ("owner", "people"),
        ("issue_id", "issue_ids"),
        ("screen_number", "screen_numbers"),
    ):
        value = filters.get(legacy_key)
        if not value:
            continue
        if legacy_key == "people" and isinstance(value, dict):
            value = value.get("names") or []
        add_filter_clause(
            clauses,
            field,
            "in" if isinstance(value, list) else "equals",
            value,
        )
    if filters.get("statuses"):
        add_filter_clause(clauses, "status", "in", filters["statuses"])
    created_at = filters.get("created_at")
    if isinstance(created_at, dict) and created_at.get("from") and created_at.get("to"):
        add_filter_clause(
            clauses,
            "created_at",
            "between",
            [created_at["from"], created_at["to"]],
        )
    return clauses


def merge_query_plan_constraints(
    constraints: Dict[str, Any],
    query_plan: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Apply validated planner filters without trusting arbitrary model values."""
    merged = {
        **constraints,
        "customer_aliases": list(constraints.get("customer_aliases") or []),
        "platform_aliases": list(constraints.get("platform_aliases") or []),
        "excluded_platform_keys": list(constraints.get("excluded_platform_keys") or []),
        "excluded_platform_aliases": list(constraints.get("excluded_platform_aliases") or []),
        "excluded_customer_keys": list(constraints.get("excluded_customer_keys") or []),
        "excluded_customer_aliases": list(constraints.get("excluded_customer_aliases") or []),
        "excluded_statuses": list(constraints.get("excluded_statuses") or []),
        "people": dict(constraints.get("people") or {"names": [], "role": None}),
        "statuses": list(constraints.get("statuses") or []),
        "issue_ids": list(constraints.get("issue_ids") or []),
        "exclude_issue_ids": list(constraints.get("exclude_issue_ids") or []),
        "screen_numbers": list(constraints.get("screen_numbers") or []),
        "date_fields": dict(constraints.get("date_fields") or {}),
        "filter_clauses": normalize_filter_clauses(constraints.get("filter_clauses") or []),
    }
    filters = (query_plan or {}).get("filters") or {}

    planned_clauses = normalize_filter_clauses((query_plan or {}).get("filter_clauses") or [])
    planned_clauses.extend(
        clause for clause in legacy_filters_to_clauses(filters) if clause not in planned_clauses
    )
    for clause in planned_clauses:
        if clause not in merged["filter_clauses"]:
            merged["filter_clauses"].append(clause)
        field, operator, value = clause["field"], clause["operator"], clause.get("value")
        values = value if isinstance(value, list) else [value]
        if field == "platform":
            for planned_value in values:
                key = normalize_for_match(str(planned_value))
                matched = next(
                    (
                        (platform_key, aliases)
                        for platform_key, aliases in PLATFORM_ALIASES.items()
                        if key in {normalize_for_match(platform_key), *(normalize_for_match(alias) for alias in aliases)}
                    ),
                    None,
                )
                if not matched:
                    continue
                platform_key, aliases = matched
                if operator in {"not_equals", "not_in"}:
                    merged["excluded_platform_keys"].append(platform_key)
                    merged["excluded_platform_aliases"].extend(aliases)
                    if merged.get("platform_key") == platform_key:
                        merged["platform_key"], merged["platform_aliases"] = None, []
                elif operator in {"equals", "in"} and not merged.get("platform_key"):
                    merged["platform_key"], merged["platform_aliases"] = platform_key, list(aliases)
        elif field == "customer":
            for planned_value in values:
                planned_customer = extract_query_constraints(str(planned_value))
                if operator in {"not_equals", "not_in"}:
                    merged["excluded_customer_keys"].append(str(planned_value))
                    merged["excluded_customer_aliases"].extend(planned_customer.get("customer_aliases") or [str(planned_value)])
                elif operator in {"equals", "in"} and not merged.get("customer_key"):
                    if planned_customer.get("customer_key"):
                        for key_name in ("customer_key", "customer_aliases", "customer_exact"):
                            merged[key_name] = planned_customer.get(key_name)
        elif field == "status" and operator in {"not_equals", "not_in"}:
            merged["excluded_statuses"].extend(str(item) for item in values)
        elif field == "status" and operator in {"equals", "in"} and not merged.get("statuses"):
            merged["statuses"] = [str(item) for item in values]
        elif field in {"owner", "reporter"} and operator in {"equals", "in"}:
            merged["people"] = {
                "names": list(dict.fromkeys(str(item) for item in values)),
                "role": "handler" if field == "owner" else "reporter",
            }
        elif field == "issue_id":
            numeric_values = [int(item) for item in values if str(item).isdigit()]
            if operator in {"not_equals", "not_in"}:
                merged["exclude_issue_ids"].extend(numeric_values)
            elif operator in {"equals", "in"}:
                merged["issue_ids"] = list(dict.fromkeys([*merged.get("issue_ids", []), *numeric_values]))
        elif field == "screen_number" and operator in {"equals", "in"}:
            merged["screen_numbers"] = list(
                dict.fromkeys([*merged.get("screen_numbers", []), *(str(item) for item in values)])
            )
        elif field == "created_at" and operator == "between" and len(values) == 2:
            start, end = str(values[0]), str(values[1])
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) and start <= end:
                merged["created_at"] = {"from": start, "to": end, "precision": "range"}

    if isinstance(filters, dict) and not merged.get("customer_key") and filters.get("customer"):
        planned_customer = extract_query_constraints(str(filters["customer"]))
        if planned_customer.get("customer_key"):
            for key in ("customer_key", "customer_aliases", "customer_exact"):
                merged[key] = planned_customer.get(key)

    if isinstance(filters, dict) and not merged.get("platform_key") and filters.get("platform"):
        planned_platform = extract_query_constraints(str(filters["platform"]))
        if planned_platform.get("platform_key"):
            merged["platform_key"] = planned_platform["platform_key"]
            merged["platform_aliases"] = planned_platform["platform_aliases"]

    created_at = filters.get("created_at") if isinstance(filters, dict) else None
    if isinstance(created_at, dict):
        start, end = str(created_at.get("from", "")), str(created_at.get("to", ""))
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", start) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", end) and start <= end:
            merged["created_at"] = {
                "from": start,
                "to": end,
                "precision": str(created_at.get("precision") or "range"),
            }
    for key in (
        "excluded_platform_keys",
        "excluded_platform_aliases",
        "excluded_customer_keys",
        "excluded_customer_aliases",
        "excluded_statuses",
    ):
        merged[key] = list(dict.fromkeys(merged.get(key) or []))
    return merged


def rule_based_search_intent(text: str, constraints: Dict[str, Any]) -> Dict[str, Any]:
    """Choose a source deterministically before asking the model planner.

    The model planner remains a fallback for genuinely ambiguous natural-language
    requests. Explicit source names and structured fields must never be overridden
    by a probabilistic classification.
    """
    normalized = normalize_for_match(text)
    explicit_notion = any(normalize_for_match(marker) in normalized for marker in EXPLICIT_NOTION_MARKERS)
    explicit_mantis = any(normalize_for_match(marker) in normalized for marker in EXPLICIT_MANTIS_MARKERS)
    combined_marker = any(normalize_for_match(marker) in normalized for marker in COMBINED_SOURCE_MARKERS)
    notion_domain = any(normalize_for_match(marker) in normalized for marker in NOTION_DOMAIN_MARKERS)
    mantis_domain = any(normalize_for_match(marker) in normalized for marker in MANTIS_DOMAIN_MARKERS)
    structured_mantis = has_constraint(constraints) and not notion_domain

    if is_simple_greeting(text):
        return {"source": "general", "terms": [], "reason": "단순 인사", "planned": True, "confidence": 1.0}
    if explicit_notion and explicit_mantis:
        source, reason, confidence = "both", "두 검색 소스를 모두 명시", 1.0
    elif combined_marker and notion_domain and (mantis_domain or structured_mantis):
        source, reason, confidence = "both", "Notion 업무와 Mantis 이슈를 함께 요청", 0.98
    elif explicit_notion:
        source, reason, confidence = "notion", "Notion을 검색 대상으로 명시", 1.0
    elif explicit_mantis:
        source, reason, confidence = "mantis", "Mantis를 검색 대상으로 명시", 1.0
    elif notion_domain and not mantis_domain:
        source, reason, confidence = "notion", "팀 문서·조직 운영 키워드를 인식", 0.92
    elif structured_mantis:
        source, reason, confidence = "mantis", "이슈 구조화 조건을 인식", 0.98
    elif mantis_domain and not notion_domain:
        source, reason, confidence = "mantis", "고객 이슈·개발 업무 키워드를 인식", 0.92
    elif any(normalize_for_match(keyword) in normalized for keyword in GENERAL_INTENT_KEYWORDS):
        source, reason, confidence = "general", "일반 대화 키워드를 인식", 0.95
    else:
        source, reason, confidence = "general", "명확한 검색 소스를 규칙으로 확정하지 못함", 0.35

    return {
        "source": source,
        "terms": [],
        "reason": reason,
        "planned": True,
        "planner": "rule",
        "confidence": confidence,
        "needs_model": confidence < 0.7 and should_use_intent_planner(text, constraints),
    }


def should_run_retrieval(text: str, constraints: Dict[str, Any]) -> bool:
    if is_simple_greeting(text):
        return False
    normalized_text = normalize_for_match(text)
    if should_run_notion_search(text):
        return True
    if constraints.get("customer_key") or constraints.get("platform_key"):
        return True
    if (
        constraints.get("issue_ids")
        or constraints.get("screen_numbers")
        or constraints.get("statuses")
        or constraints.get("date_fields")
    ):
        return True
    if constraints.get("people", {}).get("names"):
        return True
    if any(normalize_for_match(keyword) in normalized_text for keyword in RETRIEVAL_INTENT_KEYWORDS):
        return True
    if any(normalize_for_match(keyword) in normalized_text for keyword in GENERAL_INTENT_KEYWORDS):
        return False
    return False


def should_run_notion_search(text: str) -> bool:
    normalized_text = normalize_for_match(text)
    return any(
        normalize_for_match(keyword) in normalized_text
        for keyword in NOTION_INTENT_KEYWORDS
    )


def should_use_intent_planner(text: str, constraints: Dict[str, Any]) -> bool:
    if is_simple_greeting(text):
        return False
    normalized_text = normalize_for_match(text)
    explicit_source = any(
        normalize_for_match(marker) in normalized_text
        for marker in ("notion", "노션", "노선", "mantis", "멘티스", "맨티스")
    )
    if has_constraint(constraints) and not explicit_source:
        return False
    search_markers = (
        "찾아",
        "검색",
        "조회",
        "보여",
        "알려",
        "정리",
        "문서",
        "자료",
        "내역",
        "현황",
        "일정",
        "공지",
        "관련",
    )
    return any(normalize_for_match(marker) in normalized_text for marker in search_markers)


def extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text or "").strip(), flags=re.IGNORECASE)
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None


def plan_search_intent_with_model(
    question: str,
    model: Dict[str, Any],
    api_key: str,
) -> Dict[str, Any]:
    fallback = {
        "source": "general",
        "operation": infer_operation(question),
        "filters": {},
        "terms": [],
        "reason": "planner_fallback",
        "planned": False,
    }
    today_str = date.today().isoformat()
    prompt = (
        f"오늘은 {today_str}이다. "
        "사용자 질문을 사내 도구 실행 계획 JSON으로만 변환해라. "
        "source는 notion, mantis, both, general 중 하나다. "
        "operation은 search, count, get, aggregate 중 하나다. '몇 개/몇 건/건수'는 count이고, "
        "'N개 보여줘'는 search이며, 합계/총합은 aggregate다. "
        "notion은 팀 공지, 회식, 휴가, 일정, 회의, 계약, 업무 공유, 문서 및 조직 운영 자료다. "
        "mantis는 고객사 소프트웨어 이슈, 오류, 장애, 화면번호, 담당자, 상태, 개발/QC 일정, 공수 및 과거 장애 이력이다. "
        "KRX 제도개선, HTS, MTS, 차트, 조건검색 관련 요청은 별도 언급이 없어도 mantis다. "
        "both는 사용자가 두 데이터 소스를 함께 요구할 때만 사용한다. "
        "general은 사내 데이터 검색이 필요 없는 일반 대화다. "
        "filters는 [{\"field\":필드,\"operator\":연산자,\"value\":값}] 배열이다. "
        "field는 customer, platform, status, owner, reporter, issue_id, screen_number, created_at, "
        "title, keyword, database 중 하나다. operator는 equals, not_equals, in, not_in, contains, "
        "not_contains, exists, not_exists, gte, lte, between 중 하나다. "
        "'빼고/제외/아닌/말고/없는'은 반드시 not_equals, not_in, not_contains, not_exists 중 하나로 표현해라. "
        "예: 'MTS는 빼고'는 {\"field\":\"platform\",\"operator\":\"not_equals\",\"value\":\"mts\"}다. "
        "'종료 안 된/종료되지 않은/미종료'는 {\"field\":\"status\",\"operator\":\"not_equals\",\"value\":\"closed\"}다. "
        "'고객사별로/담당자별로/상태별로/유형별로'처럼 결과를 묶어 달라는 요청은 필터로 만들지 말고 "
        "각각 group_by를 customer/owner/status/type으로 출력해라. "
        "created_at은 등록일 기준이며 from/to를 YYYY-MM-DD로 출력한다. 예를 들어 2025년은 "
        "{\"from\":\"2025-01-01\",\"to\":\"2025-12-31\"}이다. "
        "'오늘/어제/이번 주/지난주/최근 N일'처럼 상대적인 시점도 위 오늘 날짜를 기준으로 "
        "실제 YYYY-MM-DD 범위로 계산해서 넣어라. "
        "terms에는 조사와 요청 표현을 제외한 실제 검색어만 최대 6개 넣어라. "
        "limit은 '5개 보여줘'처럼 표시 개수가 명시된 경우에만 숫자로 넣고 count에는 null을 넣어라. "
        "질문이 작업 공수(공수, 작업량, 투입 시간) 집계에 관한 것이면 effort 필드를 추가해라. "
        "특정 인물의 공수 합산이면 {\"type\":\"person\",\"person\":\"이름\"}이고, "
        "누가 많이/적게 했는지 순위·비교를 물으면 {\"type\":\"ranking\"}이다. "
        "공수와 무관한 질문에는 effort를 넣지 마라. "
        "형식: {\"source\":\"mantis\",\"operation\":\"count\",\"filters\":["
        "{\"field\":\"customer\",\"operator\":\"equals\",\"value\":\"신한\"},"
        "{\"field\":\"created_at\",\"operator\":\"between\",\"value\":[\"2025-01-01\",\"2025-12-31\"]}],"
        "\"terms\":[],\"limit\":null,"
        "\"sort\":{\"field\":\"created_at\",\"direction\":\"desc\"},\"reason\":\"연도별 고객사 이슈 집계\"}"
    )
    payload = {
        "model": model.get("model", model["id"]),
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": question},
        ],
        "temperature": 0,
        "max_tokens": 300,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        response = post_model_chat(
            model,
            api_key,
            payload,
            timeout=max(5, min(int(os.getenv("INTENT_PLANNER_TIMEOUT_SECONDS", "25")), 45)),
            think=False,
        )
        if response.status_code >= 400:
            return fallback
        data = response.json()
        content = extract_model_content(data)
        parsed = extract_json_object(content)
        if not parsed:
            return fallback
    except (requests.RequestException, ValueError, TypeError):
        return fallback

    source = str(parsed.get("source", "general")).strip().lower()
    if source not in {"notion", "mantis", "both", "general"}:
        source = "general"
    normalized_question = normalize_for_match(question)
    explicit_notion = any(
        normalize_for_match(marker) in normalized_question
        for marker in ("notion", "노션", "노선")
    )
    explicit_mantis = any(
        normalize_for_match(marker) in normalized_question
        for marker in ("mantis", "멘티스", "맨티스")
    )
    if explicit_notion and explicit_mantis:
        source = "both"
        parsed["reason"] = "사용자가 Notion과 Mantis를 모두 명시함"
    elif explicit_notion:
        source = "notion"
        parsed["reason"] = "사용자가 Notion을 검색 대상으로 명시함"
    elif explicit_mantis:
        source = "mantis"
        parsed["reason"] = "사용자가 Mantis를 검색 대상으로 명시함"
    terms = []
    for value in parsed.get("terms", []):
        term = normalize_search_token(str(value).strip())
        if (
            not term
            or len(term) < 2
            or len(term) > 40
            or term in STOPWORDS
            or term in NOTION_QUERY_STOPWORDS
        ):
            continue
        terms.append(term)
    unique_terms = list(dict.fromkeys(terms))[:6]
    notion_status_match = re.search(
        r"결재완료\s*\([^)]+\)|결재완료|결재요청|등록됨|보관됨",
        question,
    )
    if source in {"notion", "both"} and notion_status_match:
        status_term = re.sub(r"\s+", "", notion_status_match.group(0))
        unique_terms = [
            term
            for term in unique_terms
            if normalize_for_match(term) not in normalize_for_match(status_term)
        ]
        unique_terms.append(status_term)
    if len(unique_terms) > 1:
        unique_terms = [term for term in unique_terms if term not in {"일정", "공지", "항목"}]
    return {
        "source": source,
        "operation": str(parsed.get("operation") or parsed.get("intent") or infer_operation(question)).lower(),
        "filters": parsed.get("filters") if isinstance(parsed.get("filters"), (dict, list)) else {},
        "filter_clauses": normalize_filter_clauses(
            parsed.get("filters") if isinstance(parsed.get("filters"), list) else parsed.get("filter_clauses")
        ),
        "sort": parsed.get("sort") if isinstance(parsed.get("sort"), dict) else {},
        "group_by": str(parsed.get("group_by") or "").strip().lower() or None,
        "limit": parsed.get("limit") if isinstance(parsed.get("limit"), int) else None,
        "terms": unique_terms,
        "effort": parsed.get("effort") if isinstance(parsed.get("effort"), dict) else None,
        "reason": str(parsed.get("reason", ""))[:120],
        "confidence": 0.85,
        "planned": True,
    }


def text_matches_constraints(text: str, constraints: Dict[str, Any]) -> bool:
    if constraints.get("customer_exact") and normalize_for_match(constraints["customer_exact"]) not in normalize_for_match(text):
        return False
    if constraints.get("customer_aliases") and not text_matches_customer_aliases(
        text,
        constraints["customer_aliases"],
    ):
        return False
    if constraints.get("platform_aliases") and not text_matches_any_alias(
        text,
        constraints["platform_aliases"],
    ):
        return False
    if constraints.get("excluded_customer_aliases") and text_matches_customer_aliases(
        text,
        constraints["excluded_customer_aliases"],
    ):
        return False
    if constraints.get("excluded_platform_aliases") and text_matches_any_alias(
        text,
        constraints["excluded_platform_aliases"],
    ):
        return False
    normalized_text = normalize_for_match(text)
    for clause in normalize_filter_clauses(constraints.get("filter_clauses") or []):
        if clause["field"] not in {"title", "keyword"}:
            continue
        values = clause.get("value") if isinstance(clause.get("value"), list) else [clause.get("value")]
        normalized_values = [normalize_for_match(value) for value in values]
        if clause["operator"] == "contains" and not any(value in normalized_text for value in normalized_values):
            return False
        if clause["operator"] == "not_contains" and any(value in normalized_text for value in normalized_values):
            return False
    return True


def text_matches_status(text: str, statuses: List[str]) -> bool:
    if not statuses:
        return True
    return any(
        text_matches_any_alias(text, STATUS_MATCH_ALIASES.get(status, [status]))
        for status in statuses
    )


def text_matches_screen_numbers(text: str, screen_numbers: List[str]) -> bool:
    if not screen_numbers:
        return True
    # 감사/검증 단계에서도 숫자 부분 문자열(예: 9413이 194130에 포함됨)을
    # 화면번호 일치로 인정하지 않는다. 검색 단계의 필드/제목 정확 일치 규칙과
    # 동일하게 독립된 숫자 토큰만 허용한다.
    return any(
        re.search(rf"(?<!\d){re.escape(screen)}(?!\d)", str(text or ""))
        for screen in screen_numbers
    )


def chroma_text_matches_date_fields(text: str, date_fields: Dict[str, str]) -> bool:
    if not date_fields:
        return True
    return all(
        format_mantis_custom_date(extract_chroma_field(text, field_name)) == expected_date
        for field_name, expected_date in date_fields.items()
    )


def chroma_text_matches_created_at(text: str, created_at: Optional[Dict[str, str]]) -> bool:
    return plan_created_at_matches(extract_chroma_date(text), created_at)


def title_has_screen_number(title: str, screen_number: str) -> bool:
    return bool(re.search(rf"\[\s*{re.escape(screen_number)}\s*\]", title))


def field_matches_screen_number(value: str, screen_number: str) -> bool:
    return normalize_for_match(value) == normalize_for_match(screen_number)


def chroma_text_matches_screen_numbers(text: str, screen_numbers: List[str]) -> bool:
    if not screen_numbers:
        return True
    screen_field = extract_chroma_field(text, "화면번호")
    title = extract_title_from_chroma_text(text)
    return any(
        field_matches_screen_number(screen_field, screen_number)
        or title_has_screen_number(title, screen_number)
        for screen_number in screen_numbers
    )


def text_matches_issue_ids(text: str, issue_ids: List[int]) -> bool:
    if not issue_ids:
        return True
    normalized_text = normalize_for_match(text)
    return any(str(issue_id) in normalized_text for issue_id in issue_ids)


def text_matches_people(text: str, people: Dict[str, Any], role_text: str = "") -> bool:
    names = people.get("names", []) if people else []
    if not names:
        return True

    role = people.get("role") or "any"
    search_text = role_text or text
    if role == "handler":
        return any(text_matches_any_alias(search_text, [name]) for name in names)
    if role == "reporter":
        from_text = " ".join(
            match.group(0)
            for match in re.finditer(r"From\.\s*[가-힣A-Za-z]{2,6}", text, re.IGNORECASE)
        )
        reporter_context = " ".join([from_text, text])
        return any(text_matches_any_alias(reporter_context, [name]) for name in names)
    return any(text_matches_any_alias(text, [name]) for name in names)


def chroma_text_matches_constraints(text: str, constraints: Dict[str, Any]) -> bool:
    issue_id = extract_issue_id_from_text(text)
    if issue_id in constraints.get("exclude_issue_ids", []):
        return False
    max_issue_id = constraints.get("max_issue_id_exclusive")
    if max_issue_id and issue_id >= int(max_issue_id):
        return False
    if constraints.get("customer_exact") and normalize_for_match(
        extract_project_from_chroma_text(text)
    ) != normalize_for_match(constraints["customer_exact"]):
        return False
    if constraints.get("customer_aliases") and not text_matches_customer_aliases(
        extract_project_from_chroma_text(text),
        constraints["customer_aliases"],
    ):
        return False
    if constraints.get("excluded_customer_aliases") and text_matches_customer_aliases(
        extract_project_from_chroma_text(text),
        constraints["excluded_customer_aliases"],
    ):
        return False
    if constraints.get("platform_aliases"):
        # 재구축된 스냅샷 문서에는 '매체구분' 헤더가 있다. 있으면 그 값으로만 판정해
        # 본문의 "MTS에서는 됩니다" 같은 비교 언급 오탐을 막는다(라이브 검색의
        # issue_matches_platform과 동일한 규칙). 구버전 문서(필드 없음)는 기존처럼
        # 전문 검색으로 폴백한다.
        media_type = extract_chroma_field(text, "매체구분")
        platform_source = media_type if media_type and media_type != "기타/미상" else text
        if not text_matches_any_alias(platform_source, constraints["platform_aliases"]):
            return False
    if constraints.get("excluded_platform_aliases"):
        media_type = extract_chroma_field(text, "매체구분")
        platform_source = media_type if media_type and media_type != "기타/미상" else text
        if text_matches_any_alias(platform_source, constraints["excluded_platform_aliases"]):
            return False
    if not text_matches_status(
        extract_chroma_field(text, "현재 상태"),
        constraints.get("statuses", []),
    ):
        return False
    if constraints.get("excluded_statuses") and text_matches_status(
        extract_chroma_field(text, "현재 상태"),
        constraints["excluded_statuses"],
    ):
        return False
    if constraints.get("unassigned_people"):
        handler = extract_chroma_field(text, "담당자").strip()
        if handler and handler not in {"담당자 미지정", "미지정", "미상"}:
            return False
    if not text_matches_issue_ids(text, constraints.get("issue_ids", [])):
        return False
    if not chroma_text_matches_screen_numbers(text, constraints.get("screen_numbers", [])):
        return False
    if not chroma_text_matches_date_fields(text, constraints.get("date_fields", {})):
        return False
    if not chroma_text_matches_created_at(text, constraints.get("created_at")):
        return False
    if not text_matches_people(
        text,
        constraints.get("people", {}),
        extract_chroma_field(text, "담당자"),
    ):
        return False
    if not text_matches_constraints(text, {"filter_clauses": constraints.get("filter_clauses", [])}):
        return False
    return True


def term_is_covered_by_constraints(term: str, constraints: Dict[str, Any]) -> bool:
    aliases = [
        constraints.get("customer_key") or "",
        constraints.get("customer_exact") or "",
        constraints.get("platform_key") or "",
        *constraints.get("customer_aliases", []),
        *constraints.get("platform_aliases", []),
        *constraints.get("excluded_customer_aliases", []),
        *constraints.get("excluded_platform_aliases", []),
        *constraints.get("people", {}).get("names", []),
        *constraints.get("statuses", []),
        *constraints.get("excluded_statuses", []),
        *[
            alias
            for status in constraints.get("statuses", [])
            for alias in STATUS_ALIASES.get(status, [])
        ],
        *[
            alias
            for status in constraints.get("statuses", [])
            for alias in STATUS_MATCH_ALIASES.get(status, [])
        ],
        *[str(issue_id) for issue_id in constraints.get("issue_ids", [])],
        *constraints.get("screen_numbers", []),
        *constraints.get("date_fields", {}).keys(),
        *constraints.get("date_fields", {}).values(),
        *((constraints.get("created_at") or {}).values()),
        # "2026년에 등록된 ..."처럼 등록일 조건이 이미 created_at으로 흡수된 경우,
        # "등록된" 같은 표현이 키워드로 남으면 본문에 그 단어가 없는 이슈가
        # 전부 걸러진다. 날짜 조건이 있을 때는 등록/접수 계열 표현도 조건에
        # 포함된 것으로 간주해 키워드에서 제외한다.
        *(
            ("등록", "등록된", "접수", "접수된", "생성", "생성된", "올라온", "들어온")
            if constraints.get("created_at")
            else ()
        ),
        *(
            ("올해", "금년", "작년", "전년", "지난해", "지난년도", "이번해", "이번년도")
            if constraints.get("created_at")
            else ()
        ),
    ]
    normalized_term = normalize_for_match(term)
    return any(
        normalized_term == normalize_for_match(alias)
        or normalized_term in normalize_for_match(alias)
        or normalize_for_match(alias) in normalized_term
        for alias in aliases
        if alias
    )


def build_keyword_terms(text: str, constraints: Optional[Dict[str, Any]] = None) -> List[str]:
    terms = [normalize_search_token(term) for term in extract_mantis_keyword(text).split() if term]
    filter_control_terms = {
        "중", "제목", "본문", "내용", "키워드", "빼고", "제외", "제외하고",
        "말고", "아닌", "아니고", "없는", "없이", "있지만", "포함",
        "고객사별", "고객사마다", "프로젝트별", "프로젝트마다",
        "하나씩", "한개씩", "한건씩", "1개씩", "1건씩",
    }
    terms = [
        term
        for term in terms
        if term
        and term.lower() not in STOPWORDS
        and term not in STOPWORDS
        and term not in filter_control_terms
        and not is_control_search_term(term)
    ]
    if constraints:
        terms = [term for term in terms if not term_is_covered_by_constraints(term, constraints)]
    return terms[:4]


def wants_similar_issue_search(text: str, constraints: Optional[Dict[str, Any]] = None) -> bool:
    constraints = constraints or extract_query_constraints(text)
    if not constraints.get("issue_ids"):
        return False
    normalized = normalize_for_match(text)
    return any(
        marker in normalized
        for marker in (
            "비슷",
            "유사",
            "같은",
            "동일",
            "관련사례",
            "과거사례",
            "과거이슈",
        )
    )


def split_project_search_terms(project: str) -> List[str]:
    return [
        term
        for term in re.split(r"[\s()/,]+", str(project or ""))
        if term and term != "미상"
    ][:2]


def expand_compound_search_term(term: str, project_terms: List[str]) -> List[str]:
    normalized_projects = [normalize_for_match(value) for value in project_terms]
    match = re.match(r"^([A-Za-z]{2,})([가-힣].+)$", term)
    if not match:
        return [term]
    prefix, suffix = match.groups()
    expanded = []
    if not any(normalize_for_match(prefix) in project for project in normalized_projects):
        expanded.append(prefix)
    expanded.append(suffix)
    return expanded


def build_similar_issue_search_text(issue: Dict[str, Any]) -> str:
    title = str(issue.get("summary") or issue.get("title") or "")
    description = str(issue.get("description") or "")

    # 고객사와 화면번호는 유사 현상의 본질이 아니라 우연히 같은 프로젝트를 상위로
    # 끌어올리는 강한 편향이 된다. 벡터 질의에는 기능/증상/재현 조건만 담고, 사용자가
    # 질문에서 고객사나 매체를 명시한 경우에만 별도의 구조화 필터로 적용한다.
    clean_title = re.sub(r"^\s*(?:\[[^\]]+\]\s*)+", "", title).strip()
    clean_description = re.sub(r"https?://\S+", " ", description)
    clean_description = re.sub(r"\s+", " ", clean_description).strip()
    description_is_useful = bool(
        clean_description
        and len(clean_description) <= 400
        and "메일 수신" not in clean_description
        and normalize_for_match(clean_description) != normalize_for_match(clean_title)
    )
    return " ".join([clean_title, clean_description if description_is_useful else ""]).strip()


def extract_requested_limit(text: str) -> Optional[int]:
    match = re.search(r"(\d+)\s*(개|건)\s*만?", text)
    if not match:
        return None
    return max(1, min(int(match.group(1)), 20))


def wants_all_results(text: str) -> bool:
    normalized = normalize_for_match(text)
    return any(
        marker in normalized
        for marker in (
            "다보여",
            "다알려",
            "전부보여",
            "전부알려",
            "모두보여",
            "모두알려",
            "전체보여",
            "전체알려",
            "전체리스트",
            "이슈리스트다",
            "목록다",
        )
    )


def resolve_result_limit(text: str, sidebar_limit: Optional[int], default: int = 5) -> int:
    requested_limit = extract_requested_limit(text)
    if requested_limit:
        return requested_limit
    if wants_all_results(text):
        # 실제 조건에 맞는 이슈가 이 값을 넘으면 format_deterministic_issue_answer가
        # "총 N건 중 M건 표시" 안내를 붙인다. 그래도 "전체"라고 물었을 때 흔한
        # 케이스(연간 수십~백여 건)는 잘리지 않도록 넉넉히 잡는다.
        return 200
    return sidebar_limit or default


def extract_limit(text: str, default: int = 5) -> int:
    return resolve_result_limit(text, None, default)


# --- 대화 맥락/후속 질문 해석 (app.py에서 이동) ---

def get_latest_user_text(messages: List[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content.strip()
    return ""


FOLLOW_UP_STRONG_MARKERS = (
    "그중", "그 중", "이중", "이 중", "그거", "그것", "그건", "그때", "그 이슈", "그 건",
    "해당", "위에", "위 이슈", "아까", "방금",
    "첫번째", "첫 번째", "두번째", "두 번째", "세번째", "세 번째", "마지막",
)


FOLLOW_UP_WEAK_MARKERS = (
    "자세히", "자세하게", "추가로", "담당자", "상태",
    "진행", "언제", "왜", "어떻게", "무슨", "몇건", "몇 건",
    "빼줘", "빼고", "제외", "말고", "바꿔", "변경", "개만", "건만", "최근",
)


FOLLOW_UP_WEAK_MAX_LEN = 20


def get_recent_user_texts(messages: List[ChatMessage], limit: int = 5) -> List[str]:
    texts: List[str] = []
    for message in reversed(messages):
        if message.role == "user":
            content = (message.content or "").strip()
            if content:
                texts.append(content)
            if len(texts) >= limit:
                break
    return texts


def is_follow_up_question(text: str) -> bool:
    normalized = normalize_for_match(text)
    if any(normalize_for_match(marker) in normalized for marker in FOLLOW_UP_STRONG_MARKERS):
        return True
    if len(text.strip()) > FOLLOW_UP_WEAK_MAX_LEN:
        return False
    return any(normalize_for_match(marker) in normalized for marker in FOLLOW_UP_WEAK_MARKERS)


KOREAN_ORDINAL_WORDS = {
    "첫": 1, "두": 2, "세": 3, "네": 4, "다섯": 5,
    "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
}


def parse_ordinal_reference(text: str) -> Optional[int]:
    """'그중 3번째', '첫 번째', '마지막 이슈'에서 목록 위치를 뽑는다.

    1-base 인덱스를 반환하고 '마지막'은 -1. 참조 표현이 없으면 None.
    '마지막'은 "마지막으로 하나만 더..." 같은 담화 표지로도 흔히 쓰여서,
    뒤에 목록 항목을 가리키는 명사가 오거나 '그중'과 함께 쓰일 때만 인정한다."""
    match = re.search(r"(\d{1,2})\s*번째", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(첫|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*번째", text)
    if match:
        return KOREAN_ORDINAL_WORDS[match.group(1)]
    if re.search(r"첫째|첫번째", text):
        return 1
    if re.search(r"그\s*중\s*마지막|마지막\s*(?:이슈|항목|건|거|것|내용|결과)", text):
        return -1
    return None


def resolve_reference_from_context(text: str, audit_id: Optional[str]) -> Optional[str]:
    """서수 참조 후속 질문을 직전 답변의 실제 결과 목록으로 해석한 라우팅 텍스트.

    "그중 3번째 자세히"는 재검색으로 같은 목록이 다시 나오길 기대하는 대신,
    감사 로그에 저장된 '사용자가 실제로 본' 목록의 3번째 항목(이슈ID/제목)을
    직접 가리키게 한다. 해석에 실패하면 None을 반환해 기존 규칙 기반
    상속/LLM 재작성 경로로 폴백한다."""
    if not audit_id:
        return None
    ordinal = parse_ordinal_reference(text)
    if ordinal is None:
        return None
    latest_constraints = extract_query_constraints(text)
    if (
        latest_constraints.get("customer_key")
        or latest_constraints.get("platform_key")
        or latest_constraints.get("issue_ids")
        or latest_constraints.get("screen_numbers")
    ):
        # 자체 조건을 가진 독립 질문이면 서수는 목록 참조가 아닐 가능성이 크다.
        return None
    sources = load_audit_sources(audit_id)
    if not sources:
        return None
    index = len(sources) - 1 if ordinal == -1 else ordinal - 1
    if index < 0 or index >= len(sources):
        return None
    source = sources[index]
    issue_id = source.get("issue_id")
    if issue_id:
        # extract_issue_ids가 5자리 미만 ID도 인식하도록 7자리 0패딩으로 표기한다.
        return f"{int(issue_id):07d} {text}"
    if source.get("type") == "notion" and source.get("title"):
        return f'노션에서 "{source["title"]}" {text}'
    return None


def build_contextual_retrieval_text(messages: List[ChatMessage]) -> Tuple[str, bool]:
    """검색 라우팅용 텍스트를 만든다.

    마지막 사용자 메시지가 자체 조건(고객사/플랫폼/인물/이슈ID/화면번호)을 가진
    독립 질문이면 그대로 사용한다. 자체 조건이 없는 후속 질문이면 직전 사용자 턴에서
    조건을 물려받아 앞에 붙인다. 이 텍스트는 검색/라우팅에만 쓰이고 모델이나 화면에
    사용자 메시지로 노출되지 않는다(모델은 별도로 전체 히스토리를 받는다).

    반환값은 (텍스트, LLM 재작성이 필요한지 여부)다. 두 번째 값은 마지막 메시지가
    후속 질문으로 보이는데도 규칙 기반으로 아무 조건도 상속하지 못했을 때만 True다
    (예: 마커 사전에 없는 새로운 표현으로 참조하는 경우). 이 경우 호출부에서
    LLM 기반 재작성(rewrite_follow_up_query_with_model)으로 보완할 수 있다."""
    recent = get_recent_user_texts(messages, limit=5)
    if not recent:
        return "", False
    latest = recent[0]
    latest_constraints = extract_query_constraints(latest)
    follow_up = is_follow_up_question(latest)
    if (
        latest_constraints.get("customer_key")
        or latest_constraints.get("platform_key")
        or latest_constraints.get("issue_ids")
        or latest_constraints.get("screen_numbers")
        or latest_constraints.get("people", {}).get("names")
    ) and not follow_up:
        return latest, False
    if not follow_up:
        return latest, False

    inherited: List[str] = []
    seen_customer = bool(latest_constraints.get("customer_key") or latest_constraints.get("excluded_customer_keys"))
    seen_platform = bool(latest_constraints.get("platform_key") or latest_constraints.get("excluded_platform_keys"))
    seen_status = bool(latest_constraints.get("statuses") or latest_constraints.get("excluded_statuses"))
    seen_issue_ids = False
    seen_screen_numbers = False
    for prior in recent[1:]:
        prior_constraints = extract_query_constraints(prior)
        # 이슈ID/화면번호처럼 특정 대상을 정확히 가리키는 조건을 최우선으로 상속한다.
        # 그래야 "이슈 3187번 알려줘" -> "그 이슈 상태 어때?" 같은 후속 질문이
        # 대상을 잃지 않는다.
        if not seen_issue_ids and prior_constraints.get("issue_ids"):
            inherited.extend(str(issue_id) for issue_id in prior_constraints["issue_ids"])
            seen_issue_ids = True
        if not seen_screen_numbers and prior_constraints.get("screen_numbers"):
            # extract_screen_numbers는 대괄호([1234]) 또는 "화면번호" 접두어가 있어야
            # 다시 인식하므로, 상속 시에도 같은 형식으로 감싸야 재추출된다.
            inherited.extend(f"[{number}]" for number in prior_constraints["screen_numbers"])
            seen_screen_numbers = True
        if not seen_customer and prior_constraints.get("customer_aliases"):
            inherited.append(prior_constraints["customer_aliases"][0])
            seen_customer = True
        if not seen_customer and prior_constraints.get("excluded_customer_keys"):
            inherited.extend(f"{value} 제외" for value in prior_constraints["excluded_customer_keys"])
            seen_customer = True
        if not seen_platform and prior_constraints.get("platform_key"):
            inherited.append(prior_constraints["platform_key"])
            seen_platform = True
        if not seen_platform and prior_constraints.get("excluded_platform_keys"):
            inherited.extend(f"{value} 제외" for value in prior_constraints["excluded_platform_keys"])
            seen_platform = True
        if not seen_status and prior_constraints.get("statuses"):
            inherited.extend(prior_constraints["statuses"])
            seen_status = True
        if not seen_status and prior_constraints.get("excluded_statuses"):
            inherited.extend(f"{value} 제외" for value in prior_constraints["excluded_statuses"])
            seen_status = True
        for name in prior_constraints.get("people", {}).get("names") or []:
            if name not in inherited:
                inherited.append(name)
        if seen_customer and seen_platform and seen_status and seen_issue_ids and seen_screen_numbers:
            break

    if not inherited:
        return latest, True
    return f"{' '.join(inherited)} {latest}".strip(), False


def rewrite_follow_up_query_with_model(
    recent_user_texts: List[str],
    model: Dict[str, Any],
    api_key: str,
) -> Optional[str]:
    """규칙 기반 상속이 실패한 후속 질문을 모델에게 독립형 질문으로 재작성시킨다.

    recent_user_texts는 최신순(가장 최근이 0번)으로 들어온다. 실패하면 None을 반환해
    호출부가 규칙 기반 결과(원문 그대로)로 폴백하게 한다."""
    if len(recent_user_texts) < 2:
        return None
    ordered = list(reversed(recent_user_texts))
    history_block = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(ordered))
    prompt = (
        "아래는 사용자의 최근 대화 턴이다(오래된 순으로 번호가 매겨져 있다). "
        "마지막 번호가 가장 최근 질문이며, 이전 턴을 가리키는 후속 질문이다. "
        "마지막 질문만 그 자체로 검색 가능한 독립형 질문 한 문장으로 다시 써라. "
        "이전 턴에서 언급된 고객사, 담당자, 이슈ID, 화면번호처럼 검색 조건에 필요한 "
        "정보만 반영하고 새로운 사실을 지어내지 마라. "
        "결과는 재작성된 질문 문장 하나만 출력해라. 설명, 번호, 따옴표는 붙이지 마라."
    )
    payload = {
        "model": model.get("model", model["id"]),
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": history_block},
        ],
        "temperature": 0,
        "max_tokens": 200,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        response = post_model_chat(
            model,
            api_key,
            payload,
            timeout=max(5, min(int(os.getenv("FOLLOWUP_REWRITE_TIMEOUT_SECONDS", "20")), 45)),
            think=False,
        )
        if response.status_code >= 400:
            return None
        data = response.json()
        content = extract_model_content(data)
    except (requests.RequestException, ValueError, TypeError):
        return None

    rewritten = re.sub(r"^```(?:text)?\s*|\s*```$", "", str(content or "").strip()).strip()
    rewritten = rewritten.strip("\"'“”‘’").strip()
    if not rewritten or len(rewritten) > 300:
        return None
    return rewritten
