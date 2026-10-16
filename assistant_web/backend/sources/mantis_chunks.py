"""원본 필드와 위치를 보존하는 검색 전용 구간. 집계 문서는 변경하지 않는다."""
import re
from typing import Any, Iterator

CHUNK_SIZE = 1200
CHUNK_OVERLAP = 150


def split_issue(issue: dict[str, Any]) -> Iterator[dict[str, Any]]:
    sections = [(field, str(issue.get(field) or "")) for field in
                ("summary", "description", "additional_information", "steps_to_reproduce")]
    sections.extend((str(item.get("field", {}).get("name")), str(item.get("value") or ""))
                    for item in issue.get("custom_fields") or []
                    if item.get("field", {}).get("name") in {"사유 및 원인", "조치 및 개선"})
    sections.extend((f"note:{note.get('id', index)}", str(note.get("text") or ""))
                    for index, note in enumerate(issue.get("notes") or []))
    for field, text in sections:
        if not text.strip():
            continue
        start = 0
        while start < len(text):
            end = min(start + CHUNK_SIZE, len(text))
            if end < len(text):
                boundary = text.rfind("\n", start + CHUNK_SIZE // 2, end)
                if boundary >= 0:
                    end = boundary + 1
            yield {"issue_id": int(issue["id"]), "title": str(issue.get("summary") or ""),
                   "field": field, "start": start, "end": end, "text": text[start:end]}
            if end == len(text):
                break
            start = end - CHUNK_OVERLAP


def search_tokens(text: str) -> list[str]:
    """한국어 조사 변화는 글자쌍, 오류 코드·화면번호는 단어로 찾는다."""
    tokens = []
    for word in re.findall(r"[가-힣]+|[a-z0-9]+(?:[_./:-][a-z0-9]+)*", text.lower()):
        tokens.append("w" + word.replace("_", "x").replace("/", "x").replace(".", "x").replace(":", "x").replace("-", "x"))
        if re.fullmatch(r"[가-힣]+", word):
            tokens.extend("g" + word[index:index + 2] for index in range(len(word) - 1))
    return tokens


def embedding_text(chunk: dict[str, Any]) -> str:
    return f"[제목] {chunk['title'][:200]}\n[{chunk['field']}]\n{chunk['text']}"
