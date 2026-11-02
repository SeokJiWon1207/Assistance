"""Notion 제약 추출, 검색, 필터링과 랭킹을 담당한다."""

from backend.notion.matching import (
    notion_page_matches_common_filters as notion_page_matches_common_filters,
    notion_page_matches_constraints as notion_page_matches_constraints,
    notion_exact_match_score as notion_exact_match_score,
    notion_page_match_score as notion_page_match_score,
    notion_minimum_match_score as notion_minimum_match_score,
    notion_page_matches_terms as notion_page_matches_terms,
    notion_page_sort_key as notion_page_sort_key,
    deduplicate_notion_pages_by_id as deduplicate_notion_pages_by_id,
    collect_retrieved_notion_pages as collect_retrieved_notion_pages,
)
from backend.notion.server_filters import (
    _NOTION_USER_ID_HINTS as _NOTION_USER_ID_HINTS,
    notion_search_max_pages as notion_search_max_pages,
    notion_clause_server_covered as notion_clause_server_covered,
    NOTION_MAX_FILTER_LEVELS as NOTION_MAX_FILTER_LEVELS,
    notion_filter_levels as notion_filter_levels,
    notion_filter_fits as notion_filter_fits,
    notion_and_filter as notion_and_filter,
    notion_title_query_filter as notion_title_query_filter,
    notion_database_status_filter as notion_database_status_filter,
    notion_database_customer_filter as notion_database_customer_filter,
    notion_customer_filter_needs_postcheck as notion_customer_filter_needs_postcheck,
    notion_database_owner_filter as notion_database_owner_filter,
    notion_database_date_filter as notion_database_date_filter,
    notion_database_created_sort as notion_database_created_sort,
    notion_database_created_filter as notion_database_created_filter,
)
from backend.notion.page_enrichment import (
    notion_page_matches_and_enrich as notion_page_matches_and_enrich,
)
from backend.notion.body_index_search import (
    _indexed_body_matches as _indexed_body_matches,
)
from backend.notion import read_api
from backend.notion import schema as notion_schema
from backend.notion import transport
import os

from backend.notion.page_ownership import segment_belongs_to_page as segment_belongs_to_page
import json
import re
import time
from datetime import date as date, datetime as datetime, timedelta as timedelta, timezone as timezone
from contextlib import closing
from dataclasses import dataclass, field
from functools import lru_cache, partial
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

import requests
from fastapi import HTTPException

from backend.utils.text import (
    LOOKUP_REQUEST_PATTERN,
    normalize_for_match,
    normalize_search_token,
    remove_quoted_value,
)
from backend.domain.people import is_valid_person_name as is_valid_person_name, strip_korean_particle as strip_korean_particle
from backend.settings.notion import NOTION_API_BASE, notion_unavailable_reason
from backend.config import RUNTIME_DIR
from backend.notion.discovery import resolve_notion_settings
from backend.notion.transport import notion_post_json as notion_post_json
from backend.settings.values import STOPWORDS
from backend.intent.constants import NOTION_QUERY_STOPWORDS
from backend.intent.notion_constraints import (
    extract_notion_constraints as extract_notion_constraints,
    trim_owner_adnominal as trim_owner_adnominal,
    known_notion_owner as known_notion_owner,
)
from backend.query.notion_constraints import (
    extract_notion_database_label_from_text as extract_notion_database_label_from_text,
    _known_notion_owner_matcher as _known_notion_owner_matcher,
    _IMPLICIT_OWNER_NAME as _IMPLICIT_OWNER_NAME,
    _STATUS_BOUND_NOUN as _STATUS_BOUND_NOUN,
    _STATUS_SPOKEN_SUFFIX as _STATUS_SPOKEN_SUFFIX,
    _STATUS_WORD_END as _STATUS_WORD_END,
)
from backend.query.notion_constraints import _OWNER_SUFFIXES, _OWNER_PREDICATE
from backend.execution import CURRENT_BUDGET
from backend.ordered_prefetch import iter_in_order
from backend.sources.people_catalog import load_known_people as load_known_people
from backend.notion.read_api import NOTION_CONTENT_INCOMPLETE as NOTION_CONTENT_INCOMPLETE, fetch_notion_database_schema as fetch_notion_database_schema, fetch_notion_database_title as fetch_notion_database_title, fetch_notion_page_blocks as fetch_notion_page_blocks, fetch_notion_users as fetch_notion_users
from backend.notion.constants import (
    NOTION_BODY_SCAN_MAX_PER_REQUEST,
    NOTION_IN_ENDING_NOUNS,
    NOTION_RESULT_BODY_MAX_BLOCKS,
    NOTION_STATUS_QUERY_TERMS,
    NOTION_STATUS_TYPOS,
)
from backend.notion.mapping import normalize_notion_page as normalize_notion_page, resolve_notion_page_owner as resolve_notion_page_owner
from backend.notion.read_scope import resolve_notion_read_scope
from backend.intent.notion_grounding import grounded_notion_body_constraints
from backend.query.notion_text import notion_literal_title, notion_registration_sort, notion_structural_query, split_notion_body_query, without_registration_order
from backend.notion.query_cache import cached_title_query
from backend.notion.body_index_search import find_indexed_body_candidates
from backend.notion.database_roles import select_notion_database_ids
from backend.notion.property_roles import CUSTOMER_NAMES as CUSTOMER_NAMES, DEPLOYMENT_NAMES as DEPLOYMENT_NAMES, DUE_NAMES as DUE_NAMES, REGISTRATION_NAMES as REGISTRATION_NAMES, responsible_properties as responsible_properties, role_properties as role_properties
from backend.notion.schema import notion_property_schema as notion_property_schema, notion_title_property as notion_title_property
from backend.query_filters import clause_supported as clause_supported, matches_source_filters as matches_source_filters, normalize_filter_clauses as normalize_filter_clauses, values_for_clause as values_for_clause, notion_temporal_field as notion_temporal_field
from backend.query.operations import GROUP_BY_PATTERNS as GROUP_BY_PATTERNS, extract_group_by as extract_group_by, has_count_intent as has_count_intent
from backend.query.dates import extract_created_at_range, extract_yearless_temporal_month_range


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
_DATE_FIELD_TERMS = {
    "created_at": {"등록일", "접수일", "생성일", "작성일"},
    "due_at": {"마감", "기한", "마감일"},
    "deployed_at": {"운영반영", "배포일"},
}


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


# Mantis 명단에는 svn·signalmaker·DRFN_SM 같은 연동 계정도 섞여 있다.
# '담당자' 표지가 없는 표현에서는 제품명과 구별되는 한글 이름과
# 대문자로 시작하는 영문 이름만 사람으로 본다.


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


# 노션 API는 and/or 복합 필터를 두 단계까지만 중첩할 수 있다.


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
    # 이 색인은 정답/완전성 판정에 사용하지 않고 현재 허용 범위의 조회
    # 순서만 정한다. 오래된 후보도 원문을 다시 읽으므로 버릴 필요가 없다.
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


def execute_notion_search(
    query_text: str,
    limit: int = 5,
    constraints: Optional[Dict[str, Any]] = None,
    planned_terms: Optional[List[str]] = None,
    include_blocks: bool = True,
) -> Dict[str, Any]:
    settings = resolve_notion_settings()
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
    native_constraints = extract_notion_constraints(query_text)
    notion_constraints = merge_common_notion_constraints(
        native_constraints,
        constraints,
    )
    _body_subject, body_clauses = split_notion_body_query(query_text)
    if body_clauses:
        # 원문으로 확인된 본문 조건은 플래너가 만든 제목/상태 조건보다 우선한다.
        # 리터럴을 제거한 문장에서 다른 명시적 조건은 그대로 추출한다.
        grounded_constraints = grounded_notion_body_constraints(query_text, constraints or {})
        notion_constraints = merge_common_notion_constraints(native_constraints, grounded_constraints)
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
                database_label = read_api.fetch_notion_database_title(database_id)
                root_id = read_scope.get(database_id)
                if root_id and root_id != database_id:
                    database_label = f"{read_api.fetch_notion_database_title(root_id)} / {database_label}"
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
            customer_candidate_filter_applied = False
            if customer_api_filter:
                combined_filter = notion_and_filter(status_api_filter, customer_api_filter)
                # 중첩 한도를 넘으면 고객사 조건은 로컬에서만 검사한다.
                if notion_filter_fits(combined_filter):
                    customer_candidate_filter_applied = notion_customer_filter_needs_postcheck(customer_api_filter)
                    if not customer_candidate_filter_applied:
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
            customer_candidate_window = bool(
                any(clause['field'] == 'customer' and clause['operator'] in {'equals', 'in'}
                    for clause in common_clauses) and not terms and not exact_title
                and not require_complete_scan and not keyword_clauses
                and ("created_at" not in server_covered_fields or created_window_safe)
                and not notion_constraints.get('unassigned_owner')
                and not notion_constraints.get('excluded_statuses')
                and all(notion_clause_server_covered(clause, server_covered_fields | {'customer'},
                                                     target_database_label=target_database_label, exact_title=exact_title)
                        for clause in common_clauses))
            try:
                title_property = (
                    notion_schema.notion_title_property(notion_schema.notion_property_schema(database_id))
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
                            transport.notion_post_json,
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
                    targeted_response = transport.notion_post_json(
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
                    response = transport.notion_post_json(
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
                ) or (
                    customer_candidate_window and sum(kind == 'match' for kind, _page in scan.events) >= limit
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
                scan.candidate_count_incomplete = True
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
        from backend.notion.search_budget import bounded_live_search
        with bounded_live_search((body_index_used or bool(exact_title))
                                 and not require_complete_scan) as live_search:
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
                        from backend.notion.read_api import fetch_notion_page_content as fetch_notion_page_content

                        body = read_api.fetch_notion_page_content(page_id, max_blocks=NOTION_RESULT_BODY_MAX_BLOCKS if exact_title else 20)
                        block_text = str(body["text"])
                        own_segments = [
                            segment for segment in body["segments"]
                            if str(segment.get("owner_page_id") or "").replace("-", "") == str(page_id).replace("-", "")
                        ]
                    else:
                        block_text = read_api.fetch_notion_page_blocks(
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
        if live_search['limited']:
            search_complete = False
            candidate_count_complete = False
            result_window_complete = False
            incomplete_reasons.add('live_search_time_limit')
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
            block_text = read_api.fetch_notion_page_blocks(
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
