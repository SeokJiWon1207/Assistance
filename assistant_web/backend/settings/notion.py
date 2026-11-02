"""Notion 환경 설정과 헤더. 연결된 DB 탐색은 notion.discovery가 맡는다."""
import os
import re
from typing import Any, Dict, List


NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_DEFAULT_VERSION = "2022-06-28"


def parse_notion_database_ids() -> List[str]:
    raw_ids = (
        os.getenv("NOTION_DATABASE_IDS")
        or os.getenv("NOTION_DATABASE_ID")
        or os.getenv("NOTION_ISSUE_DB_ID")
        or ""
    )
    database_ids = [
        value.strip().replace("-", "")
        for value in re.split(r"[,\n]", raw_ids)
        if value.strip()
    ]
    return list(dict.fromkeys(database_ids))


def get_notion_token() -> str:
    return os.getenv("NOTION_API_TOKEN") or os.getenv("NOTION_TOKEN") or ""


def notion_unavailable_reason(settings: Dict[str, Any]) -> str:
    if not settings.get("token"):
        return "NOTION_API_TOKEN이 설정되지 않았습니다."
    if settings.get("database_discovery_error"):
        return str(settings["database_discovery_error"])
    if settings.get("database_source") == "discovered":
        return "API 키의 integration에 연결된 Notion DB가 없습니다. DB의 연결(Connections)에 integration을 추가하세요."
    return "NOTION_DATABASE_IDS에 조회할 DB가 없습니다."


def get_notion_headers() -> Dict[str, str]:
    # DB 자동 조회 요청도 이 헤더를 쓰므로 여기서 get_notion_settings를 부르지 않는다.
    return {
        "Authorization": f"Bearer {get_notion_token()}",
        "Notion-Version": os.getenv("NOTION_API_VERSION") or NOTION_DEFAULT_VERSION,
        "Content-Type": "application/json",
    }


def get_configured_notion_settings() -> Dict[str, Any]:
    """환경에 명시된 설정만 반환한다. DB 발견이나 네트워크 요청은 하지 않는다."""
    return {
        "token": get_notion_token(),
        "database_ids": parse_notion_database_ids(),
        "database_source": "configured",
        "database_discovery_error": "",
        "database_discovery_error_kind": "",
        "version": os.getenv("NOTION_API_VERSION") or NOTION_DEFAULT_VERSION,
    }
