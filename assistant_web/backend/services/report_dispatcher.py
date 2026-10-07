"""업무 보고서의 우선순위와 도구 사용 범위를 결정한다."""

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from backend.execution import checkpoint
from backend.services.contracts import DeterministicReportResult
from backend.understanding import extract_query_constraints, rule_based_search_intent


ReportBuilder = Callable[..., dict[str, Any] | None]

# 구체적인 요청부터 평가한다. 순서가 응답을 선택하는 계약이다.
REPORT_ORDER = (
    "unknown_customer_guard", "similar_resolution", "incident_triage",
    "monthly_customer", "weekly_report", "customer_profile", "sla", "pivot",
    "spike", "top_customers", "effort_trend", "effort_ranking",
)


@dataclass(frozen=True)
class ReportRequest:
    latest_text: str
    retrieval_text: str
    enabled_tools: dict[str, Any]
    messages: list[Any]
    user: dict[str, Any] | None


def dispatch_reports(
    request: ReportRequest,
    builders: Mapping[str, ReportBuilder],
    *,
    effort_builder: ReportBuilder,
    notion_writer: ReportBuilder,
) -> DeterministicReportResult:
    checkpoint()
    if not request.latest_text:
        return DeterministicReportResult()
    text = request.retrieval_text or request.latest_text
    source = rule_based_search_intent(text, extract_query_constraints(text)).get("source")
    pending = None
    if request.enabled_tools.get("mantis") and source != "notion":
        for name in REPORT_ORDER:
            if name == "similar_resolution" and not request.enabled_tools.get("vectorDb"):
                continue
            checkpoint()
            options = {"search_similar": bool(request.enabled_tools.get("vectorDb"))} if name == "incident_triage" else {}
            response = builders[name](text, **options)
            checkpoint()
            if response:
                return DeterministicReportResult(response=response)
        checkpoint()
        effort = effort_builder(text)
        checkpoint()
        if effort:
            if effort.get("needsPerson"):
                pending = {key: value for key, value in effort.items() if key != "needsPerson"}
            else:
                return DeterministicReportResult(response=effort)
    if request.enabled_tools.get("notion"):
        checkpoint()
        response = notion_writer(
            request.latest_text, request.messages,
            allow_write=bool(request.user and request.user.get("role") in {"manager", "admin"}),
            user_uid=str((request.user or {}).get("uid") or ""),
        )
        checkpoint()
        if response:
            return DeterministicReportResult(response=response)
    return DeterministicReportResult(pending_effort_report=pending)
