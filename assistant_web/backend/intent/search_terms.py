"""검색어·유사 검색 판정의 기존 import 경로."""
from backend.intent.keyword_terms import (
    __all__ as __all__,
    KEYWORD_ALIAS_GROUPS as KEYWORD_ALIAS_GROUPS,
    keyword_term_aliases as keyword_term_aliases,
    text_matches_keyword_terms as text_matches_keyword_terms,
    term_is_covered_by_constraints as term_is_covered_by_constraints,
    remove_list_display_instruction as remove_list_display_instruction,
    _REPORT_REQUEST as _REPORT_REQUEST,
    remove_report_request as remove_report_request,
    without_report_request_terms as without_report_request_terms,
    build_keyword_terms as build_keyword_terms,
    replacement_topic_terms as replacement_topic_terms,
    split_project_search_terms as split_project_search_terms,
    expand_compound_search_term as expand_compound_search_term,
    build_similar_issue_search_text as build_similar_issue_search_text,
    _COUNT_UNIT as _COUNT_UNIT,
    _COUNT_WORDS as _COUNT_WORDS,
    _NUMERIC_LIMIT_PATTERN as _NUMERIC_LIMIT_PATTERN,
    _KOREAN_LIMIT_PATTERN as _KOREAN_LIMIT_PATTERN,
    _BARE_KOREAN_LIMIT_PATTERN as _BARE_KOREAN_LIMIT_PATTERN,
    extract_requested_limit as extract_requested_limit,
    wants_all_results as wants_all_results,
    resolve_result_limit as resolve_result_limit,
    extract_limit as extract_limit,
)
from backend.intent.constraints import (
    wants_similar_issue_search as wants_similar_issue_search,
)
