"""제목·본문 리터럴을 분리하는 순수 텍스트 문법. 조회·의도 계층에 의존하지 않는다."""
import re
from typing import Any

from backend.utils.text import extract_quoted_value, normalize_for_match, remove_quoted_value


_LITERAL = r'''(?:"[^"]+"|'[^']+'|“[^”]+”|‘[^’]+’|[^\s"'“”‘’]+?)'''
_NEGATIVE_OPERATOR = r"(?:없는|제외(?:한|된|하고)?|빼고|(?:나오지|언급되지|포함되지)\s*않(?:는|은))"
_OPERATOR = rf"(?:{_NEGATIVE_OPERATOR}|포함(?:한|된|하는|하고)?|들어\s*(?:있는|간)|있는|나오는|나온|언급된|적힌)"
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
                operator = "not_contains" if re.fullmatch(_NEGATIVE_OPERATOR, operator_text) else "contains"
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
