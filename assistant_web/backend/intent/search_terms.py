"""Mantis 검색어, 유사 이슈 질의와 결과 표시 개수를 해석한다."""

import re
from typing import Any, Dict, List, Optional

from backend.common import is_control_search_term, normalize_for_match, normalize_search_token
from backend.config import STATUS_ALIASES, STATUS_MATCH_ALIASES, STOPWORDS
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
    personal_scope_text = bool(
        re.search(
            r"(?:^|[\s,])(?:내|나의|제|내가|제가|내게|나에게|저에게)(?:\s|[가-힣])",
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
        "하나씩", "한개씩", "한건씩", "1개씩", "1건씩",
        "방금", "아까", "해당", "위", "조건",
        "업무보고서", "업무보고서를", "주간보고서", "주간보고서를",
        "보고서", "보고서를", "작성", "작성해", "작성해줘", "작성해주세요",
        "저번주", "지난주", "이번주", "전주", "금주", "저번", "주",
        "내", "나의", "제", "내가", "제가", "내게", "나에게", "저에게",
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
