"""설명 답변의 현재 이슈 상태를 조회된 메타데이터로 표시한다."""
import re
from typing import Any

from backend.evaluation.explanation_quotes import _label


def current_status_table(question: str, grounding: dict[str, Any], selected: list[int]) -> str:
    if not re.search(r"(?:현재|지금|이슈(?:의)?)\s*상태", question):
        return ""
    rows = [issue for issue in grounding.get("issues", []) if issue.get("id") in selected]
    if not rows:
        return ""
    lines = ["### 현재 이슈 상태", "", "| 이슈 ID | 상태 |", "| --- | --- |"]
    for issue in rows:
        value = str(issue.get("status") or "상태 미상").replace("|", r"\|").replace("\n", " ")
        lines.append(f"| #{issue['id']} | {_label(value)} |")
    lines.extend(["", "조회된 이슈의 상태 필드 기준입니다. 기술 조치·고객 적용 완료 여부와는 별도로 확인합니다."])
    return "\n".join(lines)


def is_current_status_item(item: dict[str, Any]) -> bool:
    # 복합 질문 항목이나 과거의 진행 상태·운영 반영 상태는 제거하지 않는다.
    return bool(re.fullmatch(r"(?:(?:현재|지금|이슈|Mantis)(?:의)?\s*)*상태", str(item.get("question_part") or "")))
