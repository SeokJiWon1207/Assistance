"""에이전트 경로가 사용하는 읽기 전용 도구 목록."""

from backend.tools import mantis_tools, notion_tools
from backend.tools.registry import ToolRegistry


def agent_registry(*, query_tools: bool = False) -> ToolRegistry:
    specs = [*mantis_tools.SPECS, *notion_tools.SPECS]
    if not query_tools:
        from backend.tools import notion_queries
        specs.extend(spec for spec in notion_queries.SPECS if spec.name == 'get_notion_page_properties')
    if query_tools:
        from backend.tools import mantis_queries, mantis_aggregations, notion_queries, issue_reports
        specs.extend(mantis_queries.SPECS)
        specs.extend(mantis_aggregations.SPECS)
        specs.extend(mantis_tools.BATCH_SPECS)
        specs.extend(notion_queries.SPECS)
        specs.extend(issue_reports.SPECS)
    return ToolRegistry(specs)
