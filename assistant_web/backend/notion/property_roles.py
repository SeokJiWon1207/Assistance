"""업무 담당자·기여자·팀과 날짜의 의미를 속성 이름/타입으로 구분한다."""

import re
from functools import lru_cache
from typing import Any, Dict, List

from backend.common import normalize_for_match


@lru_cache(maxsize=2048)
def property_key(name: str) -> str:
    return normalize_for_match(re.sub(r"\([^)]*\)", "", name))


OWNER_NAMES = ("이슈할당", "주담당자", "담당자", "담당", "owner", "ownership", "assignee")
CONTRIBUTOR_NAMES = ("공동 작업자", "공헌", "Contributors", "기여자", "참여")
DUE_NAMES = ("마감일", "기한", "마감", "due date", "due", "전달목표일", "개발완료목표일", "일정", "날짜", "date")
DEPLOYMENT_NAMES = ("운영반영", "운영반영일", "배포일")
REGISTRATION_NAMES = ("등록일", "접수일", "생성일", "작성일", "registration date", "created date")
CUSTOMER_NAMES = ("고객사", "고객", "customer", "client")


def role_properties(
    properties: Dict[str, Dict[str, Any]], names: tuple[str, ...], types: set[str],
) -> List[str]:
    signature = tuple((name, str(prop.get("type", ""))) for name, prop in properties.items())
    return list(_role_properties_cached(signature, names, tuple(sorted(types))))


@lru_cache(maxsize=256)
def _role_properties_cached(signature: tuple[tuple[str, str], ...], names: tuple[str, ...], types: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(name for key in names for name, kind in signature
                 if property_key(name) == property_key(key) and kind in types)


def owner_properties(properties: Dict[str, Dict[str, Any]]) -> List[str]:
    # 팀/부서 rollup, 직전 담당 파트, 작성자는 개인 담당자가 아니다.
    primary = role_properties(properties, OWNER_NAMES, {"people", "relation", "rich_text", "select"})
    additional = [name for name, prop in properties.items()
                  if prop.get("type") in {"people", "relation"}
                  and property_key(name).endswith(("담당자", "담당"))
                  and not any(word in property_key(name) for word in ("직전", "이전", "과거", "팀", "부서", "본부", "파트"))]
    return list(dict.fromkeys([*primary, *additional]))


def responsible_properties(properties: Dict[str, Dict[str, Any]]) -> List[str]:
    # 담당자 필드 자체가 없는 문서 DB만 기여자를 검색용 관련자로 사용한다.
    # 빈 담당자를 기여자로 덮어쓰지 않으며 표시에는 역할을 함께 전달한다.
    return owner_properties(properties) or role_properties(properties, CONTRIBUTOR_NAMES, {"people"})


def date_property(properties: Dict[str, Dict[str, Any]], names: tuple[str, ...]) -> str:
    candidates = role_properties(properties, names, {"date"})
    return next((name for name in candidates if properties[name].get("date")), "")
