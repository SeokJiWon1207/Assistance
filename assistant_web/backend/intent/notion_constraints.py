"""명단 조회가 필요한 기존 한 인수 Notion 해석 진입점."""
from typing import Any, Dict
from backend.sources.people_catalog import load_known_people
from backend.query.notion_constraints import (
    parse_notion_constraints, trim_owner_adnominal as trim_owner,
    known_notion_owner as find_owner,
)


def extract_notion_constraints(text: str) -> Dict[str, Any]:
    return parse_notion_constraints(text, known_people=load_known_people())


def trim_owner_adnominal(candidate: str, following_text: str = "") -> str:
    return trim_owner(candidate, following_text, known_people=load_known_people())


def known_notion_owner(text: str) -> str:
    return find_owner(text, known_people=load_known_people())
