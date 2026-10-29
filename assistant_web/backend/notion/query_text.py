"""본문 검색 조건과 따옴표 제목을 구분하는 순수 질문 파서."""

import re
from typing import Any

from backend.common import extract_quoted_value, normalize_for_match, remove_quoted_value


_LITERAL = r'''(?:"[^"]+"|'[^']+'|“[^”]+”|‘[^’]+’|[^\s"'“”‘’]+?)'''
_OPERATOR = r"(?:포함(?:한|된|하는|하고)?|들어\s*있는|있는|없는|제외(?:한|된|하고)?|빼고)"
_BODY_REQUESTS = (
    re.compile(
        rf"(?<![가-힣A-Za-z0-9_])(?P<scope>본문|내용|코드(?:\s*블록)?|표(?:\s*셀)?)"
        rf"(?:에서|에는|에도|에)\s*(?P<value>{_LITERAL})(?:을|를|이|가)?\s+"
        rf"(?P<operator>{_OPERATOR})(?=\s|$|[,.;!?])",
    ),
    re.compile(
        rf"(?<![가-힣A-Za-z0-9_])(?P<value>{_LITERAL})\s+"
        rf"(?P<scope>내용|본문|코드)(?:이|가)?\s+(?P<operator>있는|없는|포함된)(?=\s|$|[,.;!?])",
    ),
)
_QUOTES = re.compile(r'''"[^"]*"|'[^']*'|“[^”]*”|‘[^’]*’''')
_BODY_CONTINUATION = re.compile(
    rf"\s+(?P<value>{_LITERAL})(?:을|를|이|가)?\s+"
    rf"(?P<operator>{_OPERATOR})(?=\s|$|[,.;!?])",
)
_STRUCTURED_CHAIN_FIELD = re.compile(
    r"(?:담당자?|기여자|공헌자|작성자|요청자|등록자|owner|contributors?|"
    r"(?:진행)?상태|status|제목|이름|문서명|title|고객사?|customer|client|"
    r"매체|플랫폼|platform|media|유형|종류|type|category|데이터베이스|database|db|"
    r"(?:등록|접수|생성|작성|수정|반영|배포|완료|요청)(?:일자?|날짜)|마감일?|기한|"
    r"날짜|일자|date|created_at|updated_at|due_at|deployed_at)(?:은|는|이|가|을|를|도)?",
    re.IGNORECASE,
)


def split_notion_body_query(text: str) -> tuple[str, list[dict[str, Any]]]:
    """명시적인 본문 조건만 소비한다. 제목 안의 같은 문구는 보존한다."""
    clauses: list[dict[str, Any]] = []
    for pattern in _BODY_REQUESTS:
        quoted_spans = [match.span() for match in _QUOTES.finditer(text)]
        pieces = []
        consumed = 0
        for match in pattern.finditer(text):
            if match.start() < consumed:
                continue
            if any(start < match.start() < end for start, end in quoted_spans):
                continue
            pieces.append(text[consumed:match.start()])
            scope = match.group("scope")
            current = match
            while True:
                value = current.group("value").strip().strip("\"'“”‘’")
                operator_text = current.group("operator")
                operator = "not_contains" if operator_text.startswith(("없는", "제외", "빼고")) else "contains"
                clauses.append({"field": "keyword", "operator": operator, "value": value,
                                "block_type": "code" if scope.startswith("코드") else "table_row" if scope.startswith("표") else ""})
                consumed = current.end()
                # '본문에 A 포함하고 B 제외한'의 B는 같은 본문 범위를
                # 상속한다. 연결 어미와 인접 조건이 없으면 제목을 보존한다.
                continuation = (
                    _BODY_CONTINUATION.match(text, consumed)
                    if operator_text.endswith(("하고", "빼고")) else None
                )
                if continuation is None:
                    break
                # '... 포함하고 담당자 없는'은 담당 속성 조건이다.
                # 따옴표를 유지한 채 검사하므로 '"담당자" 없는'은 본문 조건으로 남는다.
                if _STRUCTURED_CHAIN_FIELD.fullmatch(normalize_for_match(continuation.group("value"))):
                    break
                current = continuation
            pieces.append(" ")
        pieces.append(text[consumed:])
        text = "".join(pieces)
    return text, clauses


def notion_literal_title(text: str) -> str:
    subject, _clauses = split_notion_body_query(text)
    return extract_quoted_value(subject)


def notion_structural_query(text: str) -> str:
    """제목·본문 리터럴을 상태/담당자/날짜 조건으로 재해석하지 않는다."""
    subject, _clauses = split_notion_body_query(text)
    return remove_quoted_value(subject)


_REGISTRATION_ORDER = re.compile(
    r"(?<![가-힣A-Za-z])(?:최근|최신)(?:에|의)?\s*(?:새로\s*)?"
    r"(?:등록|접수|생성|작성)(?:된|한|됐|되었|되어)?(?=\s|$|[?!.,])"
    r"|(?<![가-힣A-Za-z])(?:등록|접수|생성|작성)(?:일)?\s*순(?:으로)?(?=\s|$|[?!.,])"
)


def notion_registration_sort(text: str) -> dict[str, str] | None:
    """기간을 임의로 만들지 않고 최근 등록·등록순 표현만 정렬로 해석한다."""
    if _REGISTRATION_ORDER.search(notion_structural_query(text)):
        return {"field": "created_at", "direction": "desc"}
    return None


def without_registration_order(text: str) -> str:
    """제목·본문 리터럴을 분리한 검색 주제에서 정렬 표현만 제거한다."""
    return _REGISTRATION_ORDER.sub(" ", text)


def grounded_notion_body_constraints(text: str, constraints: dict[str, Any]) -> dict[str, Any]:
    """리터럴 오해석은 버리고 바깥 문장에 근거한 플래너의 OR 조건은 보존한다."""
    from backend.intent.constraints import extract_query_constraints
    from backend.query_filters import normalize_filter_clauses, values_for_clause

    structural = notion_structural_query(text)
    grounded = extract_query_constraints(structural)
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
