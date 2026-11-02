"""이전 설정 import 경로와 Mantis 연결 설정.

정적 값·경로·고객사 목록·Notion 설정/통신 구현은 각 소유자에서 재내보낸다.
고객사 JSON은 앱/CLI 준비 단계에서 명시적으로 초기화한다.
"""
import os
from typing import Dict
from fastapi import HTTPException

from backend.settings.values import (
    STOPWORDS as STOPWORDS,
    KOREAN_COUNT_WORDS as KOREAN_COUNT_WORDS,
    DEFAULT_CUSTOMER_ALIASES as DEFAULT_CUSTOMER_ALIASES,
    AMBIGUOUS_DYNAMIC_CUSTOMER_ALIASES as AMBIGUOUS_DYNAMIC_CUSTOMER_ALIASES,
    PLATFORM_ALIASES as PLATFORM_ALIASES,
    STATUS_ALIASES as STATUS_ALIASES,
    MANTIS_STATUS_NAMES as MANTIS_STATUS_NAMES,
    MANTIS_OPEN_STATUSES as MANTIS_OPEN_STATUSES,
    STATUS_MATCH_ALIASES as STATUS_MATCH_ALIASES,
    PENDING_WORK_QUERY_PATTERN as PENDING_WORK_QUERY_PATTERN,
    NON_CLOSED_STATUS_PATTERNS as NON_CLOSED_STATUS_PATTERNS,
    MANTIS_DATE_CUSTOM_FIELDS as MANTIS_DATE_CUSTOM_FIELDS,
    MANTIS_DATE_FIELD_ALIASES as MANTIS_DATE_FIELD_ALIASES,
    PERSON_ROLE_ALIASES as PERSON_ROLE_ALIASES,
    PERSON_BLOCKLIST as PERSON_BLOCKLIST,
)

from backend.settings.paths import (
    BASE_DIR as BASE_DIR,
    STATIC_DIR as STATIC_DIR,
    APP_DIR as APP_DIR,
    WORKSPACE_DIR as WORKSPACE_DIR,
    CONFIG_DIR as CONFIG_DIR,
    RUNTIME_DIR as RUNTIME_DIR,
    CHROMA_DB_PATH as CHROMA_DB_PATH,
    CHROMA_PERSIST_DIR as CHROMA_PERSIST_DIR,
    MANTIS_INDEX_DB_PATH as MANTIS_INDEX_DB_PATH,
    AUDIT_DB_PATH as AUDIT_DB_PATH,
)
from backend.settings.catalogs import (
    load_customer_aliases as load_customer_aliases,
    load_customer_directory as load_customer_directory,
    load_customer_project_display_names as load_customer_project_display_names,
    CUSTOMER_ALIASES as CUSTOMER_ALIASES,
    CUSTOMER_DIRECTORY as CUSTOMER_DIRECTORY,
    CUSTOMER_PROJECT_DISPLAY_NAMES as CUSTOMER_PROJECT_DISPLAY_NAMES,
    initialize_customer_catalogs as initialize_customer_catalogs,
)
from backend.settings.notion import (
    NOTION_API_BASE as NOTION_API_BASE,
    NOTION_DEFAULT_VERSION as NOTION_DEFAULT_VERSION,
    parse_notion_database_ids as parse_notion_database_ids,
    get_notion_token as get_notion_token,
    notion_unavailable_reason as notion_unavailable_reason,
    get_notion_headers as get_notion_headers,
    get_configured_notion_settings as get_configured_notion_settings,
)
from backend.notion.transport import (
    notion_request_slot as notion_request_slot,
    notion_post_json as notion_post_json,
)
from backend.notion.discovery import (
    NOTION_DISCOVERY_MAX_PAGES as NOTION_DISCOVERY_MAX_PAGES,
    NOTION_DISCOVERY_FAILURE_TTL_SECONDS as NOTION_DISCOVERY_FAILURE_TTL_SECONDS,
    notion_discovery_ttl_seconds as notion_discovery_ttl_seconds,
    notion_discovery_max_databases as notion_discovery_max_databases,
    discover_notion_database_ids as discover_notion_database_ids,
    notion_is_configured as notion_is_configured,
    resolve_notion_settings as resolve_notion_settings,
)

get_notion_settings = resolve_notion_settings


def get_mantis_settings() -> Dict[str, str]:
    mantis_url = os.getenv("MANTIS_API_URL", "").strip()
    mantis_token = os.getenv("MANTIS_API_TOKEN", "").strip()
    if not mantis_url:
        raise HTTPException(status_code=503, detail="Mantis API 주소가 비어 있습니다. .env의 MANTIS_API_URL을 확인해 주세요.")
    if not mantis_token:
        raise HTTPException(status_code=503, detail="Mantis API 토큰이 비어 있습니다. .env의 MANTIS_API_TOKEN을 입력해 주세요.")
    return {"url": mantis_url, "token": mantis_token}



def get_mantis_headers() -> Dict[str, str]:
    settings = get_mantis_settings()
    return {
        "Authorization": settings["token"],
        "Content-Type": "application/json",
    }
