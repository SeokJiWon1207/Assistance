"""본문 검색 조건과 따옴표 제목을 구분하는 순수 질문 파서."""

import re
from typing import Any

from backend.utils.text import normalize_for_match
from backend.text_scope import (split_notion_body_query, notion_literal_title as notion_literal_title, notion_structural_query,
                                _STRUCTURED_CHAIN_FIELD)


_REGISTRATION_ORDER = re.compile(
    r"(?<![가-힣A-Za-z])(?:최근|최신)(?:에|의)?\s*(?:새로\s*)?"
    r"(?:등록|접수|생성|작성)(?:된|한|됐|되었|되어)?(?=\s|$|[?!.,])"
    r"|(?<![가-힣A-Za-z])(?:등록|접수|생성|작성)(?:일)?\s*순(?:으로)?(?=\s|$|[?!.,])"
)


def is_plain_notion_topic(text: str) -> bool:
    return bool(re.search(r'(?:관련|관한)\s*(?:(?:최근|최신)\s*)?(?:문서|페이지|자료)', text)
                and not re.search(r'''["'`“”‘’]|본문|내용|노트|텍스트|제목|title|body|content|키워드|문자열|정확히|포함|들어''',
                                  text, re.I))


def split_notion_topic_terms(terms: list[str]) -> list[str]:
    """일반 주제는 모든 단어를 찾되, 플래너의 검색어 한도 때문에 조건을 버리지는 않는다."""
    words = list(dict.fromkeys(word for term in terms for word in term.split()))
    return words if len(words) <= 6 else terms


def notion_registration_sort(text: str) -> dict[str, str] | None:
    """기간을 임의로 만들지 않고 최근 등록·등록순 표현만 정렬로 해석한다."""
    if _REGISTRATION_ORDER.search(notion_structural_query(text)):
        return {"field": "created_at", "direction": "desc"}
    return None


def without_registration_order(text: str) -> str:
    """제목·본문 리터럴을 분리한 검색 주제에서 정렬 표현만 제거한다."""
    return _REGISTRATION_ORDER.sub(" ", text)


def grounded_notion_body_constraints(text: str, constraints: dict[str, Any], *, grounded: dict[str, Any]) -> dict[str, Any]:
    """리터럴 오해석은 버리고 바깥 문장에 근거한 플래너의 OR 조건은 보존한다."""
    from backend.query_filters import normalize_filter_clauses, values_for_clause

    structural = notion_structural_query(text)
    literal_free = normalize_for_match(structural)
    clauses = [clause for clause in normalize_filter_clauses(constraints.get("filter_clauses") or [])
               if clause in grounded.get("filter_clauses", []) or (
                   clause["operator"] not in {"exists", "not_exists"}
                   and all(normalize_for_match(value) in literal_free for value in values_for_clause(clause.get("value")))
               )]
    merged = {**grounded, "filter_clauses": normalize_filter_clauses([*grounded.get("filter_clauses", []), *clauses])}
    # 공통 키워드 추출기가 '담당자 없는'을 본문 부정어로 읽어도,
    # 속성 이름은 암묵적 본문 조건으로 승격하지 않는다. 명시적 본문 리터럴은 아래에서 복원한다.
    merged["filter_clauses"] = [clause for clause in merged["filter_clauses"] if not (
        clause["field"] == "keyword"
        and any(_STRUCTURED_CHAIN_FIELD.fullmatch(normalize_for_match(value))
                for value in values_for_clause(clause.get("value")))
    )]
    # 같은 필드의 단일 포함 조건은 검증된 OR 집합이 대체한다.
    choices = [clause for clause in clauses if clause["operator"] == "in" and len(values_for_clause(clause.get("value"))) > 1]
    merged["filter_clauses"] = [clause for clause in merged.get("filter_clauses", []) if not any(
        clause["field"] == choice["field"] and clause["operator"] in {"equals", "in"}
        and set(values_for_clause(clause.get("value"))) < set(values_for_clause(choice.get("value")))
        for choice in choices
    )]
    _subject, body_clauses = split_notion_body_query(text)
    merged["filter_clauses"] = normalize_filter_clauses([*merged["filter_clauses"], *body_clauses])
    return merged
