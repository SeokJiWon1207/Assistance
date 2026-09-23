"""Mantis 검색어, 유사 이슈 질의와 결과 표시 개수를 해석한다."""

import re
from typing import Any, Dict, List, Optional

from backend.common import LOOKUP_REQUEST_PATTERN, extract_quoted_value, is_control_search_term, normalize_for_match, normalize_search_token
from backend.intent.constants import TRIAGE_REQUEST_PATTERN
from backend.config import (
    KOREAN_COUNT_WORDS,
    STATUS_ALIASES,
    STATUS_MATCH_ALIASES,
    STOPWORDS,
)
from backend.intent.entities import extract_mantis_keyword


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
        *(
            ("등록", "등록된", "접수", "접수된", "생성", "생성된", "올라온", "들어온")
            if constraints.get("created_at")
            else ()
        ),
        *(
            (
                "오늘", "금일", "어제", "전일", "그제", "그저께", "엊그제",
                "이번주", "금주", "지난주", "저번주", "전주",
                "이번달", "이달", "금월", "당월", "지난달", "저번달", "전달", "전월",
                "올해", "금년", "작년", "전년", "지난해", "지난년도", "이번해", "이번년도",
            )
            if constraints.get("created_at")
            else ()
        ),
    ]
    normalized_term = normalize_for_match(term)
    if "open" in constraints.get("statuses", []) and normalized_term in {
        "남은", "남아있는", "잔여", "안끝난", "미종료",
    }:
        return True
    return any(
        normalized_term == normalize_for_match(alias)
        or normalized_term in normalize_for_match(alias)
        or normalize_for_match(alias) in normalized_term
        for alias in aliases
        if alias
    )


def build_keyword_terms(text: str, constraints: Optional[Dict[str, Any]] = None) -> List[str]:
    if TRIAGE_REQUEST_PATTERN.search(text) and (symptom := extract_quoted_value(text)):
        text = symptom
    text = re.sub(r"(?<![가-힣])(?:표|목록|리스트|요약)(?:로|으로)\s*(?=보여|정리|만들|작성|알려)", " ", text)
    text = LOOKUP_REQUEST_PATTERN.sub(" ", text)
    terms = [normalize_search_token(term) for term in extract_mantis_keyword(text).split() if term]
    personal_scope_text = bool(
        re.search(
            r"(?:^|[\s,])(?:내|나의|제|나|저|내가|제가|내게|나에게|저에게)(?:\s|[가-힣])",
            str(text or ""),
        )
    )
    compact_personal_term = re.compile(
        r"(?:내|나의|제)(?:(?:최근|미종료|장기|열린|진행중|처리중|종료된|완료된))*"
        r"(?:이슈|업무)(?:를|을|가|은|는|도|만|의)?"
    )
    filter_control_terms = {
        "중", "제목", "본문", "내용", "키워드", "빼고", "제외", "제외하고",
        "말고", "아닌", "아니고", "없는", "없이", "있지만", "포함",
        "고객사별", "고객사마다", "프로젝트별", "프로젝트마다",
        "플랫폼별", "플랫폼마다", "매체별", "매체마다", "매체구분별", "매체구분마다",
        "하나씩", "한개씩", "한건씩", "1개씩", "1건씩",
        "방금", "아까", "해당", "위", "조건",
        "아니", "그대로", "그대", "두고", "유지", "유지하고", "바꿔", "바꿔서", "변경",
        "고객사는", "프로젝트는", "플랫폼은", "매체는", "상태만", "담당자만",
        "업무보고서", "업무보고서를", "주간보고서", "주간보고서를",
        "보고서", "보고서를", "작성", "작성해", "작성해줘", "작성해주세요",
        "요약해줘", "요약해주세요", "정리해줘", "정리해주세요", "비교해줘", "비교해주세요",
        "저번주", "지난주", "이번주", "전주", "금주", "저번", "주",
        "내", "나", "저", "나의", "제", "내가", "제가", "내게", "나에게", "저에게",
        "맡은", "담당한", "할당된", "고객사", "보여주세요", "알려주세요",
    }
    personal_control_terms = {"장기"} if personal_scope_text else set()
    terms = [
        term
        for term in terms
        if term
        and term.lower() not in STOPWORDS
        and term not in STOPWORDS
        and term not in filter_control_terms
        and term not in personal_control_terms
        and not compact_personal_term.fullmatch(term)
        and not is_control_search_term(term)
    ]
    if constraints:
        terms = [term for term in terms if not term_is_covered_by_constraints(term, constraints)]
    return terms[:4]


def wants_similar_issue_search(text: str, constraints: Optional[Dict[str, Any]] = None) -> bool:
    if constraints is None:
        from backend.intent.constraints import extract_query_constraints

        constraints = extract_query_constraints(text)
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


# 표시 개수는 "5개"처럼 숫자로도, "하나만"처럼 한글 수사로도 들어온다. 단위
# 뒤의 '월'을 배제해 "최근 3개월"의 3개를 개수로 읽지 않고, 묶음마다 하나를
# 뜻하는 '하나씩'은 표시 개수가 아니므로 제외한다.
_COUNT_UNIT = r"(?:개|건)(?!월)"
_COUNT_WORDS = "|".join(sorted(KOREAN_COUNT_WORDS, key=len, reverse=True))
_NUMERIC_LIMIT_PATTERN = re.compile(rf"(\d+)\s*{_COUNT_UNIT}\s*만?")
_KOREAN_LIMIT_PATTERN = re.compile(
    rf"(?<![가-힣])({_COUNT_WORDS})\s*{_COUNT_UNIT}\s*만?(?!\s*씩)"
)
# "이슈 하나만"처럼 단위 없이 쓰는 표현은 대상이 앞에 있을 때만 개수로 읽는다.
# 그래야 "하나증권"이나 "한국투자증권"의 앞글자를 개수로 오해하지 않는다.
_BARE_KOREAN_LIMIT_PATTERN = re.compile(
    rf"(?:이슈|건|항목|결과|문서|페이지)\s*(?:을|를)?\s*({_COUNT_WORDS})\s*(?:만|를|을|$)"
)


def extract_requested_limit(text: str) -> Optional[int]:
    numeric_match = _NUMERIC_LIMIT_PATTERN.search(text)
    if numeric_match:
        return max(1, min(int(numeric_match.group(1)), 20))
    for pattern in (_KOREAN_LIMIT_PATTERN, _BARE_KOREAN_LIMIT_PATTERN):
        match = pattern.search(text)
        if match:
            return max(1, min(KOREAN_COUNT_WORDS[match.group(1)], 20))
    return None


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
        return 200
    return sidebar_limit or default


def extract_limit(text: str, default: int = 5) -> int:
    return resolve_result_limit(text, None, default)


__all__ = [
    "term_is_covered_by_constraints",
    "build_keyword_terms",
    "wants_similar_issue_search",
    "split_project_search_terms",
    "expand_compound_search_term",
    "build_similar_issue_search_text",
    "extract_requested_limit",
    "wants_all_results",
    "resolve_result_limit",
    "extract_limit",
]
