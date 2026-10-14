"""Mantis 검색어, 유사 이슈 질의와 결과 표시 개수를 해석한다."""

import re
from typing import Any, Dict, List, Optional

from backend.common import LOOKUP_REQUEST_PATTERN, extract_quoted_value, is_control_search_term, normalize_for_match, normalize_search_token
from backend.intent.constants import TRIAGE_REQUEST_PATTERN
from backend.config import (
    KOREAN_COUNT_WORDS,
    PENDING_WORK_QUERY_PATTERN,
    STATUS_ALIASES,
    STATUS_MATCH_ALIASES,
    STOPWORDS,
)
from backend.intent.list_continuation import remove_list_continuation_request
from backend.intent.entities import extract_mantis_keyword
from backend.intent.scope_changes import SCOPE_CONTROL_TERM, has_scope_edit, without_scope_clear_phrases
from backend.team_scope import remove_team_scope_phrase


KEYWORD_ALIAS_GROUPS = (
    ("VB스크립트", "VBScript", "VB Script"),
    ("자바스크립트", "JavaScript", "Java Script", "JScript", "J Script"),
)


def keyword_term_aliases(term: str) -> tuple[str, ...]:
    normalized = normalize_for_match(term)
    for aliases in KEYWORD_ALIAS_GROUPS:
        if any(normalized == normalize_for_match(alias) for alias in aliases):
            return aliases
    return (term,)


def text_matches_keyword_terms(text: str, terms: List[str]) -> bool:
    """요청 주제는 모두 유지하고, 같은 용어의 표기 차이만 허용한다."""
    normalized_text = normalize_for_match(text)
    return all(
        any(normalize_for_match(alias) in normalized_text for alias in keyword_term_aliases(term))
        for term in terms
    )


def term_is_covered_by_constraints(term: str, constraints: Dict[str, Any]) -> bool:
    from backend.intent.identifiers import COMPOUND_IDENTIFIER
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
    for clause in constraints.get("filter_clauses") or []:
        if clause.get("field") in {"title", "keyword"}:
            values = clause.get("value")
            values = values if isinstance(values, list) else [values]
            if any(normalized_term == normalize_for_match(str(value or "")) for value in values):
                return True
    if "open" in constraints.get("statuses", []) and normalized_term in {
        "남은", "남아있는", "잔여", "안끝난", "미종료",
    }:
        return True
    return any(
        normalized_term == normalize_for_match(alias)
        or (not COMPOUND_IDENTIFIER.fullmatch(term) and (
            normalized_term in normalize_for_match(alias)
            or normalize_for_match(alias) in normalized_term
        ))
        for alias in aliases
        if alias
    )


def remove_list_display_instruction(text: str) -> str:
    # 여러 표시 열과 출력 동사를 함께 지정한 절만 제거한다. '등록일 표시 오류'
    # 같은 실제 이슈 주제는 보존한다.
    column = r"(?:이슈\s*번호|제목|담당자|등록일|프로젝트|상태|중요도)"
    return re.sub(
        rf"(?<![가-힣]){column}(?:\s*(?:[,·/]|과|와|및)\s*{column})+"
        r"\s*(?:을|를)?\s*(?:(?:모두|전부|각각)\s*)?"
        r"(?:표시|보여|출력)(?:해\s*줘|해\s*주세요|줘|주세요)[.!?]?",
        " ",
        text,
    )


def build_keyword_terms(text: str, constraints: Optional[Dict[str, Any]] = None) -> List[str]:
    # 조건 해제 명령은 원문 검색 주제가 아니다. 인용한 제목·키워드는 보존한다.
    text = without_scope_clear_phrases(text)
    text = remove_list_display_instruction(remove_list_continuation_request(text))
    if (constraints or {}).get("mantis_team_scope") == "all":
        # 전사 조회는 원문의 키워드가 아니라 담당자 범위 지정이다. 인용한 주제는 보존한다.
        text = re.sub(
            r'''["'“‘][^"'”’]*["'”’]|(?<![가-힣])(?:전사(?:\s*(?:전체|기준))?(?:으로|로|의|에서|만|는|은)?|전체\s*(?:팀|회사)(?:으로|로)?|팀\s*(?:제한|범위)(?:을|를)?\s*(?:없이|(?:해제|제거)(?:해\s*(?:줘|주세요)|하고)?))(?![가-힣])''',
            lambda match: match[0] if match[0][0] in "\"'“‘" else " ",
            text,
        )
    text = re.sub(r"\b(VB|자바)\s+스크립트", r"\1스크립트", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(VB|Java|J)\s+Script(?=\b|[가-힣])", r"\1Script", text, flags=re.IGNORECASE)
    scope_edit = has_scope_edit(text)
    resolution = (constraints or {}).get("status_resolution") or {}
    if resolution.get("status_intent") == "open" and resolution.get("evidence") and not resolution.get("needs_clarification"):
        text = text.replace(str(resolution["evidence"]), " ")
    if constraints and constraints.get("statuses") and PENDING_WORK_QUERY_PATTERN.search(text):
        text = PENDING_WORK_QUERY_PATTERN.sub(" ", text)
        text = re.sub(r"(?<![가-힣])(?:아직|거|것|건)(?:만|들|은|는|이|을|를)?(?=\s|$)", " ", text)
    if constraints and (constraints.get("mantis_team_members") or constraints.get("mantis_team_scope") == "all"):
        text = remove_team_scope_phrase(text)
    if TRIAGE_REQUEST_PATTERN.search(text) and (symptom := extract_quoted_value(text)):
        text = symptom
    # '표로 (다/모두) 보여줘' 같은 결과 형식 표현은 검색어가 아니다. 사이에 낀 '다·모두·전부'까지 함께 뺀다.
    text = re.sub(
        r"(?<![가-힣])(?:표|테이블|목록|리스트|요약)(?:로|으로)\s*(?:(?:다|모두|전부|전체|한번에|한눈에)\s*)?(?=보여|정리|만들|작성|알려)",
        " ",
        text,
    )
    text = LOOKUP_REQUEST_PATTERN.sub(" ", text)
    text = re.sub(r"(?:^|(?<=\s))[.!?]+(?=\s|$)", " ", text)
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
        "말고", "아닌", "아니고", "없는", "없이", "있지만", "포함", "포함된",
        "고객사별", "고객사마다", "프로젝트별", "프로젝트마다",
        "플랫폼별", "플랫폼마다", "매체별", "매체마다", "매체구분별", "매체구분마다",
        "하나씩", "한개씩", "한건씩", "1개씩", "1건씩",
        "방금", "아까", "해당", "위", "조건", "그중", "그",
        "아니", "그대로", "그대", "두고", "유지", "유지하고", "바꿔", "바꿔서", "변경",
        "바꿔줘", "바꿔주세요", "변경해줘", "수정해줘",
        "고객사는", "프로젝트는", "플랫폼은", "매체는", "상태만", "담당자만", "담당만",
        "업무보고서", "업무보고서를", "주간보고서", "주간보고서를",
        "보고서", "보고서를", "작성", "작성해", "작성해줘", "작성해주세요",
        "요약해줘", "요약해주세요", "정리해줘", "정리해주세요", "비교해줘", "비교해주세요",
        "저번주", "지난주", "이번주", "전주", "금주", "저번", "주",
        "내", "나", "저", "나의", "제", "내가", "제가", "내게", "나에게", "저에게",
        "맡은", "담당한", "할당된", "고객사", "보여주세요", "알려주세요",
        "쪽", "쪽에", "쪽은", "보면", "있어", "있나", "있을까", "있나요", "있는지", "그럼", "그러면",
        "관련해서", "관련하여", "관련된", "관련한", "관련해", "바꿔달라", "바꿔달라는", "있었어", "곳", "곳은", "곳이", "어디", "어디야", "어디인가요",
        "해제", "제거", "해제해줘", "제거해줘", "무관", "상관없이",
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
        and not (scope_edit and SCOPE_CONTROL_TERM.fullmatch(term))
        and not compact_personal_term.fullmatch(term)
        and not is_control_search_term(term)
    ]
    if constraints:
        terms = [term for term in terms if not term_is_covered_by_constraints(term, constraints)]
    inherited = (constraints or {}).get("inherited_terms") or []
    return list(dict.fromkeys([*terms[:4], *inherited]))


def replacement_topic_terms(question: str, constraints: Dict[str, Any]) -> List[str]:
    """'그럼 X 관련…'의 새 주제만 읽고 '그중 X…'의 추가 조건은 보존한다."""
    match = re.match(r"^\s*(?:그럼|그러면)\s+(.+?)\s*관련(?:해서|하여|된|한|해)?", question)
    if not match or re.search(r"그\s*중|그대로|유지|추가|도\s*포함", match.group(1)):
        return []
    return build_keyword_terms(match.group(1), constraints)


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
    return bool(re.search(r"(?:모두|전부)(?:표로|목록으로|리스트로)?(?:정리|요약|조회|보여|알려)", normalized)
                or re.search(r"(?:이슈|결과|항목|문서)(?:를|을)?전체(?:표로|목록으로)?(?:정리|요약|조회)", normalized)) or any(
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
            "전체목록",
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
