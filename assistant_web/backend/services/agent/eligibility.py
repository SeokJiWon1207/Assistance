"""실행기와 독립적인 에이전트 지원 범위·정책 판정."""
from backend.execution import CURRENT_BUDGET
from backend.services.agent.policy import active_agent_policy
from backend.services.agent.documents import document_kind
from backend.services.contracts import ChatPipelineContext
from backend.tools.conditions import planned_sources
from backend.tools.report_contracts import REPORT_KINDS


AGENT_SOURCES = frozenset({"mantis", "notion", "both"})
READ_OPERATIONS = frozenset({'count', 'search', 'list', 'aggregate', 'compare', 'get', 'explain'})
CALCULATION_REPORTS = REPORT_KINDS


def query_plan(plan: dict) -> bool:
    return ((plan.get('source') in {'mantis', 'both'}
             and plan.get('operation') in READ_OPERATIONS - {'explain'})
            or plan.get('source') == 'notion' and plan.get('operation') in {'count', 'search', 'list', 'get'})


def calculation_report(plan: dict) -> bool:
    return (plan.get('source') in {'mantis', 'both'} and plan.get('operation') == 'report'
            and plan.get('report_kind') in CALCULATION_REPORTS)


def supports_read_plan(plan: dict) -> bool:
    return bool(plan.get('source') in AGENT_SOURCES and (
        plan.get('operation') == 'explain' or query_plan(plan) or calculation_report(plan) or document_kind(plan)))


def uses_query_tools(context: ChatPipelineContext) -> bool:
    plan = context.intent_plan or {}
    return supports_read_plan(plan)


def agent_route_reason(context: ChatPipelineContext) -> str:
    """진입하지 않는 이유. 빈 문자열이면 진입한다."""
    policy = active_agent_policy()
    if policy is None:
        return "not_candidate"
    if policy.model_id != (context.model or {}).get("id"):
        return "model_changed"
    if context.agent_planning_context:
        return ""
    plan = context.intent_plan or {}
    if plan.get('operation') == 'get':
        from backend.intent.context import parse_ordinal_reference
        if (parse_ordinal_reference(context.latest_user_text) is not None
                and context.ordinal_source_id not in (context.query_constraints.get('issue_ids') or [])):
            return 'unresolved_reference'
    # 업무 문서 종류는 출력 목적이다. 실제 조회·읽기는 에이전트가 결정한다.
    if plan.get("planner") != "unified_llm" or not supports_read_plan(plan):
        return "not_explain"
    if plan.get("source") not in AGENT_SOURCES:
        return "not_internal_source"
    if not any(context.enabled_tools.get(source) for source in planned_sources(plan)):
        return "source_disabled"
    if uses_query_tools(context) and not all(context.enabled_tools.get(source) for source in planned_sources(plan)):
        return "source_disabled"
    budget = CURRENT_BUDGET.get()
    if budget is not None and budget.remaining() < policy.settings.min_start_seconds:
        return "insufficient_time"
    return ""


