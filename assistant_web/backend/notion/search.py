"""Notion 제약 추출, 검색, 필터링과 랭킹을 담당한다."""

import os

from backend.notion.body_index import segment_belongs_to_page
import json
import re
import time
from datetime import date, datetime, timedelta, timezone
from contextlib import closing
from dataclasses import dataclass, field
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import requests
from fastapi import HTTPException

from backend.common import (
    LOOKUP_REQUEST_PATTERN,
    is_valid_person_name,
    normalize_for_match,
    normalize_search_token,
    remove_quoted_value,
    strip_korean_particle,
)
from backend.config import (
    NOTION_API_BASE,
    RUNTIME_DIR,
    STOPWORDS,
    get_notion_settings as _get_notion_settings,
    notion_post_json as _notion_post_json,
    notion_unavailable_reason,
)
from backend.intent.constants import NOTION_QUERY_STOPWORDS
from backend.execution import CURRENT_BUDGET
from backend.ordered_prefetch import iter_in_order
from backend.intent.entities import load_known_people
from backend.notion.client import (
    NOTION_CONTENT_INCOMPLETE,
    fetch_notion_database_schema as _fetch_notion_database_schema,
    fetch_notion_database_title as _fetch_notion_database_title,
    fetch_notion_page_blocks as _fetch_notion_page_blocks,
    fetch_notion_users as _fetch_notion_users,
)
from backend.notion.compat import compat_dependency
from backend.notion.constants import (
    NOTION_BODY_SCAN_MAX_PER_REQUEST,
    NOTION_IN_ENDING_NOUNS,
    NOTION_RESULT_BODY_MAX_BLOCKS,
    NOTION_STATUS_QUERY_TERMS,
    NOTION_STATUS_TYPOS,
)
from backend.notion.mapping import normalize_notion_page, resolve_notion_page_owner
from backend.notion.read_scope import resolve_notion_read_scope
from backend.notion.query_text import grounded_notion_body_constraints, notion_literal_title, notion_registration_sort, notion_structural_query, split_notion_body_query, without_registration_order
from backend.notion.query_cache import cached_title_query
from backend.notion.body_index_search import find_indexed_body_candidates
from backend.notion.database_roles import select_notion_database_ids
from backend.notion.property_roles import (
    CUSTOMER_NAMES,
    DEPLOYMENT_NAMES,
    DUE_NAMES,
    REGISTRATION_NAMES,
    responsible_properties,
    role_properties,
)
from backend.notion.schema import (
    notion_property_schema as _notion_property_schema,
    notion_title_property,
)
from backend.query_filters import clause_supported, matches_source_filters, normalize_filter_clauses, values_for_clause, notion_temporal_field
from backend.query_planning import (
    GROUP_BY_PATTERNS,
    extract_created_at_range,
    extract_group_by,
    extract_yearless_temporal_month_range,
    has_count_intent,
)


# 의존 명사와 조사는 붙여 쓰기도 하지만, 다음 일반 명사의 일부까지
# 소비해서는 안 된다. 이 표현 뒤에는 반드시 어절 경계를 확인한다.
_STATUS_BOUND_NOUN = r"(?:것|거|건)(?:들)?(?:만|은|는|이|가|을|를|도)?"
_STATUS_SPOKEN_SUFFIX = rf"(?:(?:인|된)\s*)?{_STATUS_BOUND_NOUN}"
_STATUS_WORD_END = r"(?:\s|$|[,.;!?])"
_OWNER_PREDICATE = r"(?:맡|담당|처리)[가-힣]*"
_OWNER_SUFFIXES = ("이면서", "으로", "이고", "이며", "인", "이", "가", "은", "는", "의", "로")
_DATE_VALUE_TERM = re.compile(
    r"(?:"
    r"\d{1,4}[년월일]|20\d{2}[-./]\d{1,2}(?:[-./]\d{1,2})?|"
    r"이번[달주월해]?|금주|금년|올해|작년|재작년|내년|"
    r"지난[달주월해]?|저번[달주월]?|전주|다음[달주월해]?|"
    r"오늘|어제|내일|최근|"
    # "이번 달에"처럼 띄어 쓰면 단위만 조사와 함께 남는다.
    r"[달주월해](?=에|엔|부터|까지))"
    r"(?:된|인|에|에는|엔|부터|까지|만|자)?"
)
# "이번 달 중에·동안·안에", "올해 들어"처럼 기간 뒤에 붙는 말.
_DATE_RANGE_TAIL_TERM = re.compile(r"중에|동안(?:에)?|안에|이내(?:에)?|내에|들어(?:서)?")
_CREATED_DATE_VERB = re.compile(
    r"(?:등록(?:된|한|됐|되었|되어)|접수(?:된|한|됐|되었|되어)|"
    r"생성(?:된|한|됐|되었|되어)|작성(?:된|한|됐|되었|되어)|만든|올라온|들어온)"
    r"(?:(?:것|거|게|건)(?:들)?(?:만|은|는|이|가|을|를|도)?)?"
)
_PERSONAL_OWNER_QUERY = re.compile(
    r"(?<![가-힣])(?:내|제|나|저)(?:가|랑|와|과|의|에게|한테|게)?"
    r"(?:\s*관련(?:된|있는)?)?(?=\s|$|[,.;!?])"
)
_EXISTENCE_LIST_QUERY = re.compile(
    r"(?:문서|항목|페이지|자료|업무)(?:가|은|는)?\s*있(?:어(?:요)?|나요|는지)(?=\s|$|[,.;!?])"
)
_NOTION_USER_ID_HINTS: Dict[str, str] = {}
_DATE_FIELD_TERMS = {
    "created_at": {"등록일", "접수일", "생성일", "작성일"},
    "due_at": {"마감", "기한", "마감일"},
    "deployed_at": {"운영반영", "배포일"},
}


def get_notion_settings() -> Dict[str, Any]:
    return compat_dependency("get_notion_settings", _get_notion_settings)()

def notion_post_json(*args: Any, **kwargs: Any) -> requests.Response:
    return compat_dependency("notion_post_json", _notion_post_json)(*args, **kwargs)

def fetch_notion_database_schema(database_id: str) -> Dict[str, Any]:
    return compat_dependency("fetch_notion_database_schema", _fetch_notion_database_schema)(database_id)

def fetch_notion_database_title(database_id: str) -> str:
    return compat_dependency("fetch_notion_database_title", _fetch_notion_database_title)(database_id)

def fetch_notion_page_blocks(*args: Any, **kwargs: Any) -> str:
    return compat_dependency("fetch_notion_page_blocks", _fetch_notion_page_blocks)(*args, **kwargs)

def fetch_notion_users() -> tuple[tuple[str, str], ...]:
    return compat_dependency("fetch_notion_users", _fetch_notion_users)()

def notion_property_schema(database_id: str) -> Dict[str, Any]:
    return compat_dependency("notion_property_schema", _notion_property_schema)(database_id)


def correct_notion_status_typo(text: str) -> Tuple[str, Optional[Tuple[str, str]]]:
    """Notion 상태 오타를 바로잡은 텍스트와 (원문, 보정값)을 돌려준다."""
    for wrong, right in NOTION_STATUS_TYPOS.items():
        if wrong in text:
            return text.replace(wrong, right), (wrong, right)
    return text, None


_NOTION_FIELD_QUESTIONS = {
    "location": re.compile(
        r"(?<![가-힣])어디(?:에서|서)?(?:해|하[가-힣]*|야|예요|인가요|인지|죠|요)?(?=\s|$|[.!?])"
        r"|(?:장소|위치)(?:는|은|가|이|을|를)?\s*(?:알려[가-힣]*|확인(?:해줘|해주세요)|어디[가-힣]*)(?=\s|$|[.!?])"
    ),
    "schedule": re.compile(
        r"(?<![가-힣])언제(?:해|하[가-힣]*|야|예요|인가요|인지|죠|요)?(?=\s|$|[.!?])"
        r"|(?:일정|일시|날짜|시간)(?:는|은|가|이|을|를)?\s*(?:알려[가-힣]*|확인(?:해줘|해주세요)|언제[가-힣]*)(?=\s|$|[.!?])"
    ),
}
_NOTION_REQUESTED_FIELD_TERMS = {
    "location": {"장소", "위치", "어디", "어디서"},
    "schedule": {"일정", "일시", "날짜", "시간", "언제"},
}


def notion_requested_information_fields(text: str) -> List[str]:
    return [kind for kind, pattern in _NOTION_FIELD_QUESTIONS.items() if pattern.search(remove_quoted_value(text))]


def notion_subject_text(text: str) -> str:
    """본문에서 읽을 정보의 질문 표현을 검색 대상 명사와 분리한다."""
    for pattern in _NOTION_FIELD_QUESTIONS.values():
        text = pattern.sub(" ", text)
    return re.sub(r"(회식|송년회|신년회|팀빌딩)(?:일정|일시|장소)", r"\1", text)


def ground_notion_terms(text: str, terms: List[str], constraints: Dict[str, Any]) -> List[str]:
    """플래너가 빠뜨린 분기를 복원하고 본문 질문을 제목의 AND 조건에서 뺀다."""
    if constraints.get("exact_title"):
        return terms
    requested_fields = {
        normalize_for_match(term)
        for kind, pattern in _NOTION_FIELD_QUESTIONS.items()
        if pattern.search(text)
        for term in _NOTION_REQUESTED_FIELD_TERMS[kind]
    }
    grounded = [
        re.sub(r"^(회식|송년회|신년회|팀빌딩)(?:일정|일시|장소)$", r"\1", term)
        for term in terms if normalize_for_match(term) not in requested_fields
    ]
    if notion_registration_sort(text):
        subject = normalize_for_match(without_registration_order(notion_structural_query(text)))
        grounded = [term for term in grounded if term not in {"등록", "접수", "생성", "작성"}
                    or normalize_for_match(term) in subject]
    excluded_quarters = set()
    for match in re.finditer(r"(?<!\d)[1-4]\s*분기", text):
        quarter = re.sub(r"\s+", "", match.group())
        tail = text[match.end():]
        registration_period = re.match(
            r"(?:에|동안|중에|중)?\s*(?:등록|접수|생성|작성)(?:된|한|됐|되었)", tail,
        )
        if registration_period or re.match(r"(?:는|은|를|을)?\s*(?:말고|제외|빼고|아닌)", tail):
            excluded_quarters.add(quarter)
        else:
            grounded.append(quarter)
    grounded = [term for term in grounded if normalize_for_match(term) not in excluded_quarters]
    return remove_structured_date_terms(list(dict.fromkeys(grounded))[:8], {
        **constraints, "sort": notion_registration_sort(text) or constraints.get("sort"),
    })


def notion_keyword_subject_text(text: str, constraints: Dict[str, Any]) -> Tuple[str, set[str]]:
    """부정 키워드 조건으로 소비한 값과 요청 표현을 자유 검색 대상에서 분리한다."""
    consumed: set[str] = set()
    if constraints.get("exact_title"):
        return text, consumed
    for clause in constraints.get("common_filter_clauses", []):
        if clause.get("field") != "keyword" or clause.get("operator") not in {"not_contains", "not_equals", "not_in"}:
            continue
        for value in values_for_clause(clause.get("value")):
            pattern = re.compile(
                rf"(?<![가-힣A-Za-z0-9_]){re.escape(value)}(?:은|는|이|가|을|를)?\s*"
                r"(?:제외(?:한|된|하고|해서|해|해줘|해주세요)?|빼고|뺀|빼줘|빼주세요|말고|아닌)"
                r"(?=\s|$|[,.;!?])", re.IGNORECASE,
            )

            def remove_request(match: re.Match[str]) -> str:
                consumed.update(normalize_for_match(normalize_search_token(token)) for token in match.group().split())
                return " "

            text = pattern.sub(remove_request, text)
    return text, consumed


def build_notion_terms(text: str, constraints: Optional[Dict[str, Any]] = None) -> List[str]:
    if not notion_literal_title(text):
        constraints = {**(constraints or {}), "sort": notion_registration_sort(text) or (constraints or {}).get("sort")}
    text, body_clauses = split_notion_body_query(text)
    if body_clauses and (constraints or {}).get("exact_title"):
        # 정확한 제목은 서버 필터가 처리한다. 제목 지정 문구까지 본문 AND 검색어로 넣지 않는다.
        return []
    if not (constraints or {}).get("exact_title"):
        text = notion_subject_text(text)
        if not notion_literal_title(text):
            text = without_registration_order(text)
    text, _consumed = notion_keyword_subject_text(text, constraints or {})
    owner_values = [
        value
        for clause in (constraints or {}).get("common_filter_clauses", [])
        if clause.get("field") == "owner"
        for value in values_for_clause(clause.get("value"))
    ]
    if (constraints or {}).get("owner") or owner_values:
        text = _PERSONAL_OWNER_QUERY.sub(" ", text)
    if (constraints or {}).get("unassigned_owner"):
        # 담당자 유무는 속성 필터가 처리한다. '없는'을 별도 본문 검색어로 남기지 않는다.
        text = re.sub(
            r"(?<![가-힣A-Za-z0-9_])담당자?\s*(?:가|는|를|도)?\s*"
            r"(?:없는|없이|없음|미지정|미배정|안\s*정해진|지정\s*안\s*된|할당\s*안\s*된|배정\s*안\s*된)",
            " ", text,
        )
    text = _EXISTENCE_LIST_QUERY.sub(" ", text)
    for pattern in GROUP_BY_PATTERNS.values():
        text = pattern.sub(" ", text)
    text = re.sub(
        r"(?<![가-힣A-Za-z0-9_])(?:정리|요약|설명|묶어)(?:해)?"
        r"(?:줘(?:요)?|주세요|줄래(?:요)?|주실래(?:요)?)(?=\s|$|[.!?])",
        " ", text,
    )
    text = LOOKUP_REQUEST_PATTERN.sub(" ", text)
    cleaned = re.sub(r"[#,:;!?()\[\]{}\"']", " ", text)
    cleaned = re.sub(r"\d+\s*(개|건)\s*만?", " ", cleaned)
    cleaned = re.sub(r"(?<![가-힣])(?:문서|항목|페이지|자료)(?:들)?(?:에서|에는|만|을|를|은|는)?(?=\s|$)", " ", cleaned)
    notion_stopwords = {
        normalize_for_match(keyword)
        for keyword in {*STOPWORDS, *NOTION_QUERY_STOPWORDS, "관한", "관해서", "관하여"}
    }
    if any(clause.get("operator") == "in" and len(values_for_clause(clause.get("value"))) > 1
           for clause in (constraints or {}).get("common_filter_clauses", [])):
        notion_stopwords.update({"또는", "혹은"})
    terms: List[str] = []
    for token in cleaned.split():
        normalized = token if token in NOTION_IN_ENDING_NOUNS else normalize_search_token(token)
        if not normalized or len(normalized) < 2 or normalize_for_match(normalized) in notion_stopwords:
            continue
        if normalized.isdigit():
            continue
        terms.append(normalized)
    normalized_text = normalize_for_match(text)
    if "결재요청" in normalized_text and "결재요청" not in terms:
        terms.append("결재요청")
    deduplicated = list(dict.fromkeys(terms))[:8]
    return remove_structured_date_terms(deduplicated, constraints or {})


def remove_structured_date_terms(terms: List[str], constraints: Dict[str, Any]) -> List[str]:
    """구조화된 기간 조건과 같은 뜻인 자유 검색어를 제거한다."""
    clauses = constraints.get("common_filter_clauses") or constraints.get("filter_clauses") or []
    date_fields = {
        clause.get("field")
        for clause in clauses
        if clause.get("operator") in {"between", "equals", "gte", "lte"}
    }
    if constraints.get("date_filter"):
        date_fields.add(str(constraints["date_filter"].get("field") or ""))
    if (constraints.get("sort") or {}).get("field") == "created_at":
        date_fields.add("created_at")
    if not date_fields & {"created_at", "due_at", "deployed_at"}:
        return terms
    field_terms = {
        normalize_for_match(value)
        for field in date_fields
        for value in _DATE_FIELD_TERMS.get(str(field), set())
    }
    created_at = "created_at" in date_fields
    return [
        term for term in terms
        if not _DATE_VALUE_TERM.fullmatch(term)
        and not _DATE_RANGE_TAIL_TERM.fullmatch(term)
        and normalize_for_match(term) not in field_terms
        and not (created_at and _CREATED_DATE_VERB.fullmatch(term))
        and not (created_at and re.fullmatch(r"(?:등록|접수|생성|작성)(?:일)?순(?:으로)?", term))
    ]


def trim_owner_adnominal(candidate: str, following_text: str = "") -> str:
    """이름 자체를 우선 보존하고 확인 가능한 조사만 정리한다."""
    name = str(candidate or "").strip()
    handlers, reporters = load_known_people()
    known = set(handlers) | set(reporters)
    if name in known or len(name) <= 3:
        return name
    for suffix in _OWNER_SUFFIXES:
        if name.endswith(suffix) and len(name) > 2 and name[: -len(suffix)] in known:
            return name[: -len(suffix)]
    # 명단에 없는 4글자 이름 자체를 조사로 줄이지 않는다. 긴 연결
    # 어미나 원래 이름이 될 수 없는 길이는 분리하고, 짧은 조사는
    # 뒤 문법까지 확인한다 ('김지은이 맡은', '김지은의 문서').
    for suffix in _OWNER_SUFFIXES:
        if name.endswith(suffix) and 3 <= len(name) - len(suffix) <= 4:
            grammatical_suffix = (
                suffix in {"이", "가", "은", "는"}
                and re.match(rf"\s+{_OWNER_PREDICATE}", following_text)
            ) or (
                suffix == "의" and re.match(r"\s+(?:문서|항목|업무|이슈)", following_text)
            )
            if len(suffix) > 1 or len(name) > 4 or grammatical_suffix:
                return name[: -len(suffix)]
    return name


# Mantis 명단에는 svn·signalmaker·DRFN_SM 같은 연동 계정도 섞여 있다.
# '담당자' 표지가 없는 표현에서는 제품명과 구별되는 한글 이름과
# 대문자로 시작하는 영문 이름만 사람으로 본다.
_IMPLICIT_OWNER_NAME = re.compile(r"[가-힣]{2,4}|[A-Z][a-z]{1,19}")


@lru_cache(maxsize=8)
def _known_notion_owner_matcher(
    handlers: tuple[str, ...], reporters: tuple[str, ...],
) -> tuple[Optional[re.Pattern[str]], Dict[str, Tuple[int, str]]]:
    """스냅샷에서 읽은 명단 내용별로 검증·정렬·정규식 생성을 한 번만 한다."""
    names = sorted(
        (
            name for name in set(handlers) | set(reporters)
            if _IMPLICIT_OWNER_NAME.fullmatch(name) and is_valid_person_name(name)
        ),
        key=lambda name: (-len(name), name),
    )
    if not names:
        return None, {}
    pattern = re.compile(
        r"(?<![가-힣A-Za-z])(?P<owner>" + "|".join(re.escape(name) for name in names) + r")(?:님)?"
        rf"(?:(?:이|가|은|는)?\s+(?:{_OWNER_PREDICATE}|소유|작성|등록|할당)"
        r"|의\s+(?:문서|항목|업무|이슈))",
        re.IGNORECASE,
    )
    return pattern, {name.casefold(): (rank, name) for rank, name in enumerate(names)}


def known_notion_owner(text: str) -> str:
    """담당자 표지가 없는 표현은 실제 명단에 있는 이름만 인정한다."""
    # load_known_people은 스냅샷 경로·수정 시각·크기 변경을 감지한다.
    # 명단 내용이 바뀌면 새 matcher를 만들고, 내용이 같으면 재사용한다.
    handlers, reporters = load_known_people()
    pattern, names = _known_notion_owner_matcher(tuple(handlers), tuple(reporters))
    if pattern is None:
        return ""
    # 기존의 긴 이름 우선 선택을 유지하면서 텍스트는 합친 패턴으로 검색한다.
    match = min(
        pattern.finditer(text), key=lambda item: names[item.group("owner").casefold()][0], default=None,
    )
    return names[match.group("owner").casefold()][1] if match else ""


def inherit_notion_context(recent_texts: List[str], *, prior_source: str = "") -> str:
    """후속 질문이 잃어버린 Notion 조건(DB·상태)을 이전 턴에서 물려받는다.

    "노션에서 결재요청 상태인 항목 보여줘" 다음의 "담당자별로 묶어줘"는
    Mantis 쪽 조건 상속만으로는 상태와 소스를 모두 잃는다. 반환한 접두
    텍스트를 질문 앞에 붙이면 기존 조건 추출이 그대로 다시 인식한다.
    """
    if len(recent_texts) < 2:
        return ""
    from backend.intent.context import is_affirmative_read_follow_up, is_follow_up_question
    from backend.intent.source_router import has_notion_retrieval_signal

    if re.search(r"mantis|맨티스|노션|notion", recent_texts[0], re.IGNORECASE) and not re.search(
        r"그\s*(?:중|항목|문서)|방금|그대로|상태만|담당자만", recent_texts[0],
    ):
        return ""
    if not is_follow_up_question(recent_texts[0]) and not re.search(
        r"(?:상태|담당자)만.*(?:바꿔|변경)", recent_texts[0],
    ):
        return ""
    latest = extract_notion_constraints(recent_texts[0])
    for prior in recent_texts[1:]:
        independent = not is_follow_up_question(prior)
        if is_affirmative_read_follow_up(recent_texts[0]) and not independent and not is_affirmative_read_follow_up(prior):
            # 중간 턴의 변경 조건은 확정된 감사 로그 또는 문맥 재작성으로 복원한다.
            return ""
        if is_affirmative_read_follow_up(recent_texts[0]) and independent and (
            prior_source == "notion" or has_notion_retrieval_signal(prior)
        ):
            # 이전 사용자 질문을 다시 조회한다. 모델의 이전 답변은 근거로 쓰지 않는다.
            return f"노션 {prior}"
        if not re.search(r"노션|notion|이슈리스트|주요이슈", prior, re.IGNORECASE):
            if independent:
                break
            continue
        prior_constraints = extract_notion_constraints(prior)
        parts = []
        for key in ("database_label", "exact_title", "status", "owner"):
            value = prior_constraints.get(key)
            if not value or latest.get(key):
                continue
            if key == "status" and latest.get("excluded_statuses"):
                continue
            if key == "owner" and latest.get("unassigned_owner"):
                continue
            parts.append({
                "database_label": str(value), "exact_title": f'"{value}"',
                "status": f"상태가 {value}인", "owner": f"담당자 {value}",
            }[key])
        if not latest.get("status") and not latest.get("excluded_statuses"):
            parts.extend(f"{value} 제외" for value in prior_constraints.get("excluded_statuses", []))
        if prior_constraints.get("unassigned_owner") and not latest.get("owner"):
            parts.append("담당자 미지정")
        if parts:
            # 소스가 Notion이라는 사실도 함께 물려줘야 Mantis로 새지 않는다.
            return " ".join([*parts, "노션"])
        if independent:
            break
    return ""


def extract_notion_database_label_from_text(text: str) -> Optional[str]:
    normalized_text = normalize_for_match(text)
    if "이슈리스트" in normalized_text:
        return "이슈리스트"
    if "주요이슈" in normalized_text or "주요이슈현황" in normalized_text:
        return "주요이슈"
    # 라벨은 질문의 표현과 DB 제목 양쪽에 공통으로 들어가는 부분이어야
    # 검색어 목록에서 빠지고 DB 범위도 좁혀진다. ('조직도' → 'DB00_조직')
    if "조직도" in normalized_text:
        return "조직"
    # UI와 대화에서는 DB14_문서양식을 보통 "템플릿" 또는 "양식"이라고 부른다.
    # 목록 탐색일 때만 DB 별칭으로 취급해 구체적인 문서 키워드 검색은 넓게 유지한다.
    if (
        any(alias in normalized_text for alias in ("템플릿", "양식"))
        and any(marker in normalized_text for marker in ("목록", "리스트", "전체", "보여", "어떤"))
    ):
        return "문서양식"
    # 질문에 쓰는 표현이 DB 제목의 일부이면 그대로 라벨로 쓴다.
    for label in (
        "DB02_통합_프로젝트", "DB03_작업", "버전관리", "기술공유",
        "회의록", "사내규정", "휴가신청", "정기보고서", "정기점검",
        "문서양식", "주간업무", "팀세미나",
    ):
        if normalize_for_match(label) in normalized_text:
            return label
    return None


def remove_notion_database_label_terms(terms: List[str], database_label: str) -> List[str]:
    """DB를 선택하는 표현을 본문 AND 검색어에서 제거한다."""
    database_target = normalize_for_match(database_label)
    if not database_target:
        return terms
    aliases = {database_target}
    if database_target == normalize_for_match("문서양식"):
        aliases.update({normalize_for_match("템플릿"), normalize_for_match("양식")})
    return [
        term
        for term in terms
        if not any(alias in normalize_for_match(term) for alias in aliases)
    ]


def extract_notion_constraints(text: str) -> Dict[str, Any]:
    exact_title = notion_literal_title(text)
    constraint_text = notion_structural_query(text)
    # 인용된 제목은 사용자가 지정한 정확한 문자열이므로 건드리지 않는다.
    # DB 옵션은 고유한 값이다. '결제완료'가 실제 옵션인 DB도 있으므로
    # 스키마를 보지 않은 상태에서 '결재완료'로 고쳐 쓰지 않는다.
    status_typo = None
    status = ""
    excluded_statuses: List[str] = []
    # 기존 상태명은 다양한 활용형(완료건수/완료처리된/시작전이었던)을
    # 계속 인식한다. 합성어와 모호한 '대기'에만 엄격한 뒤 경계를 둔다.
    remaining_status_text = constraint_text
    for candidate in sorted(NOTION_STATUS_QUERY_TERMS | NOTION_STATUS_TYPOS.keys(), key=len, reverse=True):
        spelling = r"\s*".join(re.escape(char) for char in candidate)
        boundary = (
            rf"(?=(?:{_STATUS_SPOKEN_SUFFIX}|상태|인|으로|로|만|은|는|이|가|을|를|된)?{_STATUS_WORD_END})"
            if candidate == "대기" else ""
        )
        pattern = re.compile(r"(?<![가-힣A-Za-z0-9_])" + spelling + boundary, re.IGNORECASE)
        for match in pattern.finditer(remaining_status_text):
            if re.match(
                rf"\s*(?:{_STATUS_SPOKEN_SUFFIX}|상태|항목|문서|이슈)?"
                r"\s*(?:은|는|이|가)?\s*(?:아닌|말고|제외|빼고)",
                remaining_status_text[match.end():],
            ):
                excluded_statuses.append(candidate)
            elif not status:
                status = candidate
        remaining_status_text = pattern.sub(" ", remaining_status_text)

    # DB별 사용자 정의 상태도 명시적인 'X 상태는 빼고'에서 추출한다.
    # 제목은 이미 제거했으므로 제목 속 단어를 필터로 해석하지 않는다.
    for match in re.finditer(
        r"(?<![가-힣A-Za-z0-9_])([가-힣A-Za-z0-9_()-]+)\s+상태"
        r"\s*(?:은|는|이|가|을|를)?\s*(?:아닌|말고|제외|빼고)",
        constraint_text,
    ):
        value = match.group(1)
        if value not in excluded_statuses:
            excluded_statuses.append(value)
        if status == value:
            status = ""

    # 사용자 정의 포함 상태는 명시적인 '상태가 X인' 표현만 받는다.
    # '상태가 변경된 항목'의 형용사를 옵션 이름으로 만들지 않는다.
    explicit_status = re.search(
        r"상태\s*(?:가|는|=|:)\s*([가-힣A-Za-z0-9_()-]+?)"
        rf"(?:인(?=(?:\s*{_STATUS_BOUND_NOUN})?{_STATUS_WORD_END})|(?:으로|로)(?={_STATUS_WORD_END}))",
        constraint_text,
    )
    if explicit_status:
        value = explicit_status.group(1).strip()
        if value and value not in excluded_statuses and not any(
            word in explicit_status.group(0) for word in ("말고", "제외", "빼고", "아닌")
        ):
            status = value

    group_by = extract_group_by(constraint_text)
    owner_text = re.sub(
        r"(?:담당자|담당)\s*(?:별(?:로)?|기준(?:으)?로|마다)",
        " ",
        constraint_text,
    )
    owner = known_notion_owner(owner_text)
    # "담당자 미지정 항목만"은 미지정이라는 이름을 찾는 게 아니라 담당자가
    # 비어 있는 항목을 찾는 요청이다.
    unassigned_owner = bool(
        re.search(
            r"담당자?\s*(?:가|는|를|도)?\s*"
            r"(?:없는|없이|없음|미지정|미배정|안\s*정해진|지정\s*안\s*된|할당\s*안\s*된|배정\s*안\s*된)",
            constraint_text,
        )
    )
    owner_match = re.search(
        # 조사 "가"를 함께 소비하지 않으면 "담당자가"가 담당+자가로 쪼개져
        # "자가"가 이름으로 잡힌다.
        r"(?:담당자|담당|owner|기여자|공헌자|공동\s*작업자|contributors?)\s*(?:가|는|은|를|을|만|=|:)?\s*"
        r"([가-힣]{2,8}|[A-Za-z]{2,20})(?=\s|$|[,.;!?])",
        owner_text,
        re.IGNORECASE,
    )
    if owner_match and not unassigned_owner and not owner:
        candidate = trim_owner_adnominal(owner_match.group(1), owner_text[owner_match.end(1):])
        # 조사가 붙은 일반 명사가 이름으로 잡히지 않도록 어간까지 검증한다.
        if candidate not in {"문서", "항목", "업무", "자료", "페이지", "이슈", "모두"} and is_valid_person_name(candidate) and is_valid_person_name(
            strip_korean_particle(candidate)
        ):
            owner = candidate
    if not owner and not unassigned_owner:
        owner = known_notion_owner(owner_text)
    if unassigned_owner:
        owner = ""

    result = {
        "database_label": extract_notion_database_label_from_text(constraint_text) or "",
        "exact_title": exact_title,
        "status": status,
        "excluded_statuses": excluded_statuses,
        "owner": owner,
        "unassigned_owner": unassigned_owner,
        "status_typo": status_typo,
        "native_status_constraint": bool(status or excluded_statuses),
        "group_by": group_by,
    }
    temporal_field = notion_temporal_field(constraint_text)
    date_range = (
        extract_created_at_range(constraint_text)
        or extract_yearless_temporal_month_range(constraint_text, temporal_field)
        if temporal_field != "created_at" else None
    )
    if date_range:
        result["date_filter"] = {"field": temporal_field, "operator": "between", "value": [date_range["from"], date_range["to"]]}
    registration_sort = notion_registration_sort(text)
    if registration_sort:
        result["sort"] = registration_sort
    return result


def merge_common_notion_constraints(
    notion_constraints: Dict[str, Any],
    constraints: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """공통 필터 계획을 Notion 검색/후처리 조건으로 연결한다."""
    merged = {
        **notion_constraints,
        "common_filter_clauses": normalize_filter_clauses((constraints or {}).get("filter_clauses") or []),
        "customer_aliases": list((constraints or {}).get("customer_aliases") or []),
        "excluded_statuses": list(
            dict.fromkeys(
                [
                    *(notion_constraints.get("excluded_statuses") or []),
                    *((constraints or {}).get("excluded_statuses") or []),
                ]
            )
        ),
    }
    if notion_constraints.get("native_status_constraint"):
        # Notion 원문 상태를 Mantis의 open/closed로 재해석한 공통 조건과
        # 교집합하면 실제 DB 옵션과 모순되어 정상 문서도 모두 사라진다.
        native_status = normalize_for_match(str(merged.get("status") or ""))
        merged["common_filter_clauses"] = [
            clause for clause in merged["common_filter_clauses"]
            if clause["field"] != "status" or (
                clause["operator"] == "in"
                and len(values_for_clause(clause.get("value"))) > 1
                and native_status in {
                    normalize_for_match(value) for value in values_for_clause(clause.get("value"))
                }
            )
        ]
        merged["excluded_statuses"] = list(notion_constraints.get("excluded_statuses") or [])
    for clause in merged["common_filter_clauses"]:
        values = values_for_clause(clause.get("value"))
        if clause["field"] == "customer" and clause["operator"] in {"equals", "in"}:
            aliases = [
                str(alias)
                for alias in merged.get("customer_aliases", [])
                if len(normalize_for_match(str(alias))) >= 3
            ]
            expanded = list(dict.fromkeys([*values, *aliases]))
            clause["operator"] = "in"
            clause["value"] = expanded
            values = expanded
        if clause["field"] == "owner" and merged.get("owner"):
            owner = str(merged["owner"])
            corrected = [owner if value in {owner + suffix for suffix in _OWNER_SUFFIXES} else value for value in values]
            if corrected != values:
                clause["value"] = corrected if isinstance(clause.get("value"), list) else corrected[0]
                values = corrected
        if clause["field"] == "status":
            if clause["operator"] in {"not_equals", "not_in"}:
                merged["excluded_statuses"].extend(values)
            elif clause["operator"] == "in" and len(values) > 1:
                # 원문 파서가 잡은 첫 값이 OR 후보에 포함돼도 단일 AND 조건으로 남기지 않는다.
                if normalize_for_match(str(merged.get("status") or "")) in {
                    normalize_for_match(value) for value in values
                }:
                    merged["status"] = ""
            elif clause["operator"] in {"equals", "in"} and values and not merged.get("status"):
                merged["status"] = values[0]
        elif clause["field"] == "owner" and clause["operator"] in {"equals", "in"}:
            if len(values) > 1:
                if normalize_for_match(str(merged.get("owner") or "")) in {
                    normalize_for_match(value) for value in values
                }:
                    merged["owner"] = ""
            elif values and not merged.get("owner"):
                merged["owner"] = values[0]
        elif clause["field"] == "database" and clause["operator"] in {"equals", "contains"} and values and not merged.get("database_label"):
            merged["database_label"] = values[0]
        elif clause["field"] == "title" and clause["operator"] == "equals" and values and not merged.get("exact_title"):
            merged["exact_title"] = values[0]
    merged["excluded_statuses"] = list(dict.fromkeys(merged["excluded_statuses"]))
    return merged


def notion_page_matches_common_filters(page: Dict[str, Any], clauses: List[Dict[str, Any]]) -> bool:
    field_values = {
        "customer": page.get("customer") or page.get("search_text", ""),
        "platform": page.get("platform") or page.get("search_text", ""),
        "status": page.get("status", ""),
        "owner": page.get("owner", ""),
        "created_at": (
            page.get("registration_dates", [page.get("registration_date", "")])
            if page.get("registration_date_properties") or page.get("registration_date_property")
            else page.get("created_time", "")
        ),
        "due_at": page.get("due_date", ""),
        "deployed_at": page.get("deployment_date", ""),
        "title": page.get("title", ""),
        "keyword": "\n".join([page.get("search_text", ""), page.get("content", "")]),
        "database": page.get("database_label", ""),
    }
    for clause in normalize_filter_clauses(clauses):
        if not clause_supported("notion", clause) or clause["field"] not in field_values:
            continue
        if clause["field"] in {"created_at", "due_at", "deployed_at"}:
            if not matches_source_filters("notion", field_values, [clause]):
                return False
            continue
        candidate_raw = str(field_values[clause["field"]] or "")
        if clause["field"] == "owner" and candidate_raw.strip() in {"담당자 미지정", "미지정"}:
            candidate_raw = ""
        candidate = normalize_for_match(candidate_raw)
        values = [normalize_for_match(value) for value in values_for_clause(clause.get("value"))]
        operator = clause["operator"]
        # customer/platform/keyword are represented inside a property search
        # document, so equality is intentionally compiled as membership.
        document_field = clause["field"] in {"customer", "platform", "keyword"}
        owner_names = {normalize_for_match(name) for name in candidate_raw.split(",")}
        if operator in {"equals", "in"}:
            matched = any(
                value in owner_names if clause["field"] == "owner"
                else value in candidate if document_field else value == candidate for value in values
            )
            if not matched:
                return False
        elif operator in {"not_equals", "not_in"}:
            matched = any(
                value in owner_names if clause["field"] == "owner"
                else value in candidate if document_field else value == candidate for value in values
            )
            if matched:
                return False
        elif operator == "contains" and not any(value in candidate for value in values):
            return False
        elif operator == "not_contains" and any(value in candidate for value in values):
            return False
        elif operator == "exists" and not candidate:
            return False
        elif operator == "not_exists" and candidate:
            return False
        elif operator == "gte" and values and candidate_raw[:10] < str(clause.get("value"))[:10]:
            return False
        elif operator == "lte" and values and candidate_raw[:10] > str(clause.get("value"))[:10]:
            return False
        elif operator == "between" and len(values) == 2:
            date_value = candidate_raw[:10]
            raw_values = values_for_clause(clause.get("value"))
            if not date_value or not (raw_values[0] <= date_value <= raw_values[1]):
                return False
    return True


def notion_page_matches_constraints(page: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    if constraints.get("date_filter") and not matches_source_filters(
        "notion", {"due_at": page.get("due_date", ""), "deployed_at": page.get("deployment_date", "")},
        [constraints["date_filter"]],
    ):
        return False
    if constraints.get("database_label") and normalize_for_match(constraints["database_label"]) not in normalize_for_match(
        page.get("database_label", "")
    ):
        return False
    if constraints.get("exact_title") and normalize_for_match(constraints["exact_title"]) != normalize_for_match(
        page.get("title", "")
    ):
        return False
    requested_status = normalize_for_match(constraints.get("status", ""))
    if requested_status == "진행":
        requested_status = "진행중"
    if requested_status and requested_status != normalize_for_match(page.get("status", "")):
        return False
    if constraints.get("owner") and normalize_for_match(constraints["owner"]) not in {
        normalize_for_match(name) for name in str(page.get("owner", "")).split(",")
    }:
        return False
    if constraints.get("unassigned_owner") and str(page.get("owner", "")).strip() not in {
        "",
        "담당자 미지정",
        "미지정",
    }:
        return False
    if any(
        normalize_for_match(status) in {
            normalize_for_match(value)
            for value in [page.get("status", ""), *(page.get("status_fields") or {}).values()]
        }
        for status in constraints.get("excluded_statuses", [])
    ):
        return False
    if not notion_page_matches_common_filters(page, constraints.get("common_filter_clauses", [])):
        return False
    return True


def notion_page_matches_and_enrich(page: Dict[str, Any], constraints: Dict[str, Any]) -> bool:
    """Apply cheap constraints first, then lazily resolve relation-backed owners."""
    if constraints.get("status"):
        # 같은 페이지의 구상태/상태 중 검색 조건에 사용한 속성을 표시한다.
        # 필터는 시작전인데 답변에는 구상태=종료된이슈를 표시하는 일을 막는다.
        target = normalize_for_match(constraints["status"])
        for name, value in (page.get("status_fields") or {}).items():
            if normalize_for_match(value) == target:
                page["status"] = value
                page["status_property"] = name
                break
    without_owner = {
        **constraints, "owner": "", "unassigned_owner": False,
        "common_filter_clauses": [
            clause for clause in constraints.get("common_filter_clauses", [])
            if clause.get("field") != "owner"
        ],
    }
    if not notion_page_matches_constraints(page, without_owner):
        return False
    if (constraints.get("owner") or constraints.get("unassigned_owner")
            or constraints.get("group_by") == "owner"
            or any(clause.get("field") == "owner" for clause in constraints.get("common_filter_clauses", []))):
        resolve_notion_page_owner(page)
    return notion_page_matches_constraints(page, constraints)


def notion_exact_match_score(page: Dict[str, Any], constraints: Dict[str, Any]) -> int:
    score = 0
    if constraints.get("database_label"):
        score += 30
    if constraints.get("exact_title"):
        score += 60
    if constraints.get("status"):
        score += 25
    if constraints.get("owner"):
        score += 20
    return score


def notion_page_match_score(page: Dict[str, Any], terms: List[str]) -> int:
    if not terms:
        return 1
    weighted_fields = [
        (page.get("title", ""), 6),
        (page.get("type", ""), 4),
        (page.get("status", ""), 3),
        (page.get("owner", ""), 2),
        (page.get("search_text", ""), 1),
    ]
    score = 0
    for term in terms:
        normalized_term = normalize_for_match(term)
        for value, weight in weighted_fields:
            if normalized_term in normalize_for_match(value):
                score += weight
                break
    return score


def notion_minimum_match_score(terms: List[str]) -> int:
    if not terms:
        return 1
    if len(terms) <= 2:
        return 1
    return 2


def notion_page_matches_terms(page: Dict[str, Any], terms: List[str]) -> bool:
    if not terms:
        return True
    normalized_search_text = normalize_for_match(page.get("search_text", ""))
    return all(normalize_for_match(term) in normalized_search_text for term in terms)


def notion_search_max_pages(
    constraints: Dict[str, Any],
    *,
    server_filter_applied: bool = False,
    require_complete: bool = False,
) -> int:
    # Structured filters and grouped summaries must scan the complete configured
    # databases. 목록 조회는 서버 필터가 실제 적용된 뒤에만 상한을 낮춘다.
    # 스키마 조회 실패 등으로 필터가 적용되지 않았다면 기존 전체 스캔으로
    # 되돌아가 최근 수정 행만 보고 0건으로 단정하는 일을 막는다.
    has_structured_filter = bool(
        constraints.get("status")
        or constraints.get("excluded_statuses")
        or constraints.get("unassigned_owner")
        or constraints.get("owner")
        or constraints.get("group_by")
        or constraints.get("common_filter_clauses")
    )
    if require_complete:
        return 30
    if has_structured_filter:
        return 5 if server_filter_applied else 30
    return 3


def notion_clause_server_covered(
    clause: Dict[str, Any],
    server_covered_fields: set[str],
    *,
    target_database_label: str,
    exact_title: str,
) -> bool:
    """서버 필터가 로컬 후처리 조건을 빠짐없이 포함하는지 확인한다."""
    field = str(clause.get("field") or "")
    operator = str(clause.get("operator") or "")
    if field in {"status", "customer"}:
        return field in server_covered_fields and operator in {"equals", "in"}
    if field == "owner":
        return field in server_covered_fields and operator in {"equals", "in"}
    if field in {"created_at", "deployed_at"}:
        return field in server_covered_fields and operator in {"between", "equals", "gte", "lte"}
    if field == "database":
        return bool(target_database_label)
    if field == "title":
        return bool(exact_title)
    return False


# 노션 API는 and/or 복합 필터를 두 단계까지만 중첩할 수 있다.
NOTION_MAX_FILTER_LEVELS = 2


def notion_filter_levels(api_filter: Optional[Dict[str, Any]]) -> int:
    """and/or 복합 필터가 겹친 최대 단계 수를 센다."""
    if not isinstance(api_filter, dict):
        return 0
    for key in ("and", "or"):
        if key in api_filter:
            return 1 + max((notion_filter_levels(item) for item in api_filter[key]), default=0)
    return 0


def notion_filter_fits(api_filter: Optional[Dict[str, Any]]) -> bool:
    return notion_filter_levels(api_filter) <= NOTION_MAX_FILTER_LEVELS


def notion_and_filter(*filters: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """필터를 and로 묶되 안쪽 and는 펼친다. 의미는 같고 중첩 단계만 줄어든다."""
    parts: List[Dict[str, Any]] = []

    def add(api_filter: Optional[Dict[str, Any]]) -> None:
        if not api_filter:
            return
        if set(api_filter) == {"and"}:
            for item in api_filter["and"]:
                add(item)
        else:
            parts.append(api_filter)

    for api_filter in filters:
        add(api_filter)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"and": parts}


def notion_title_query_filter(
    title_filter: Dict[str, Any],
    condition_filter: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """제목 필터에 나머지 조건을 붙인다. 중첩 한도를 넘으면 제목 필터만 보낸다.

    서버가 돌려준 페이지는 모든 조건으로 다시 로컬 검사하므로 결과는 같고,
    조회량만 늘어난다.
    """
    combined = notion_and_filter(title_filter, condition_filter)
    return combined if combined is not None and notion_filter_fits(combined) else title_filter


def notion_database_status_filter(
    database_id: str,
    status: str,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Return whether a DB has status-like select fields and an exact API filter.

    When the requested status is absent from every status-like option list, the
    caller can safely skip that database. If schema inspection fails, return
    ``False`` so the existing local-scan fallback remains available.
    """
    if not status:
        return False, None
    try:
        properties = fetch_notion_database_schema(database_id).get("properties", {})
    except (HTTPException, requests.RequestException):
        return False, None
    target = normalize_for_match(status)
    filters: List[Dict[str, Any]] = []
    supported = False
    for prop_name, prop in properties.items():
        prop_type = prop.get("type")
        if prop_type not in {"status", "select"} or not any(
            key in normalize_for_match(prop_name) for key in ("상태", "status", "진행")
        ):
            continue
        supported = True
        for option in (prop.get(prop_type) or {}).get("options", []):
            option_name = str(option.get("name") or "")
            if normalize_for_match(option_name) == target:
                filters.append({"property": prop_name, prop_type: {"equals": option_name}})
    if not filters:
        return supported, None
    return True, filters[0] if len(filters) == 1 else {"or": filters}


def notion_database_customer_filter(
    database_id: str,
    clauses: List[Dict[str, Any]],
    aliases: List[str],
) -> Optional[Dict[str, Any]]:
    """고객사 속성이 확인된 DB에만 안전한 서버 필터를 만든다."""
    customer_clauses = [
        clause for clause in normalize_filter_clauses(clauses)
        if clause["field"] == "customer" and clause["operator"] in {"equals", "in"}
    ]
    if not customer_clauses:
        return None
    properties = notion_property_schema(database_id)
    fields = role_properties(
        properties,
        CUSTOMER_NAMES,
        {"select", "multi_select"},
    )
    if not fields:
        return None

    clause_filters: List[Dict[str, Any]] = []
    for clause in customer_clauses:
        requested = list(dict.fromkeys([
            *values_for_clause(clause.get("value")),
            *[str(alias) for alias in aliases if str(alias).strip()],
        ]))
        normalized_requested = [normalize_for_match(value) for value in requested]
        filters: List[Dict[str, Any]] = []
        for name in fields:
            prop_type = str(properties[name].get("type") or "")
            if prop_type in {"select", "multi_select"}:
                options = (properties[name].get(prop_type) or {}).get("options", [])
                for option in options:
                    option_name = str(option.get("name") or "")
                    normalized_option = normalize_for_match(option_name)
                    if not option_name or not any(
                        value in normalized_option or normalized_option in value
                        for value in normalized_requested if value
                    ):
                        continue
                    operator = "equals" if prop_type == "select" else "contains"
                    filters.append({"property": name, prop_type: {operator: option_name}})
        if not filters:
            return None
        clause_filters.append(filters[0] if len(filters) == 1 else {"or": filters})
    return clause_filters[0] if len(clause_filters) == 1 else {"and": clause_filters}


def notion_database_owner_filter(
    database_id: str,
    owner: str,
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """담당자 people 필드가 있으면 사용자 ID로 서버 필터를 만든다.

    첫 반환값이 참이고 필터가 없으면 해당 DB에는 관련자 필드가 없어
    안전하게 건너뛸 수 있다. relation 기반 담당자는 로컬 해석을 유지한다.
    """
    if not owner:
        return False, None
    properties = notion_property_schema(database_id)
    fields = responsible_properties(properties)
    if not fields:
        # 빈 스키마는 조회 실패/테스트 대역일 수 있으므로 로컬 검사로
        # 폴백한다. 실제 속성이 있는데 관련자 필드가 없는 DB만 건너뛴다.
        return bool(properties), None
    people_fields = [name for name in fields if properties[name].get("type") == "people"]
    if not people_fields or len(people_fields) != len(fields):
        # 다른 담당자 속성에만 배정된 항목을 people 필터가 먼저 제외하지 않게 한다.
        return False, None
    normalized_owner = normalize_for_match(owner)
    user_id = _NOTION_USER_ID_HINTS.get(normalized_owner, "") or next(
        (
            user_id
            for user_name, user_id in fetch_notion_users()
            if normalize_for_match(user_name) == normalized_owner
        ),
        "",
    )
    if not user_id:
        # users.list는 게스트/제한 멤버를 빠뜨릴 수 있지만, 페이지의 people
        # 값에는 이름과 ID가 함께 들어온다. 최근 페이지 한 번만 읽어 ID를
        # 찾아 두면 이후 모든 DB에 정확한 서버 필터를 적용할 수 있다.
        response = notion_post_json(
            f"{NOTION_API_BASE}/databases/{database_id}/query",
            {
                "page_size": 100,
                "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
            },
            timeout=20,
            retries=1,
        )
        if response.status_code < 400:
            for raw_page in response.json().get("results", []):
                raw_properties = raw_page.get("properties") or {}
                for name in people_fields:
                    for person in (raw_properties.get(name) or {}).get("people") or []:
                        person_name = str(person.get("name") or "").strip()
                        person_id = str(person.get("id") or "").strip()
                        if (
                            person_id
                            and normalize_for_match(person_name) == normalized_owner
                        ):
                            user_id = person_id
                            break
                    if user_id:
                        break
                if user_id:
                    break
    if not user_id:
        return False, None
    _NOTION_USER_ID_HINTS[normalized_owner] = user_id
    filters = [
        {"property": name, "people": {"contains": user_id}}
        for name in people_fields
    ]
    return True, filters[0] if len(filters) == 1 else {"or": filters}


def notion_database_date_filter(database_id: str, clause: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """정확한 날짜 속성으로 먼저 좁히고, 우선순위는 행 후처리에서 검사한다."""
    properties = notion_property_schema(database_id)
    names = DUE_NAMES if clause["field"] == "due_at" else DEPLOYMENT_NAMES
    fields = role_properties(properties, names, {"date"})
    values = values_for_clause(clause.get("value"))
    if not fields:
        return None
    filters: List[Dict[str, Any]]
    if clause["field"] == "due_at":
        # Notion의 기간 필터는 시작일을 볼 수 있으므로 마감(끝 날짜)의
        # 하한을 서버에 강제하지 않는다. 최종 비교는 end 기준 로컬 검사다.
        bound = values[1] if clause["operator"] == "between" else values[0]
        condition = {"is_not_empty": True} if clause["operator"] == "gte" else {"on_or_before": bound}
        filters = [{"property": name, "date": condition} for name in fields]
    elif clause["operator"] == "between":
        filters = [{"and": [
            {"property": name, "date": {"on_or_after": values[0]}},
            {"property": name, "date": {"on_or_before": values[1]}},
        ]} for name in fields]
    else:
        operators = {"equals": "equals", "gte": "on_or_after", "lte": "on_or_before"}
        operator = operators.get(clause["operator"])
        if not operator:
            return None
        filters = [{"property": name, "date": {operator: values[0]}} for name in fields]
    return filters[0] if len(filters) == 1 else {"or": filters}


def notion_database_created_sort(database_id: str) -> tuple[Dict[str, str], bool]:
    fields = role_properties(notion_property_schema(database_id), REGISTRATION_NAMES, {"date"})
    if fields:
        # 빈 등록일은 생성 시각으로 대체하므로 한 속성의 API 정렬만으로
        # 전체 상위 N건을 확정할 수 없다. 이 경우 기존 전체 후보 검사를 유지한다.
        return {"property": fields[0], "direction": "descending"}, False
    return {"timestamp": "created_time", "direction": "descending"}, True


def notion_database_created_filter(
    database_id: str, clause: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, str], bool]:
    """등록일 속성을 우선 사용하고 없으면 페이지 생성 시각으로 좁힌다."""
    properties = notion_property_schema(database_id)
    fields = role_properties(properties, REGISTRATION_NAMES, {"date"})
    values = values_for_clause(clause.get("value"))
    operator = clause.get("operator")

    def range_condition(start: str, end: str) -> Dict[str, str]:
        exclusive_end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        return {"on_or_after": start, "before": exclusive_end}

    if operator == "between" and len(values) == 2:
        condition = range_condition(values[0], values[1])
    elif operator == "equals" and values:
        condition = range_condition(values[0], values[0])
    elif operator == "gte" and values:
        condition = {"on_or_after": values[0]}
    elif operator == "lte" and values:
        condition = {"before": (date.fromisoformat(values[0]) + timedelta(days=1)).isoformat()}
    else:
        raise ValueError("unsupported created_at filter")

    if fields:
        filters = [
            {"property": name, "date": dict(condition)}
            for name in fields
        ]
        api_filter = filters[0] if len(filters) == 1 else {"or": filters}
        return api_filter, {"property": fields[0], "direction": "descending"}, len(fields) == 1
    timestamp_condition = {
        key: f"{value}T00:00:00+09:00"
        for key, value in condition.items()
    }
    return (
        {"timestamp": "created_time", "created_time": timestamp_condition},
        {"timestamp": "created_time", "direction": "descending"},
        True,
    )


def notion_page_sort_key(page: Dict[str, Any], *, created_at_filtered: bool) -> tuple[str, float, int]:
    """날짜와 전체 ISO 시각을 분리해 모든 DB에서 같은 타입으로 정렬한다."""
    if created_at_filtered:
        calendar_date = str(page.get("registration_date") or page.get("created_time") or "")[:10]
        raw_time = str(page.get("registration_time_raw") or page.get("created_time_raw") or page.get("created_time") or "")
    else:
        calendar_date = str(page.get("last_edited_time") or "")[:10]
        raw_time = str(page.get("last_edited_time_raw") or page.get("last_edited_time") or "")
    try:
        parsed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            local_registration = created_at_filtered and page.get("registration_time_raw")
            parsed = parsed.replace(tzinfo=timezone(timedelta(hours=9)) if local_registration else timezone.utc)
        timestamp = parsed.timestamp()
    except (ValueError, OverflowError):
        timestamp = 0.0
    return calendar_date, timestamp, int(page.get("match_score") or 0)


@lru_cache(maxsize=8)
def _title_index_cached(path: str, mtime_ns: int, size: int) -> Dict[str, tuple[str, ...]]:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {
        str(title): tuple(
            str(item.get("parent_id") or "").replace("-", "")
            for item in matches
            if item.get("parent_id")
        )
        for title, matches in (data.get("titles") or {}).items()
        if isinstance(matches, list)
    }


def indexed_exact_title_database_ids(exact_title: str, database_ids: List[str]) -> List[str]:
    path = RUNTIME_DIR / "notion_title_index.json"
    try:
        stat = path.stat()
    except OSError:
        return []
    try:
        max_age = float(os.getenv("NOTION_TITLE_INDEX_TTL_SECONDS", "86400"))
    except (TypeError, ValueError):
        max_age = 86400.0
    if max_age >= 0 and time.time() - stat.st_mtime > max_age:
        return []
    index = _title_index_cached(str(path), stat.st_mtime_ns, stat.st_size)
    allowed = {identifier.replace("-", ""): identifier for identifier in database_ids}
    return list(dict.fromkeys(
        allowed[parent_id]
        for parent_id in index.get(normalize_for_match(exact_title), ())
        if parent_id in allowed
    ))


def indexed_title_term_database_ids(terms: List[str], database_ids: List[str]) -> List[str]:
    """완성된 최신 제목 인덱스에서 모든 검색어가 제목에 있는 DB를 찾는다."""
    normalized_terms = [normalize_for_match(term) for term in terms if normalize_for_match(term)]
    if not normalized_terms:
        return []
    path = RUNTIME_DIR / "notion_title_index.json"
    try:
        stat = path.stat()
        data = json.loads(path.read_text(encoding="utf-8"))
        max_age = float(os.getenv("NOTION_TITLE_INDEX_TTL_SECONDS", "86400"))
    except (OSError, TypeError, ValueError):
        return []
    if data.get("complete") is not True or (max_age >= 0 and time.time() - stat.st_mtime > max_age):
        return []
    allowed = {identifier.replace("-", ""): identifier for identifier in database_ids}
    matched: List[str] = []
    for normalized_title, rows in (data.get("titles") or {}).items():
        if not all(term in normalize_for_match(str(normalized_title)) for term in normalized_terms):
            continue
        for row in rows if isinstance(rows, list) else []:
            parent_id = str(row.get("parent_id") or "").replace("-", "")
            if parent_id in allowed:
                matched.append(allowed[parent_id])
    return list(dict.fromkeys(matched))


@dataclass
class _NotionDatabaseScan:
    """DB 하나를 조회한 결과. 공유 상태는 호출자가 DB 순서대로 병합한다."""

    database_id: str
    meta: Optional[Dict[str, Any]] = None
    # ("match", page)는 결과, ("candidate", page)는 본문 검색 후보다.
    events: List[Tuple[str, Dict[str, Any]]] = field(default_factory=list)
    errors: List[Dict[str, Any]] = field(default_factory=list)
    scanned_count: int = 0
    incomplete_reasons: set[str] = field(default_factory=set)
    search_incomplete: bool = False
    title_incomplete: bool = False
    candidate_count_incomplete: bool = False
    result_window_complete: bool = False


def notion_search_max_workers() -> int:
    """DB를 동시에 조회할 수. 전역 요청 간격 제한은 그대로 적용된다."""
    try:
        return max(1, min(int(os.getenv("NOTION_SEARCH_MAX_WORKERS", "4")), 6))
    except ValueError:
        return 4


def _indexed_body_matches(
    indexed: Dict[str, Any], constraints: Dict[str, Any], pre_body_constraints: Dict[str, Any],
    body_clauses: List[Dict[str, Any]], keyword_clauses: List[Dict[str, Any]],
    terms: List[str], read_scope: Dict[str, str],
) -> List[Dict[str, Any]]:
    """현재 원문을 읽은 색인 후보에도 기존 속성·유형·소유 문서 조건을 적용한다."""
    matched_pages = []
    labels: Dict[str, str] = {}
    remaining_constraints = {
        **constraints,
        "common_filter_clauses": [
            clause for clause in constraints["common_filter_clauses"]
            if clause not in normalize_filter_clauses(body_clauses)
        ],
    }
    for candidate in indexed["candidates"]:
        database_id = str(candidate["database_id"])
        try:
            if database_id not in labels:
                label = fetch_notion_database_title(database_id)
                root_id = read_scope.get(database_id)
                if root_id and root_id != database_id:
                    label = f"{fetch_notion_database_title(root_id)} / {label}"
                labels[database_id] = label
            page = normalize_notion_page(candidate["raw_page"], include_blocks=False,
                                         database_id=database_id, database_label=labels[database_id])
            if "snapshot_property_text" in candidate:
                page["search_text"] = candidate["snapshot_property_text"]
            if not notion_page_matches_and_enrich(page, pre_body_constraints):
                continue
        except (HTTPException, requests.RequestException):
            indexed["incomplete_reasons"].append("body_index_metadata_error")
            continue
        page_id = str(page.get("id") or "").replace("-", "")
        segments = [block for block in candidate["segments"]
                    if segment_belongs_to_page(block, page_id)]
        # 명시적 본문 조건에는 속성/자식 문서의 텍스트를 섞지 않는다.
        body_text = "\n".join(str(block["text"]) for block in segments)
        page["content"] = "\n\n".join(part for part in (page.get("properties_text", ""), body_text) if part)
        if not candidate.get("complete"):
            page["content_incomplete"] = True
            if any(clause["operator"] in {"not_contains", "not_equals", "not_in", "not_exists"}
                   for clause in keyword_clauses):
                continue
        search_text = f"{page.get('search_text', '')}\n{body_text}"
        if not all(normalize_for_match(term) in normalize_for_match(search_text) for term in terms):
            continue
        evidence = []
        for clause in body_clauses:
            matching = [block for block in segments
                        if (not clause["block_type"] or block["type"] == clause["block_type"])
                        and normalize_for_match(str(clause["value"])) in normalize_for_match(str(block["text"]))]
            if bool(matching) != (clause["operator"] == "contains"):
                break
            evidence.extend(matching)
        else:
            if not notion_page_matches_constraints(page, remaining_constraints):
                continue
            if not body_clauses:
                evidence = [block for block in segments if any(
                    normalize_for_match(term) in normalize_for_match(str(block["text"])) for term in terms
                )]
                page["index_title_match"] = bool(terms) and all(
                    normalize_for_match(term) in normalize_for_match(page.get("title", "")) for term in terms
                )
            page["matched_body_blocks"] = list({block["block_id"]: block for block in evidence}.values())[:10]
            page["match_method"] = "notion_body_index"
            page["matched_terms"] = terms
            page["match_score"] = notion_page_match_score(page, terms) - 5
            matched_pages.append(page)
    return deduplicate_notion_pages_by_id(matched_pages)


def execute_notion_search(
    query_text: str,
    limit: int = 5,
    constraints: Optional[Dict[str, Any]] = None,
    planned_terms: Optional[List[str]] = None,
    include_blocks: bool = True,
) -> Dict[str, Any]:
    settings = get_notion_settings()
    database_ids = settings.get("database_ids", [])
    if not settings["token"] or not database_ids:
        # 키 오류·장애로 DB 목록을 못 받은 것은 '설정 없음'이 아니라 조회 오류다.
        discovery_failed = bool(settings.get("database_discovery_error"))
        return {
            "source": "notion",
            "status": "error" if discovery_failed else "skipped",
            "error_kind": (
                "dependency" if settings.get("database_discovery_error_kind") == "dependency" else "internal"
            ) if discovery_failed else None,
            "reason": notion_unavailable_reason(settings),
            "notion_pages": [],
            "meta": {"displayed_count": 0},
        }

    read_scope, scope_complete = resolve_notion_read_scope(database_ids, settings["token"])
    database_ids = list(read_scope)
    notion_constraints = merge_common_notion_constraints(
        extract_notion_constraints(query_text),
        constraints,
    )
    _body_subject, body_clauses = split_notion_body_query(query_text)
    if body_clauses:
        # 원문으로 확인된 본문 조건은 플래너가 만든 제목/상태 조건보다 우선한다.
        # 리터럴을 제거한 문장에서 다른 명시적 조건은 그대로 추출한다.
        grounded_constraints = grounded_notion_body_constraints(query_text, constraints or {})
        notion_constraints = merge_common_notion_constraints(extract_notion_constraints(query_text), grounded_constraints)
    notion_constraints["body_filter_clauses"] = body_clauses
    structural_query = notion_structural_query(query_text)
    temporal_field = notion_temporal_field(structural_query)
    if temporal_field != "created_at":
        date_text = structural_query
        date_range = extract_created_at_range(date_text) or extract_yearless_temporal_month_range(
            date_text, temporal_field,
        )
        notion_constraints["common_filter_clauses"] = [
            clause for clause in notion_constraints["common_filter_clauses"] if clause["field"] != "created_at"
        ]
        if date_range:
            clause = {"field": temporal_field, "operator": "between", "value": [date_range["from"], date_range["to"]]}
            if clause not in notion_constraints["common_filter_clauses"]:
                notion_constraints["common_filter_clauses"].append(clause)
    keyword_clauses = [
        clause for clause in notion_constraints["common_filter_clauses"]
        if clause["field"] == "keyword" and clause_supported("notion", clause)
    ]
    pre_body_constraints = {
        **notion_constraints,
        "common_filter_clauses": [
            clause for clause in notion_constraints["common_filter_clauses"]
            if clause["field"] != "keyword"
        ],
    }
    exact_title = notion_constraints.get("exact_title", "")
    subject_text, consumed_keyword_terms = notion_keyword_subject_text(query_text, notion_constraints)
    terms = list(dict.fromkeys(
        build_notion_terms(query_text, notion_constraints) if body_clauses
        else planned_terms or build_notion_terms(subject_text, notion_constraints)
    ))[:8]
    terms = [
        term for term in terms
        if normalize_for_match(normalize_search_token(term)) not in consumed_keyword_terms
        or normalize_for_match(normalize_search_token(term)) in normalize_for_match(subject_text)
    ]
    terms = ground_notion_terms(query_text, terms, notion_constraints)
    if not exact_title:
        terms = remove_structured_date_terms(terms, notion_constraints)
    database_target = normalize_for_match(notion_constraints.get("database_label", ""))
    if database_target and not exact_title:
        terms = remove_notion_database_label_terms(
            terms,
            str(notion_constraints.get("database_label") or ""),
        )
    if notion_constraints.get("owner"):
        terms = [term for term in terms if normalize_for_match(term) not in {"기여자", "공헌자", "contributors", "contributor", "공동", "작업자"}]
        # 담당 조건으로 소비한 '김지은이 맡은'의 서술어가 본문 검색어로
        # 남으면 담당자가 맞는 문서도 '맡은'이라는 단어가 없어 탈락한다.
        owner_predicates = {
            normalize_search_token(match.group(1))
            for match in re.finditer(
                rf"(?<![가-힣A-Za-z]){re.escape(notion_constraints['owner'])}(?:이|가|은|는)?"
                rf"\s+({_OWNER_PREDICATE})(?=\s|$)",
                remove_quoted_value(query_text) if exact_title else query_text,
            )
        }
        terms = [term for term in terms if normalize_search_token(term) not in owner_predicates]
    # Status/owner are structured filters. Leaving them in free-text terms makes
    # every field participate in an accidental AND query and hides valid rows.
    structured_values = {
        normalize_for_match(value)
        for value in (notion_constraints.get("status"), notion_constraints.get("owner"))
        if value
    }
    structured_values.update(
        normalize_for_match(value) for value in notion_constraints.get("excluded_statuses", [])
    )
    structured_values.update(
        normalize_for_match(value)
        for clause in notion_constraints.get("common_filter_clauses", [])
        for value in values_for_clause(clause.get("value"))
        if clause.get("field") not in {"title", "keyword"}
    )
    structured_fields = {
        str(clause.get("field") or "")
        for clause in notion_constraints.get("common_filter_clauses", [])
    }
    structured_field_terms: set[str] = set()
    if "customer" in structured_fields:
        structured_field_terms.update({"고객사", "고객", "customer", "client"})
    if "platform" in structured_fields:
        structured_field_terms.update({"매체", "플랫폼", "media", "platform"})
    from backend.intent.identifiers import COMPOUND_IDENTIFIER

    terms = [
        term
        for term in terms
        if normalize_for_match(term) not in structured_field_terms
        and not any(
            normalize_for_match(term) == value
            or (not COMPOUND_IDENTIFIER.fullmatch(term) and (
                normalize_for_match(term) in value or value in normalize_for_match(term)
            ))
            for value in structured_values
        )
    ]
    title_terms = [
        term
        for term in terms
        if normalize_for_match(term)
        not in {normalize_for_match(status) for status in NOTION_STATUS_QUERY_TERMS}
    ]
    scoped_database_ids = list(database_ids)
    database_ids, selected_database_role = select_notion_database_ids(
        database_ids, query_text, terms, notion_constraints,
    )
    created_at_filtered = any(
        clause.get("field") == "created_at"
        for clause in notion_constraints.get("common_filter_clauses", [])
    )
    created_order = created_at_filtered or (notion_constraints.get("sort") or {}).get("field") == "created_at"
    metadata_list = bool(
        created_order and not include_blocks and not terms and not exact_title and not body_clauses and not keyword_clauses
        and not has_count_intent(query_text) and not notion_constraints.get("group_by")
        and not any(notion_constraints.get(key) for key in ("status", "owner", "unassigned_owner", "excluded_statuses"))
        and all(clause.get("field") == "created_at" for clause in notion_constraints.get("common_filter_clauses", []))
    )
    if metadata_list:
        notion_constraints["metadata_list"] = True
    if scope_complete:
        from backend.notion.complete_snapshot import search_complete_snapshot
        snapshot_result = search_complete_snapshot(
            database_ids, terms=terms, read_scope=read_scope, constraints=notion_constraints,
            pre_body_constraints=pre_body_constraints, body_clauses=body_clauses,
            keyword_clauses=keyword_clauses, limit=limit, include_blocks=include_blocks)
        if snapshot_result is not None:
            snapshot_result["meta"]["selected_database_role"] = selected_database_role
            return snapshot_result
    target_database_label = notion_constraints.get("database_label", "")
    pages: List[Dict[str, Any]] = []
    scanned_count = 0
    database_meta = []
    errors = []
    search_complete = scope_complete
    incomplete_reasons: set[str] = set() if scope_complete else {"child_database_discovery"}
    seen_page_ids: set[str] = set()
    content_incomplete_page_ids: set[str] = set()
    require_complete_scan = bool(
        has_count_intent(query_text) or notion_constraints.get("group_by")
    )
    max_pages = notion_search_max_pages(
        notion_constraints,
        require_complete=require_complete_scan,
    )
    created_list_window = bool(
        created_at_filtered
        and not terms
        and not exact_title
        and not has_count_intent(query_text)
        and not notion_constraints.get("group_by")
        and not notion_constraints.get("status")
        and not notion_constraints.get("owner")
        and not notion_constraints.get("unassigned_owner")
        and not notion_constraints.get("excluded_statuses")
        and all(
            clause.get("field") == "created_at"
            for clause in notion_constraints.get("common_filter_clauses", [])
        )
    )
    page_size = 100
    candidate_count_complete = True
    result_window_complete = False
    body_scan_candidate_groups: List[Tuple[str, List[Dict[str, Any]]]] = []
    body_scan_candidates_truncated = False
    body_scan_attempted_count = 0
    body_index_meta: Dict[str, Any] = {}
    body_index_used = False

    indexed_exact_database_ids = indexed_exact_title_database_ids(exact_title, database_ids) if exact_title else []
    indexed_term_database_ids = (
        indexed_title_term_database_ids(title_terms, database_ids)
        if title_terms and not exact_title
        else []
    )
    indexed_database_ids = list(dict.fromkeys([
        *indexed_exact_database_ids,
        *indexed_term_database_ids,
    ]))
    indexed_ids = set(indexed_database_ids)
    search_database_ids = [
        *indexed_database_ids,
        *(database_id for database_id in database_ids if database_id not in indexed_ids),
    ]

    # 제목 검색은 모든 DB에서 먼저 끝낸다. 인덱스는 조회 순서에만
    # 사용하므로 인덱스 작성 뒤 다른 DB에 생긴 문서도 놓치지 않는다.
    # 본문 키워드 조건은 제목이나 속성 일치만으로 확정할 수 없다.
    title_first = bool(title_terms and not exact_title and not keyword_clauses)
    title_search_complete = True
    title_search_only = False
    try:
        positive_body_terms = [str(clause["value"]) for clause in body_clauses if clause["operator"] == "contains"]
        index_terms = positive_body_terms or (terms if not body_clauses and not exact_title else [])
        if index_terms and os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() not in {"0", "false", "no"}:
            indexed = find_indexed_body_candidates(database_ids, list(dict.fromkeys([*index_terms, *terms])),
                                                   body_clauses=body_clauses, exact_title=exact_title,
                                                   allowed_database_ids=scoped_database_ids)
            if indexed is not None:
                body_index_meta = {**indexed["coverage"], "candidate_count": indexed["candidate_count"],
                                   "validation_complete": indexed["validation_complete"]}
                pages = _indexed_body_matches(indexed, notion_constraints, pre_body_constraints,
                                              body_clauses, keyword_clauses, terms, read_scope)
                body_index_used = bool(pages)
                # 현재 원문으로 확인된 결과라도 후보 발견은 저장본 시점 기준이다.
                # 신규/변경 페이지의 미색인 키워드를 실시간 전체 건수로 확정하지 않는다.
                if body_index_used or indexed["incomplete_reasons"]:
                    search_complete = False
                    candidate_count_complete = False
                    incomplete_reasons.update(indexed["incomplete_reasons"])
                if body_index_used:
                    incomplete_reasons.add("body_index_snapshot")
                    if not indexed["coverage"].get("complete"):
                        incomplete_reasons.add("body_index_incomplete")
                # 색인 결과가 없으면 아래 실시간 열거의 완전성을 독립적으로
                # 판단한다. 완전히 읽은 음성 대조군까지 미확인으로 낮추지 않는다.
                seen_page_ids.update(str(page["id"]) for page in pages)

        def scan_database(
            database_id: str,
            title_pass: bool,
            seen_before: frozenset[str],
        ) -> _NotionDatabaseScan:
            """DB 하나를 조회한다. 공유 상태는 건드리지 않고 결과에 담아 돌려준다."""
            scan = _NotionDatabaseScan(database_id)
            try:
                database_label = fetch_notion_database_title(database_id)
                root_id = read_scope.get(database_id)
                if root_id and root_id != database_id:
                    database_label = f"{fetch_notion_database_title(root_id)} / {database_label}"
            except (HTTPException, requests.RequestException) as exc:
                # 다른 DB에서 확보한 결과를 한 DB의 스키마 장애 때문에
                # 전부 버리지 않는다. 실패한 범위는 부분 오류로 남긴다.
                scan.title_incomplete = True
                scan.errors.append({
                    "database_id": database_id,
                    "database_label": database_id,
                    "error_kind": "dependency",
                    "reason": f"Notion database schema failed: {exc}",
                })
                return scan
            if target_database_label and normalize_for_match(target_database_label) not in normalize_for_match(database_label):
                return scan
            created_filter_applied = False
            created_window_safe = False
            server_covered_fields: set[str] = set()
            status_filter_supported, status_api_filter = notion_database_status_filter(
                database_id,
                notion_constraints.get("status", ""),
            )
            if notion_constraints.get("status") and status_filter_supported and not status_api_filter:
                scan.meta = (
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "found_count": 0,
                    }
                )
                return scan
            if status_api_filter:
                server_covered_fields.add("status")
            common_clauses = notion_constraints.get("common_filter_clauses", [])
            date_clauses = [clause for clause in common_clauses
                            if clause["field"] in {"created_at", "due_at", "deployed_at"}
                            and clause_supported("notion", clause)
                            and clause["operator"] in {"between", "equals", "gte", "lte"}]
            database_sort: Dict[str, str] = {
                "timestamp": "last_edited_time", "direction": "descending",
            }
            registration_window_safe = False
            if created_order and not date_clauses:
                database_sort, registration_window_safe = notion_database_created_sort(database_id)
            if date_clauses:
                try:
                    date_filters: List[Optional[Dict[str, Any]]] = []
                    for clause in date_clauses:
                        if clause["field"] == "created_at":
                            created_filter, database_sort, created_window_safe = notion_database_created_filter(
                                database_id, clause,
                            )
                            date_filters.append(created_filter)
                            created_filter_applied = True
                            server_covered_fields.add("created_at")
                        else:
                            date_filters.append(notion_database_date_filter(database_id, clause))
                            if clause["field"] == "deployed_at":
                                server_covered_fields.add("deployed_at")
                    if any(date_filter is None for date_filter in date_filters):
                        scan.meta = {"database_id": database_id, "database_label": database_label, "found_count": 0}
                        return scan
                    combined_filter = notion_and_filter(status_api_filter, *date_filters)
                    if notion_filter_fits(combined_filter):
                        status_api_filter = combined_filter
                    else:
                        # 중첩 한도를 넘는 날짜 조건은 서버에 보내지 않고 로컬에서만 검사한다.
                        created_filter_applied = False
                        created_window_safe = False
                        server_covered_fields.difference_update({"created_at", "deployed_at"})
                except (HTTPException, requests.RequestException, ValueError):
                    # 스키마 실패 시 조건을 버리지 않고 기존 로컬 필터로 검사한다.
                    server_covered_fields.difference_update({"created_at", "deployed_at"})
            try:
                customer_api_filter = notion_database_customer_filter(
                    database_id,
                    common_clauses,
                    notion_constraints.get("customer_aliases", []),
                )
            except (HTTPException, requests.RequestException):
                customer_api_filter = None
            if customer_api_filter:
                combined_filter = notion_and_filter(status_api_filter, customer_api_filter)
                # 중첩 한도를 넘으면 고객사 조건은 로컬에서만 검사한다.
                if notion_filter_fits(combined_filter):
                    server_covered_fields.add("customer")
                    status_api_filter = combined_filter
            owner_filter_decisive = False
            owner_api_filter: Optional[Dict[str, Any]] = None
            if notion_constraints.get("owner"):
                try:
                    owner_filter_decisive, owner_api_filter = notion_database_owner_filter(
                        database_id,
                        str(notion_constraints["owner"]),
                    )
                except (HTTPException, requests.RequestException):
                    owner_filter_decisive = False
                    owner_api_filter = None
                if owner_filter_decisive and not owner_api_filter:
                    scan.meta = {
                        "database_id": database_id,
                        "database_label": database_label,
                        "found_count": 0,
                    }
                    return scan
                if owner_api_filter:
                    combined_filter = notion_and_filter(status_api_filter, owner_api_filter)
                    # 중첩 한도를 넘으면 담당자 조건은 로컬에서만 검사한다.
                    if notion_filter_fits(combined_filter):
                        server_covered_fields.add("owner")
                        status_api_filter = combined_filter

            server_filter_complete = bool(status_api_filter) and all(
                notion_clause_server_covered(
                    clause,
                    server_covered_fields,
                    target_database_label=target_database_label,
                    exact_title=exact_title,
                )
                for clause in common_clauses
            ) and not any((
                notion_constraints.get("unassigned_owner"),
                notion_constraints.get("excluded_statuses"),
            ))
            if notion_constraints.get("owner") and "owner" not in server_covered_fields:
                server_filter_complete = False
            if notion_constraints.get("status") and "status" not in server_covered_fields:
                server_filter_complete = False
            database_max_pages = notion_search_max_pages(
                notion_constraints,
                server_filter_applied=server_filter_complete,
                require_complete=require_complete_scan,
            )
            server_filtered_list_window = bool(
                server_filter_complete
                and not terms
                and not exact_title
                and not require_complete_scan
                and ("created_at" not in server_covered_fields or created_window_safe)
            )
            server_filtered_list_window = server_filtered_list_window or bool(metadata_list and registration_window_safe)
            try:
                title_property = (
                    notion_title_property(notion_property_schema(database_id))
                    if title_pass or not title_first else ""
                )
            except requests.Timeout:
                # 스키마 GET이 지연돼도 키워드 검색 자체는 DB query와 로컬
                # 매칭으로 계속할 수 있다. 다른 DB의 제목 필터까지 끄면
                # 전체 스캔 요청이 급증하므로 실패한 DB만 폴백한다.
                title_property = ""
            except (HTTPException, requests.RequestException):
                title_property = ""
            if exact_title and not title_property:
                scan.errors.append(
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "error_kind": "internal",
                        "reason": "Notion title property could not be resolved",
                    }
                )
                scan.meta = (
                    {
                        "database_id": database_id,
                        "database_label": database_label,
                        "found_count": 0,
                    }
                )
                return scan
            if exact_title and title_property:
                title_filter: Dict[str, Any] = {
                    "property": title_property, "title": {"equals": exact_title},
                }
                exact_payload: Dict[str, Any] = {
                    "page_size": page_size,
                    # 상태는 아래의 공통 필터에서 적용한다. 후속 상태 변경 질문도
                    # 동일한 제목 후보를 재사용하면서 현재 조건으로 다시 검증한다.
                    "filter": title_filter,
                    "sorts": [database_sort],
                }
                exact_max_pages = notion_search_max_pages(
                    notion_constraints,
                    server_filter_applied=True,
                    require_complete=require_complete_scan,
                )
                for _ in range(exact_max_pages):
                    try:
                        exact_response = cached_title_query(
                            f"{NOTION_API_BASE}/databases/{database_id}/query",
                            exact_payload,
                            notion_post_json,
                            timeout=45,
                            retries=2,
                        )
                        exact_response.raise_for_status()
                    except requests.RequestException as exc:
                        response_status = getattr(exc.response, "status_code", None)
                        scan.errors.append({
                            "database_id": database_id,
                            "database_label": database_label,
                            "error_kind": "internal" if response_status and 400 <= response_status < 500
                            and response_status != 429 else "dependency",
                            "reason": f"Notion exact-title query failed: {exc}",
                        })
                        break
                    exact_data = exact_response.json()
                    scan.meta = scan.meta or {}
                    response_headers = getattr(exact_response, "headers", {})
                    if not isinstance(response_headers, Mapping):
                        response_headers = {}
                    scan.meta["query_cache_age_seconds"] = max(
                        float(scan.meta.get("query_cache_age_seconds", 0)),
                        float(response_headers.get("X-Assistance-Cache-Age", "0")),
                    )
                    scan.meta["query_cache_hit"] = scan.meta.get("query_cache_hit", False) or (
                        "X-Assistance-Cache-Age" in response_headers
                    )
                    exact_results = exact_data.get("results", [])
                    scan.scanned_count += len(exact_results)
                    for raw_page in exact_results:
                        page = normalize_notion_page(
                            raw_page,
                            include_blocks=False,
                            database_id=database_id,
                            database_label=database_label,
                        )
                        page_id = page.get("id", "")
                        if page_id in seen_before:
                            continue
                        if not notion_page_matches_and_enrich(page, pre_body_constraints):
                            continue
                        page["match_score"] = (
                            notion_page_match_score(page, terms)
                            + notion_exact_match_score(page, notion_constraints)
                            + 60
                        )
                        page["matched_terms"] = terms
                        page["match_method"] = "notion_exact_title_filter"
                        scan.events.append(("candidate" if keyword_clauses else "match", page))
                    if not exact_data.get("has_more"):
                        break
                    next_cursor = exact_data.get("next_cursor")
                    if not next_cursor or next_cursor == exact_payload.get("start_cursor"):
                        scan.search_incomplete = True
                        scan.incomplete_reasons.add("invalid_cursor")
                        break
                    exact_payload["start_cursor"] = next_cursor
                else:
                    scan.search_incomplete = True
                    scan.incomplete_reasons.add("page_limit")
                # Exact-title requests are backed by Notion's server-side title
                # filter. Do not fall through to a multi-thousand-row database
                # scan when a literal title is absent or one database is slow.
                scan.meta = (
                    {
                        **(scan.meta or {}),
                        "database_id": database_id,
                        "database_label": database_label,
                        "found_count": 0,
                        "page_limit": exact_max_pages,
                    }
                )
                return scan
            if title_pass and title_property:
                try:
                    title_filter = {
                        "and": [
                            {"property": title_property, "title": {"contains": term}}
                            for term in title_terms
                        ]
                    }
                    targeted_response = notion_post_json(
                        f"{NOTION_API_BASE}/databases/{database_id}/query",
                        {
                            "page_size": page_size,
                            "filter": notion_title_query_filter(title_filter, status_api_filter),
                            "sorts": [database_sort],
                        },
                        timeout=45,
                        retries=2,
                    )
                    if targeted_response.status_code < 400:
                        targeted_data = targeted_response.json()
                        if targeted_data.get("has_more"):
                            scan.title_incomplete = True
                            scan.search_incomplete = True
                            scan.candidate_count_incomplete = True
                            scan.incomplete_reasons.add("title_page_limit")
                        targeted_results = targeted_data.get("results", [])
                        scan.scanned_count += len(targeted_results)
                        for raw_page in targeted_results:
                            page = normalize_notion_page(
                                raw_page,
                                include_blocks=False,
                                database_id=database_id,
                                database_label=database_label,
                            )
                            page_id = page.get("id", "")
                            if page_id in seen_before:
                                continue
                            if not notion_page_matches_and_enrich(page, notion_constraints):
                                continue
                            page["match_score"] = (
                                notion_page_match_score(page, terms)
                                + notion_exact_match_score(page, notion_constraints)
                                + 20
                            )
                            page["matched_terms"] = terms
                            page["match_method"] = "notion_title_filter"
                            scan.events.append(("match", page))
                    else:
                        scan.title_incomplete = True
                except (HTTPException, requests.RequestException) as exc:
                    scan.title_incomplete = True
                    scan.errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "error_kind": "dependency" if isinstance(exc, requests.RequestException) else "internal",
                            "reason": f"Notion targeted-title query failed: {exc}",
                        }
                    )
            if title_pass:
                if not title_property:
                    scan.title_incomplete = True
                scan.meta = {
                    "database_id": database_id,
                    "database_label": database_label,
                    "found_count": 0,
                    "phase": "title",
                }
                return scan
            cursor = None
            for _ in range(database_max_pages):
                payload: Dict[str, Any] = {
                    "page_size": (
                        max(1, min(100, limit))
                        if (
                            server_filtered_list_window
                            or (created_list_window and created_filter_applied and created_window_safe)
                        )
                        else page_size
                    ),
                    "sorts": [database_sort],
                }
                if status_api_filter:
                    payload["filter"] = status_api_filter
                if cursor:
                    payload["start_cursor"] = cursor

                try:
                    response = notion_post_json(
                        f"{NOTION_API_BASE}/databases/{database_id}/query",
                        payload,
                        timeout=45,
                        retries=2,
                    )
                except requests.RequestException as exc:
                    scan.errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "error_kind": "dependency",
                            "reason": f"Notion database scan failed: {exc}",
                        }
                    )
                    break
                if response.status_code >= 400:
                    scan.errors.append(
                        {
                            "database_id": database_id,
                            "database_label": database_label,
                            "error_kind": (
                                "dependency"
                                if response.status_code == 429 or response.status_code >= 500
                                else "internal"
                            ),
                            "reason": f"{response.status_code} {response.text[:300]}",
                        }
                    )
                    break

                data = response.json()
                results = data.get("results", [])
                scan.scanned_count += len(results)
                for raw_page in results:
                    page = normalize_notion_page(
                        raw_page,
                        include_blocks=False,
                        database_id=database_id,
                        database_label=database_label,
                    )
                    page_id = page.get("id", "")
                    if page_id in seen_before:
                        continue
                    if not notion_page_matches_and_enrich(page, pre_body_constraints):
                        continue
                    if keyword_clauses:
                        # 포함 조건의 본문 후보와 제외 조건의 숨은 일치를 모두 확인한다.
                        scan.events.append(("candidate", page))
                        continue
                    match_score = notion_page_match_score(page, terms)
                    if notion_page_matches_terms(page, terms):
                        page["match_score"] = match_score + notion_exact_match_score(page, notion_constraints)
                        page["matched_terms"] = [
                            term
                            for term in terms
                            if normalize_for_match(term)
                            in normalize_for_match(page.get("search_text", ""))
                        ]
                        scan.events.append(("match", page))
                    elif terms:
                        scan.events.append(("candidate", page))
                if (body_index_used and keyword_clauses and data.get("has_more")
                        and sum(kind == "candidate" for kind, _page in scan.events) >= NOTION_BODY_SCAN_MAX_PER_REQUEST):
                    # 병합 단계도 DB당 앞 30개 본문 후보만 사용한다. 같은 후보
                    # 창을 확보한 뒤 더 읽는 속성 페이지는 재현율을 늘리지 못한다.
                    scan.search_incomplete = True
                    scan.candidate_count_incomplete = True
                    scan.incomplete_reasons.add("body_candidate_window")
                    break
                if server_filtered_list_window or (
                    created_list_window and created_filter_applied and created_window_safe
                ):
                    # 서버가 모든 조건과 내림차순 정렬을 적용했으므로
                    # 목록에 필요한 상위 N건은 첫 응답만으로 확정된다.
                    scan.result_window_complete = True
                    if data.get("has_more"):
                        scan.candidate_count_incomplete = True
                    break
                if not data.get("has_more"):
                    break
                next_cursor = data.get("next_cursor")
                if not next_cursor or next_cursor == cursor:
                    scan.search_incomplete = True
                    scan.incomplete_reasons.add("invalid_cursor")
                    break
                cursor = next_cursor
            else:
                scan.search_incomplete = True
                scan.incomplete_reasons.add("page_limit")

            scan.meta = (
                {
                    "database_id": database_id,
                    "database_label": database_label,
                    "found_count": 0,
                    "page_limit": database_max_pages,
                }
            )
            return scan

        def merge_scan(scan: _NotionDatabaseScan) -> None:
            """DB 결과를 원래 조회 순서대로 반영한다. 중복 제거도 여기서 한다."""
            nonlocal scanned_count, search_complete, title_search_complete
            nonlocal candidate_count_complete, result_window_complete, body_scan_candidates_truncated
            scanned_count += scan.scanned_count
            errors.extend(scan.errors)
            incomplete_reasons.update(scan.incomplete_reasons)
            if scan.search_incomplete:
                search_complete = False
            if scan.title_incomplete:
                title_search_complete = False
            if scan.candidate_count_incomplete:
                candidate_count_complete = False
            if scan.result_window_complete:
                result_window_complete = True
            found_count = 0
            body_scan_candidates: List[Dict[str, Any]] = []
            for kind, page in scan.events:
                page_id = page.get("id", "")
                if page_id in seen_page_ids:
                    continue
                if kind == "match":
                    seen_page_ids.add(page_id)
                    pages.append(page)
                    found_count += 1
                elif len(body_scan_candidates) < NOTION_BODY_SCAN_MAX_PER_REQUEST:
                    body_scan_candidates.append(page)
                else:
                    body_scan_candidates_truncated = True
            if (terms or keyword_clauses) and (require_complete_scan or found_count < limit) and body_scan_candidates:
                body_scan_candidate_groups.append((scan.database_id, body_scan_candidates))
            if scan.meta is not None:
                # found_count는 중복 제거 뒤의 값이라 병합 단계에서 채운다.
                scan.meta["found_count"] = found_count
                database_meta.append(scan.meta)

        def title_results_suffice() -> bool:
            # 제목 검색이 누락 없이 끝났고 결과가 있으면 전체 검색을 이어가지 않는다.
            return bool(title_search_complete and pages and not require_complete_scan and not body_index_used)

        # DB 조회는 단계 안에서만 미리 실행하고, 병합과 종료 판단은 원래 DB 순서를 따른다.
        workers = notion_search_max_workers()
        for title_pass in ((True, False) if title_first else (False,)):
            full_pass_after_title = title_first and not title_pass
            if full_pass_after_title and title_results_suffice():
                title_search_only = True
                break
            scans = iter_in_order(
                partial(scan_database, title_pass=title_pass, seen_before=frozenset(seen_page_ids)),
                search_database_ids,
                workers=workers,
            )
            with closing(scans) as ordered_scans:
                for _database_id, scan in ordered_scans:
                    if full_pass_after_title and title_results_suffice():
                        title_search_only = True
                        break
                    merge_scan(scan)
        if title_search_only:
            result_window_complete = True
            candidate_count_complete = False

        # DB 순서가 결과를 좌우하지 않도록 후보를 라운드로빈으로 읽되,
        # 실제 본문 요청은 DB 수와 관계없이 요청 전체 30회까지만 허용한다.
        body_match_counts: Dict[str, int] = {}
        for indexed_page in pages:
            if indexed_page.get("match_method") == "notion_body_index":
                indexed_db = str(indexed_page.get("database_id") or "")
                body_match_counts[indexed_db] = body_match_counts.get(indexed_db, 0) + 1
        max_candidates = max(
            (len(candidates) for _, candidates in body_scan_candidate_groups),
            default=0,
        )
        body_scan_candidate_count = sum(
            len(candidates) for _, candidates in body_scan_candidate_groups
        )
        body_scan_budget_exhausted = False
        # 색인 hit가 있어도 새 미색인 문서를 찾아 합친다. 검증된 오래된 결과가
        # limit를 채웠다는 이유로 최근 원본 후보 확인을 생략하지 않는다.
        require_body_scan = require_complete_scan or body_index_used
        body_scan_deadline = time.monotonic() + 30 if body_index_used else float("inf")
        active_budget = CURRENT_BUDGET.get()
        if body_index_used and active_budget is not None:
            body_scan_deadline = min(body_scan_deadline, active_budget.deadline - 5)
        for candidate_index in range(max_candidates):
            for database_id, candidates in body_scan_candidate_groups:
                if candidate_index >= len(candidates):
                    continue
                if not require_body_scan and len(pages) >= limit:
                    break
                if time.monotonic() >= body_scan_deadline:
                    body_scan_budget_exhausted = True
                    incomplete_reasons.add("body_index_live_scan_limit")
                    break
                if body_scan_attempted_count >= NOTION_BODY_SCAN_MAX_PER_REQUEST:
                    body_scan_budget_exhausted = True
                    break
                page = candidates[candidate_index]
                page_id = page.get("id", "")
                if page_id in seen_page_ids:
                    continue
                body_scan_attempted_count += 1
                # 정확한 제목으로 한정된 후보는 결과 본문과 같은 범위까지
                # 확인해야 앞의 20개 블록 뒤에 있는 표·코드도 찾을 수 있다.
                if body_clauses:
                    from backend.notion.client import fetch_notion_page_content

                    body = fetch_notion_page_content(page_id, max_blocks=NOTION_RESULT_BODY_MAX_BLOCKS if exact_title else 20)
                    block_text = str(body["text"])
                    own_segments = [
                        segment for segment in body["segments"]
                        if str(segment.get("owner_page_id") or "").replace("-", "") == str(page_id).replace("-", "")
                    ]
                else:
                    block_text = fetch_notion_page_blocks(
                        page_id,
                        max_blocks=NOTION_RESULT_BODY_MAX_BLOCKS if exact_title else 20,
                    )
                    own_segments = []
                if os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() in {"0", "false", "no"}:
                    search_complete = False
                    candidate_count_complete = False
                    incomplete_reasons.add("body_disabled")
                    page["content_incomplete"] = True
                if NOTION_CONTENT_INCOMPLETE in block_text:
                    search_complete = False
                    candidate_count_complete = False
                    incomplete_reasons.add("body_limit")
                    page["content_incomplete"] = True
                if not block_text and not keyword_clauses:
                    continue
                body_search_text = (
                    f"{page.get('search_text', '')}\n"
                    f"{block_text.replace(NOTION_CONTENT_INCOMPLETE, '')}"
                )
                if not all(
                    normalize_for_match(term) in normalize_for_match(body_search_text)
                    for term in terms
                ):
                    continue
                page["content"] = "\n\n".join(
                    part for part in (page.get("properties_text", ""), block_text) if part
                ).strip()
                if keyword_clauses:
                    # 읽지 못한 본문 뒤에 제외 키워드가 있을 수 있으므로 부정 조건을 확정하지 않는다.
                    if page.get("content_incomplete") and any(
                        clause["operator"] in {"not_contains", "not_equals", "not_in", "not_exists"}
                        for clause in keyword_clauses
                    ):
                        continue
                    body_matched = True
                    matched_blocks = []
                    for clause in body_clauses:
                        candidates = [segment for segment in own_segments
                                      if not clause["block_type"] or segment["type"] == clause["block_type"]]
                        matching = [segment for segment in candidates
                                    if normalize_for_match(str(clause["value"])) in normalize_for_match(str(segment["text"]))]
                        if bool(matching) != (clause["operator"] == "contains"):
                            body_matched = False
                            break
                        if matching:
                            matched_blocks.extend(matching)
                    if not body_matched:
                        continue
                    remaining_constraints = {
                        **notion_constraints,
                        "common_filter_clauses": [
                            clause for clause in notion_constraints["common_filter_clauses"]
                            if clause not in normalize_filter_clauses(body_clauses)
                        ],
                    }
                    if not notion_page_matches_constraints(page, remaining_constraints):
                        continue
                    if matched_blocks:
                        page["matched_body_blocks"] = list({block["block_id"]: block for block in matched_blocks}.values())[:10]
                seen_page_ids.add(page_id)
                page["match_score"] = notion_page_match_score(page, terms) - 5
                page["matched_terms"] = [
                    term
                    for term in terms
                    if normalize_for_match(term) in normalize_for_match(body_search_text)
                ]
                page["match_method"] = "notion_body_scan"
                pages.append(page)
                body_match_counts[database_id] = body_match_counts.get(database_id, 0) + 1
            if body_scan_budget_exhausted or (
                not require_body_scan and len(pages) >= limit
            ):
                break
        body_scan_stopped_at_limit = bool(
            not require_body_scan
            and len(pages) >= limit
            and body_scan_attempted_count < body_scan_candidate_count
        )
        body_scan_result_incomplete = bool(
            body_scan_budget_exhausted
            or (
                body_scan_candidates_truncated
                and (require_body_scan or len(pages) < limit)
            )
        )
        if body_scan_result_incomplete:
            search_complete = False
            candidate_count_complete = False
            incomplete_reasons.add("body_scan_limit")
        elif body_scan_candidates_truncated or body_scan_stopped_at_limit:
            # 목록에 필요한 결과 창은 확보했지만, 나머지 본문 후보를 읽지
            # 않았으므로 전체 후보 건수까지 확정됐다고 표시하지 않는다.
            candidate_count_complete = False
            result_window_complete = True
        for meta in database_meta:
            found_count = meta.get("found_count")
            meta["found_count"] = (found_count if isinstance(found_count, int) else 0) + body_match_counts.get(
                str(meta.get("database_id") or ""), 0,
            )
    except requests.RequestException as exc:
        return {
            "source": "notion",
            "status": "error",
            "error_kind": "dependency",
            "reason": f"Notion connection failed: {exc}",
            "notion_pages": [],
            "meta": {"displayed_count": 0},
        }

    pages.sort(
        key=lambda page: (
            bool(terms) and (page.get("match_method") == "notion_title_filter" or page.get("index_title_match", False)),
            notion_page_sort_key(page, created_at_filtered=created_order),
        ),
        reverse=True,
    )
    deduplicated_pages = deduplicate_notion_pages_by_id(pages)
    limited_pages = deduplicated_pages[:limit]
    content_requested = include_blocks and os.getenv(
        "NOTION_FETCH_PAGE_BLOCKS", "true"
    ).lower() not in {"0", "false", "no"}
    if content_requested:
        for page in limited_pages:
            if page.get("content_incomplete"):
                content_incomplete_page_ids.add(str(page.get("id") or ""))
            if page.get("match_method") == "notion_body_index":
                # 색인 후보는 이미 일반 결과(200블록)보다 큰 범위를 실시간
                # 검증했다. 더 작은 한도로 다시 읽어 뒤쪽의 근거를 덮어쓰지 않는다.
                continue
            # 짧은 후보 스캔이 온전한 본문을 읽었으면 재사용하고, 일부만
            # 읽은 일치 문서는 모델 답변에 필요한 결과 본문 한도로 보강한다.
            if (
                page.get("match_method") == "notion_body_scan"
                and page.get("content")
                and not page.get("content_incomplete")
            ):
                continue
            block_text = fetch_notion_page_blocks(
                page.get("id", ""),
                max_blocks=NOTION_RESULT_BODY_MAX_BLOCKS,
            )
            if NOTION_CONTENT_INCOMPLETE in block_text:
                page["content_incomplete"] = True
                content_incomplete_page_ids.add(str(page.get("id") or ""))
            elif block_text:
                page.pop("content_incomplete", None)
                content_incomplete_page_ids.discard(str(page.get("id") or ""))
            if block_text:
                page["content"] = "\n\n".join(
                    part for part in (page.get("properties_text", ""), block_text) if part
                ).strip()
    status = "partial_error" if errors and limited_pages else "error" if errors else "ok"
    database_page_limits = [
        value
        for meta in database_meta
        if isinstance((value := meta.get("page_limit")), int)
    ]

    return {
        "source": "notion",
        "status": status,
        "error_kind": (
            "dependency"
            if errors and all(error.get("error_kind") == "dependency" for error in errors)
            else "internal" if errors else None
        ),
        "reason": "; ".join(
            f"{error['database_label']}: {error['reason']}" for error in errors
        )[:500]
        if errors
        else None,
        "meta": {
            "displayed_count": len(limited_pages),
            "candidate_count": len(deduplicated_pages),
            "candidate_count_complete": candidate_count_complete,
            "result_window_complete": result_window_complete,
            "scanned_count": scanned_count,
            "search_complete": search_complete and not errors,
            "incomplete_reasons": sorted(incomplete_reasons | ({"query_error"} if errors else set())),
            "content_complete": (
                not content_incomplete_page_ids if content_requested else None
            ),
            "content_incomplete_page_ids": sorted(content_incomplete_page_ids),
            "content_incomplete_count": len(content_incomplete_page_ids),
            "scan_limit_per_database": max(database_page_limits or [max_pages]) * page_size,
            "body_scan_attempted_count": body_scan_attempted_count,
            "body_scan_limit": NOTION_BODY_SCAN_MAX_PER_REQUEST,
            "body_index": body_index_meta,
            "body_index_used": body_index_used,
            "terms": terms[:6],
            "constraints": notion_constraints,
            "database_ids": database_ids,
            "scoped_database_ids": scoped_database_ids,
            "selected_database_role": selected_database_role,
            "databases": database_meta,
            "errors": errors,
            "exact_title_index_hit": bool(indexed_exact_database_ids),
            "title_term_index_hit": bool(indexed_term_database_ids),
            "title_search_only": title_search_only,
            "queriedAt": datetime.now(timezone.utc).isoformat(),
            "query_cache_hit": any(row.get("query_cache_hit") for row in database_meta),
            "query_cache_age_seconds": max((row.get("query_cache_age_seconds", 0) for row in database_meta), default=0),
        },
        "notion_pages": limited_pages,
    }


def deduplicate_notion_pages_by_id(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    deduplicated_pages: List[Dict[str, Any]] = []
    seen_result_page_ids = set()
    for page in pages:
        page_id = str(page.get("id") or "")
        if not page_id or page_id in seen_result_page_ids:
            continue
        seen_result_page_ids.add(page_id)
        deduplicated_pages.append(page)
    return deduplicated_pages


def collect_retrieved_notion_pages(retrieval_result: Dict[str, Any], limit: int) -> List[Dict[str, Any]]:
    pages = []
    seen_page_ids = set()
    for stage in retrieval_result.get("stages", []):
        for page in stage.get("notion_pages", []):
            page_id = page.get("id")
            if not page_id or page_id in seen_page_ids:
                continue
            seen_page_ids.add(page_id)
            pages.append(page)
            if len(pages) >= limit:
                return pages
    return pages


__all__ = [
    "correct_notion_status_typo",
    "build_notion_terms",
    "trim_owner_adnominal",
    "inherit_notion_context",
    "extract_notion_database_label_from_text",
    "extract_notion_constraints",
    "merge_common_notion_constraints",
    "notion_page_matches_common_filters",
    "notion_page_matches_constraints",
    "notion_page_matches_and_enrich",
    "notion_exact_match_score",
    "notion_page_match_score",
    "notion_minimum_match_score",
    "notion_page_matches_terms",
    "notion_search_max_pages",
    "notion_database_status_filter",
    "execute_notion_search",
    "deduplicate_notion_pages_by_id",
    "collect_retrieved_notion_pages",
]
