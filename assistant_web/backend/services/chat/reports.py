"""결정형 리포트 선택과 인증 범위 적용."""

from typing import Any, Dict, List, Optional

from backend.answers import build_unknown_customer_report
from backend.intent.request_scope import AUTHENTICATED_OWNER, authenticated_owner_scope
from backend.reports.aggregate import (
    build_mantis_pivot_report,
    build_mantis_spike_report,
    build_top_customers_report,
)
from backend.reports.effort import build_effort_ranking_report, build_effort_report
from backend.reports.effort_trend import build_effort_trend_report
from backend.reports.customer import build_customer_profile_report
from backend.reports.incident_triage import build_incident_triage_report
from backend.reports.monthly_customer import build_monthly_customer_report
from backend.reports.similar import build_similar_resolution_report
from backend.reports.sla import build_sla_report
from backend.reports.weekly_customer import build_weekly_report
from backend.services.contracts import DeterministicReportResult
from backend.services.report_dispatcher import ReportRequest, dispatch_reports
from backend.notion.write import run_notion_write_command
from backend.intent.context import get_recent_user_texts, is_follow_up_question
from backend.services.chat.scope import should_scope_to_authenticated_mantis_user


def build_deterministic_report(
    latest_user_text: str,
    retrieval_text: str,
    enabled_tools: Dict[str, Any],
    messages: List[Any],
    user: Optional[Dict[str, Any]],
) -> DeterministicReportResult:
    scope_text = " ".join(get_recent_user_texts(messages, limit=5)) if is_follow_up_question(latest_user_text) else latest_user_text
    owner = AUTHENTICATED_OWNER.get()
    if not owner and should_scope_to_authenticated_mantis_user(scope_text):
        owner = str((user or {}).get("mantisUsername") or (user or {}).get("displayName") or "").strip()
    with authenticated_owner_scope(owner):
        return _build_deterministic_report(latest_user_text, retrieval_text, enabled_tools, messages, user)


def _build_deterministic_report(
    latest_user_text: str,
    retrieval_text: str,
    enabled_tools: Dict[str, Any],
    messages: List[Any],
    user: Optional[Dict[str, Any]],
) -> DeterministicReportResult:
    return dispatch_reports(
        ReportRequest(latest_user_text, retrieval_text, enabled_tools, messages, user),
        {
            "unknown_customer_guard": build_unknown_customer_report,
            "similar_resolution": build_similar_resolution_report,
            "incident_triage": build_incident_triage_report,
            "monthly_customer": build_monthly_customer_report,
            "weekly_report": build_weekly_report,
            "customer_profile": build_customer_profile_report,
            "sla": build_sla_report,
            "pivot": build_mantis_pivot_report,
            "spike": build_mantis_spike_report,
            "top_customers": build_top_customers_report,
            "effort_trend": build_effort_trend_report,
            "effort_ranking": build_effort_ranking_report,
        },
        effort_builder=build_effort_report,
        notion_writer=run_notion_write_command,
    )
