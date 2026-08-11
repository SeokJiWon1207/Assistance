"""기존 질의 이해 공개 경로를 기능별 구현 모듈에 연결하는 호환 파사드."""

from backend.audit import load_audit_sources as load_audit_sources
from backend.intent.constants import (
    COMBINED_SOURCE_MARKERS as COMBINED_SOURCE_MARKERS,
    EXPLICIT_MANTIS_MARKERS as EXPLICIT_MANTIS_MARKERS,
    EXPLICIT_NOTION_MARKERS as EXPLICIT_NOTION_MARKERS,
    ISSUE_ANALYSIS_INTENT_KEYWORDS as ISSUE_ANALYSIS_INTENT_KEYWORDS,
    ISSUE_LIST_INTENT_KEYWORDS as ISSUE_LIST_INTENT_KEYWORDS,
    NOTION_QUERY_STOPWORDS as NOTION_QUERY_STOPWORDS,
)
from backend.intent.constraints import (
    build_keyword_terms as build_keyword_terms,
    build_similar_issue_search_text as build_similar_issue_search_text,
    expand_compound_search_term as expand_compound_search_term,
    extract_json_object as extract_json_object,
    extract_limit as extract_limit,
    extract_query_constraints as extract_query_constraints,
    extract_requested_limit as extract_requested_limit,
    has_constraint as has_constraint,
    has_targeting_constraint as has_targeting_constraint,
    legacy_filters_to_clauses as legacy_filters_to_clauses,
    merge_query_plan_constraints as merge_query_plan_constraints,
    plan_search_intent_with_model as plan_search_intent_with_model,
    resolve_result_limit as resolve_result_limit,
    rule_based_search_intent as rule_based_search_intent,
    should_run_notion_search as should_run_notion_search,
    should_run_retrieval as should_run_retrieval,
    should_use_intent_planner as should_use_intent_planner,
    split_project_search_terms as split_project_search_terms,
    term_is_covered_by_constraints as term_is_covered_by_constraints,
    wants_all_results as wants_all_results,
    wants_similar_issue_search as wants_similar_issue_search,
)
from backend.intent.context import (
    build_contextual_retrieval_text as build_contextual_retrieval_text,
    get_latest_user_text as get_latest_user_text,
    get_recent_user_texts as get_recent_user_texts,
    has_demonstrative_issue_reference as has_demonstrative_issue_reference,
    is_follow_up_question as is_follow_up_question,
    parse_ordinal_reference as parse_ordinal_reference,
    resolve_recent_created_at as resolve_recent_created_at,
    resolve_reference_from_context as resolve_reference_from_context,
    rewrite_follow_up_query_with_model as rewrite_follow_up_query_with_model,
)
from backend.intent.dates import (
    extract_date_field_constraints as extract_date_field_constraints,
    remove_date_field_phrases as remove_date_field_phrases,
)
from backend.intent.entities import (
    expression_is_excluded as expression_is_excluded,
    extract_excluded_issue_ids as extract_excluded_issue_ids,
    extract_explicit_keyword_filter_clauses as extract_explicit_keyword_filter_clauses,
    extract_issue_ids as extract_issue_ids,
    extract_mantis_keyword as extract_mantis_keyword,
    extract_people_constraints as extract_people_constraints,
    extract_screen_numbers as extract_screen_numbers,
    extract_status_constraints as extract_status_constraints,
    extract_unknown_customers as extract_unknown_customers,
    get_customer_alias_groups as get_customer_alias_groups,
    infer_person_role as infer_person_role,
    is_simple_greeting as is_simple_greeting,
    load_known_people as load_known_people,
    load_project_alias_groups as load_project_alias_groups,
)
from backend.intent.matching import (
    chroma_text_matches_constraints as chroma_text_matches_constraints,
    chroma_text_matches_created_at as chroma_text_matches_created_at,
    chroma_text_matches_date_fields as chroma_text_matches_date_fields,
    chroma_text_matches_screen_numbers as chroma_text_matches_screen_numbers,
    field_matches_screen_number as field_matches_screen_number,
    text_matches_constraints as text_matches_constraints,
    text_matches_issue_ids as text_matches_issue_ids,
    text_matches_people as text_matches_people,
    text_matches_screen_numbers as text_matches_screen_numbers,
    text_matches_status as text_matches_status,
    title_has_screen_number as title_has_screen_number,
)
from backend.llm import post_model_chat as post_model_chat


__all__ = [name for name in globals() if not name.startswith("_")]
