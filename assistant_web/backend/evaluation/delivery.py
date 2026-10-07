"""확정적인 오류만 응답 전달을 막고, 휴리스틱 진단은 검토 정보로 남긴다."""

from dataclasses import dataclass
from typing import Any


# 작업 의도 추정(operation_intent), 텍스트 기반 조건 추정 경고, AI 서술 검토는
# 확정적 오류가 아니므로 전달 차단에 사용하지 않는다.
BLOCKING_CHECKS = frozenset({
    "issue_ids", "constraints", "status_resolution", "filter_schema",
    "count_result", "count_filters", "effort_recalculation", "effort_breakdown",
    "effort_display", "effort_issue_count", "work_intent", "work_scope",
    "work_evidence", "work_answer_count",
})


@dataclass(frozen=True)
class DeliveryDecision:
    blocked_checks: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return not self.blocked_checks


def decide_delivery(verification: dict[str, Any]) -> DeliveryDecision:
    # 쓰기 확인문에는 사용자가 입력한 제목/메모가 그대로 들어간다. 그 안의
    # 이슈 번호를 검색 사실 주장으로 판정하거나 완료된 쓰기 결과를 숨기지 않는다.
    # 쓰기 권한·대상·승인 검증은 Notion 쓰기 처리기가 실행 전에 담당한다.
    if verification.get("mode") in {"notion_write_pending", "notion_create", "notion_update"}:
        return DeliveryDecision()
    return DeliveryDecision(tuple(dict.fromkeys(
        str(check["id"])
        for check in verification.get("checks", [])
        if check.get("id") in BLOCKING_CHECKS and check.get("status") == "fail"
    )))


BLOCKED_ANSWER = (
    "조회 조건 또는 집계 결과가 원문과 일치하는지 확인하지 못해 답변을 보류했습니다. "
    "고객사·담당자·기간 등 조회 조건을 구체적으로 지정해 다시 요청해 주세요."
)
