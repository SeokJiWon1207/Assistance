"""공통 함수의 기존 import 경로. 구현은 역할별 모듈이 소유한다."""

import re as re
from datetime import date as date, datetime as datetime, timedelta as timedelta, timezone as timezone
from functools import lru_cache as lru_cache
from typing import Any as Any, Dict as Dict, List as List, Optional as Optional

from backend.utils.text import (
    compact_text as compact_text,
    quoted_value_span as quoted_value_span,
    extract_quoted_value as extract_quoted_value,
    remove_quoted_value as remove_quoted_value,
    normalize_for_match as normalize_for_match,
    normalize_search_token as normalize_search_token,
    has_lookup_request as has_lookup_request,
    is_control_search_term as is_control_search_term,
    text_matches_any_alias as text_matches_any_alias,
    COUNT_EXPRESSION_PATTERN as COUNT_EXPRESSION_PATTERN,
    PERIOD_EXPRESSION_PATTERN as PERIOD_EXPRESSION_PATTERN,
    LOOKUP_REQUEST_PATTERN as LOOKUP_REQUEST_PATTERN,
)
from backend.utils.numbers import (
    clamp_float as clamp_float,
    clamp_int as clamp_int,
)
from backend.utils.dates import (
    parse_date as parse_date,
    format_mantis_custom_date as format_mantis_custom_date,
)
from backend.customer_identity import (
    customer_alias_owner as customer_alias_owner,
    customer_project_owner as customer_project_owner,
    text_matches_customer_aliases as text_matches_customer_aliases,
    add_customer_alias as add_customer_alias,
    derive_customer_aliases_from_project as derive_customer_aliases_from_project,
)
from backend.domain.people import (
    extract_from_names as extract_from_names,
    is_valid_person_name as is_valid_person_name,
    is_valid_handler_name as is_valid_handler_name,
    strip_korean_particle as strip_korean_particle,
    text_contains_person_name as text_contains_person_name,
    KOREAN_PARTICLE_PATTERN as KOREAN_PARTICLE_PATTERN,
)
from backend.sources.chroma_document import (
    extract_chroma_field as extract_chroma_field,
    extract_chroma_date as extract_chroma_date,
    extract_issue_id_from_text as extract_issue_id_from_text,
    extract_title_from_chroma_text as extract_title_from_chroma_text,
    extract_project_from_chroma_text as extract_project_from_chroma_text,
    normalize_chroma_issue_document as normalize_chroma_issue_document,
)
from backend.mantis_status import (
    display_mantis_status_label as display_mantis_status_label,
    MANTIS_STATUS_DISPLAY_LABELS as MANTIS_STATUS_DISPLAY_LABELS,
)
from backend.settings.values import (
    AMBIGUOUS_DYNAMIC_CUSTOMER_ALIASES as AMBIGUOUS_DYNAMIC_CUSTOMER_ALIASES,
    KOREAN_COUNT_WORDS as KOREAN_COUNT_WORDS,
    MANTIS_DATE_CUSTOM_FIELDS as MANTIS_DATE_CUSTOM_FIELDS,
    PERSON_BLOCKLIST as PERSON_BLOCKLIST,
    PERSON_ROLE_ALIASES as PERSON_ROLE_ALIASES,
    PLATFORM_ALIASES as PLATFORM_ALIASES,
    STATUS_ALIASES as STATUS_ALIASES,
    STOPWORDS as STOPWORDS,
)
from backend.config import (
    CUSTOMER_ALIASES as CUSTOMER_ALIASES,
)
