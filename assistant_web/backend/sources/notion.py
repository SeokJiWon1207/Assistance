"""기존 Notion 소스 import 경로를 새 구현으로 연결하는 호환 파사드."""

import sys

from backend.notion import core as _core
from backend.notion.core import (
    build_notion_terms,
    build_sources_from_notion_pages,
    build_sources_from_single_notion_page,
    collect_retrieved_notion_pages,
    create_notion_page,
    deduplicate_notion_pages_by_id,
    ensure_configured_notion_database,
    execute_notion_search,
    extract_notion_constraints,
    fetch_notion_database_schema,
    fetch_notion_database_title,
    fetch_notion_users,
    format_deterministic_notion_answer,
    format_grouped_notion_answer,
    inherit_notion_context,
    merge_common_notion_constraints,
    normalize_notion_page,
    notion_database_status_filter,
    notion_page_matches_constraints,
    notion_properties_to_text,
    notion_search_max_pages,
    resolve_notion_page_owner,
    run_notion_write_command,
    should_return_deterministic_notion_list,
    update_notion_page,
    validate_notion_page_id,
)


__all__ = [
    "build_notion_terms",
    "build_sources_from_notion_pages",
    "build_sources_from_single_notion_page",
    "collect_retrieved_notion_pages",
    "create_notion_page",
    "deduplicate_notion_pages_by_id",
    "ensure_configured_notion_database",
    "execute_notion_search",
    "extract_notion_constraints",
    "fetch_notion_database_schema",
    "fetch_notion_database_title",
    "fetch_notion_users",
    "format_deterministic_notion_answer",
    "format_grouped_notion_answer",
    "inherit_notion_context",
    "merge_common_notion_constraints",
    "normalize_notion_page",
    "notion_database_status_filter",
    "notion_page_matches_constraints",
    "notion_properties_to_text",
    "notion_search_max_pages",
    "resolve_notion_page_owner",
    "run_notion_write_command",
    "should_return_deterministic_notion_list",
    "update_notion_page",
    "validate_notion_page_id",
]


sys.modules[__name__] = _core
