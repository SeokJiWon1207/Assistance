"""긴 원문을 버리지 않고 출처 안의 겹치는 문단과 핵심 발췌로 보존한다."""
import re


def passage_parts(text: str, limit: int = 450) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts = []
    start = 0
    while start < len(text):
        end = min(start + limit, len(text))
        if end < len(text):
            space = text.rfind(' ', start + limit // 2, end)
            if space > start:
                end = space
        part = text[start:end].strip()
        if part:
            parts.append(part)
        if end == len(text):
            break
        start = max(start + 1, end - 100)
    return parts


def focused_excerpt(text: str, pattern: re.Pattern, limit: int) -> str:
    """핵심 앞의 긴 배경 설명 때문에 진단·조치가 잘리지 않게 원문 창을 고른다."""
    if len(text) <= limit:
        return text
    match = pattern.search(text)
    if match is None or match.end() <= limit - 10:
        return text
    start = max(0, match.start() - 35)
    return ('… ' if start else '') + text[start:]
