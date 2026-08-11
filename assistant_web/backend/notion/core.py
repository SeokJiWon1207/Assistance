"""기존 Notion 공개 경로를 기능별 구현 모듈에 연결하는 호환 파사드."""

import requests as requests

from backend.notion import client, formatting, mapping, schema, search, write
from backend.notion.client import (
    fetch_notion_database_schema as fetch_notion_database_schema,
    fetch_notion_database_title as fetch_notion_database_title,
    fetch_notion_page_blocks as fetch_notion_page_blocks,
    fetch_notion_relation_title as fetch_notion_relation_title,
    fetch_notion_user_name as fetch_notion_user_name,
    fetch_notion_users as fetch_notion_users,
    get_notion_headers as get_notion_headers,
    get_notion_settings as get_notion_settings,
    notion_post_json as notion_post_json,
)
from backend.notion.formatting import (
    build_sources_from_notion_pages as build_sources_from_notion_pages,
    build_sources_from_single_notion_page as build_sources_from_single_notion_page,
    format_deterministic_notion_answer as format_deterministic_notion_answer,
    format_grouped_notion_answer as format_grouped_notion_answer,
    notion_page_owner_names as notion_page_owner_names,
    should_return_deterministic_notion_list as should_return_deterministic_notion_list,
)
from backend.notion.mapping import (
    normalize_notion_page as normalize_notion_page,
    notion_properties_to_text as notion_properties_to_text,
    resolve_notion_page_owner as resolve_notion_page_owner,
)
from backend.notion.schema import (
    ensure_configured_notion_database as ensure_configured_notion_database,
    notion_property_schema as notion_property_schema,
    validate_notion_page_id as validate_notion_page_id,
)
from backend.notion.search import (
    build_notion_terms as build_notion_terms,
    collect_retrieved_notion_pages as collect_retrieved_notion_pages,
    correct_notion_status_typo as correct_notion_status_typo,
    deduplicate_notion_pages_by_id as deduplicate_notion_pages_by_id,
    execute_notion_search as execute_notion_search,
    extract_notion_constraints as extract_notion_constraints,
    inherit_notion_context as inherit_notion_context,
    merge_common_notion_constraints as merge_common_notion_constraints,
    notion_database_status_filter as notion_database_status_filter,
    notion_page_matches_constraints as notion_page_matches_constraints,
    notion_search_max_pages as notion_search_max_pages,
)
from backend.notion.write import (
    create_notion_page as create_notion_page,
    execute_notion_write_command as execute_notion_write_command,
    find_pending_notion_write_command as find_pending_notion_write_command,
    is_notion_write_confirm_phrase as is_notion_write_confirm_phrase,
    run_notion_write_command as run_notion_write_command,
    update_notion_page as update_notion_page,
)


for _module in (client, mapping, schema, search, formatting, write):
    for _name in _module.__all__:
        globals().setdefault(_name, getattr(_module, _name))

__all__ = sorted(
    name for name in globals() if not name.startswith("_") and name != "requests"
)
