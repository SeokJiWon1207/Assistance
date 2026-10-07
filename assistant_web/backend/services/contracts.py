"""채팅 실행 단계가 공유하는 문맥과 명시적인 완료 상태."""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Dict, List, Optional


class StageState(StrEnum):
    CONTINUE = "continue"
    ANSWER = "answer"
    CLARIFICATION = "clarification"
    DEGRADED = "degraded"


@dataclass(frozen=True)
class StageResult:
    state: StageState
    response: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if (self.state == StageState.CONTINUE) != (self.response is None):
            raise ValueError("계속 실행하는 단계는 응답이 없어야 하며, 종료 단계는 응답이 필요합니다.")
        if self.response is not None and not isinstance(self.response.get("content"), str):
            raise ValueError("종료 단계에는 문자열 답변이 필요합니다.")

    @classmethod
    def from_response(cls, response: Optional[Dict[str, Any]]) -> "StageResult":
        if response is None:
            return cls(StageState.CONTINUE)
        mode = str((response.get("raw") or {}).get("mode", ""))
        state = StageState.ANSWER
        if "clarification" in mode or mode.startswith("missing_personal_"):
            state = StageState.CLARIFICATION
        elif mode in {"empty_evidence", "model_timeout_fallback"}:
            state = StageState.DEGRADED
        return cls(state, response)


@dataclass
class DeterministicReportResult:
    """LLM 계획 전에 실행하는 결정형 리포트의 판정 결과."""

    response: Optional[Dict[str, Any]] = None
    pending_effort_report: Optional[Dict[str, Any]] = None


@dataclass
class ChatPipelineContext:
    """한 채팅 요청이 분석·라우팅·검색 단계를 지나며 공유하는 상태."""

    latest_user_text: str
    model: Dict[str, Any]
    api_key: str
    messages: List[Dict[str, Any]]
    retrieval_text: str
    enabled_tools: Dict[str, Any]
    system_notes: List[str]
    query_constraints: Dict[str, Any]
    active_limit: int
    authenticated_owner: str = ""
    pending_effort_report: Optional[Dict[str, Any]] = None
    intent_plan: Optional[Dict[str, Any]] = None
    retrieval_result: Optional[Dict[str, Any]] = None
    status_resolution: Optional[Dict[str, Any]] = None
