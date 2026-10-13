"""Mantis 분류를 제목 검색어와 구분해 정확 일치 조건으로 읽는다."""

import re


CATEGORY_EXPRESSION = re.compile(
    r"(?<![가-힣A-Za-z0-9_])(?:분류|카테고리|category)"
    r"(?=\s|[:=]|(?:는|은|가|이|를|을|만)(?:\s|[:=]))"
    r"(?P<assignment>\s*[:=]\s*|\s*(?:는|은|가|이|를|을|만)\s*(?:[:=]\s*)?)"
    r"(?P<value>[\"'“‘][^\"'”’\n]+[\"'”’]|[^\s,.;!?=:'\"“‘]+)"
    r"(?P<tail>\s*(?:분류\s*)?(?:제외(?:하고)?|빼고|말고))?",
    re.IGNORECASE,
)
CONTROL_VALUES = {"조건", "필터", "제한", "범위", "전체", "무관", "상관없이", "빼줘", "해제해줘", "변경", "바꿔줘"}


def category_expressions(text: str):
    quoted = [(m.start(), m.end()) for m in re.finditer(r'''["'“‘][^"'”’]*["'”’]''', text)]
    for match in CATEGORY_EXPRESSION.finditer(text):
        # 제목/본문의 인용문 안에 있는 '분류=…'는 검색 범위를 바꾸지 않는다.
        if any(start <= match.start() < end for start, end in quoted):
            continue
        raw = match["value"]
        value = raw.strip('"\'“‘”’') if raw[0] in '"\'“‘' else re.sub(r"(?:으로|로|인|만|이고|이며)$", "", raw)
        scope_control = bool(re.fullmatch(r"(?:조건|필터|제한|범위)(?:은|는|을|를|도|만)?", value))
        field_lookup = raw[0] not in '"\'“‘' and not re.search(r"[:=]", match["assignment"]) and bool(
            re.match(r"(?:뭐|무엇|어떤|알려|보여|확인해|설명해|조회해)", value))
        if value and value not in CONTROL_VALUES and not scope_control and not field_lookup:
            yield match, value


def extract_category_filter_clauses(text: str) -> list[dict]:
    return [{"field": "category", "operator": "not_equals" if match["tail"] else "equals", "value": value}
            for match, value in category_expressions(text)]


def remove_category_expressions(text: str) -> str:
    for match, _ in reversed(list(category_expressions(text))):
        text = text[:match.start()] + " " * (match.end() - match.start()) + text[match.end():]
    return text


def category_criteria(constraints: dict) -> list[str]:
    parts = []
    for clause in constraints.get("filter_clauses") or []:
        if clause.get("field") != "category":
            continue
        raw = clause.get("value")
        values = raw if isinstance(raw, list) else [raw]
        label = "분류 제외" if clause.get("operator", "").startswith("not_") else "분류"
        parts.append(f"{label}=" + ", ".join(str(value) for value in values if value))
    return parts


def category_scope_text(constraints: dict) -> str:
    parts = []
    for clause in constraints.get("filter_clauses") or []:
        if clause.get("field") != "category":
            continue
        raw = clause.get("value")
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            suffix = " 제외" if clause.get("operator", "").startswith("not_") else ""
            quoted = f"'{value}'" if re.search(r"\s", str(value)) else str(value)
            parts.append(f"분류={quoted}{suffix}")
    return " ".join(parts)
