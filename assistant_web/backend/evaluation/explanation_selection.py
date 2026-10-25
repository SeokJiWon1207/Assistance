"""반복 키를 줄인 모델 선택값을 기존 근거 검증 형식으로만 확장한다."""
from copy import deepcopy
from typing import Any


def expand_selection(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    result = deepcopy(payload)
    for field, keys in (
        ('sections', ('topic', 'evidence_ids', 'unanswered')),
        ('answer_items', ('question_part', 'evidence_ids', 'unanswered', 'answer')),
    ):
        rows = result.get(field, [])
        if not isinstance(rows, list):
            return {}
        if any(isinstance(row, list) for row in rows):
            if any(not isinstance(row, list) or len(row) != len(keys) for row in rows):
                return {}
            result[field] = [dict(zip(keys, row, strict=True)) for row in rows]
    # 필드·문단 ID·미확정·선택 범위의 의미 검사는 기존 검증기에서 그대로 수행한다.
    return result
