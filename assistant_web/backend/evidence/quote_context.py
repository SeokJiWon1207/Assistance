"""인용의 이슈 색인과 절단하지 않은 원문 문맥."""
import re
from typing import Any

TOPIC_TITLES = {
    "cause": "원인", "action": "조치", "timeline": "진행 이력",
    "conditions": "조건", "technical": "기술 내용", "actors": "기록된 관계자",
    "background": "등록 내용",
}


UNANSWERED_TEXT = "이 항목은 선택한 근거로 답변을 확정하지 못했습니다"


_MAX_CONTEXT_CHARS = 12000


def _issues(grounding: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {issue["id"]: issue for issue in grounding.get("issues", [])
            if isinstance(issue, dict) and type(issue.get("id")) is int}


def _paragraphs(text: str, quote: str) -> list[str]:
    """개행으로 꺾인 부정·예정 표현도 보존하도록 빈 줄 사이의 문단을 복원한다."""
    boundaries = list(re.finditer(r"\n[ \t]*\n", text))
    contexts = []
    start = text.find(quote)
    while start >= 0:
        end = start + len(quote)
        left = max((match.end() for match in boundaries if match.end() <= start), default=0)
        right = min((match.start() for match in boundaries if match.start() >= end), default=len(text))
        context = text[left:right]
        if context not in contexts:
            contexts.append(context)
        start = text.find(quote, start + 1)
    return contexts
