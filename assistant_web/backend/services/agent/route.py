"""검증된 설명·이슈 문서 작업을 에이전트에 전달한다. 판정에는 모델·검색 호출을 쓰지 않는다."""

from typing import Any, Dict, Optional

from backend.schemas import ChatRequest
from backend.services.agent.policy import active_agent_policy
from backend.services.contracts import ChatPipelineContext
from backend.services.agent.eligibility import agent_route_reason as agent_route_reason, uses_query_tools


def shadow_skipped(request: ChatRequest, reason: str) -> Dict[str, Any]:
    return {"modelId": request.modelId, "sources": [], "raw": {"mode": "agent_query_shadow_skipped"},
            "content": f"shadow 비교를 건너뛰었습니다. {reason}"}


def run_query_shadow_if_requested(request: ChatRequest, context: ChatPipelineContext) -> Optional[Dict[str, Any]]:
    from backend.services.agent.query_shadow import QUERY_SHADOW
    if not QUERY_SHADOW.get():
        return None
    policy = active_agent_policy()
    if not policy or agent_route_reason(context):
        return shadow_skipped(request, "검증된 읽기 계획·모델 정책·원천 설정 또는 남은 시간을 확인해 주세요.")
    from backend.services.agent.orchestrator import run_agent_investigation
    return run_agent_investigation(request, context, policy, query_tools=True)


def run_agent_if_eligible(request: ChatRequest, context: ChatPipelineContext) -> Optional[Dict[str, Any]]:
    policy = active_agent_policy()
    if policy is None or agent_route_reason(context):
        return None
    from backend.services.agent.orchestrator import run_agent_investigation
    return run_agent_investigation(request, context, policy, query_tools=uses_query_tools(context))
