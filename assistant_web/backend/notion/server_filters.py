"""Notion 스키마에 맞춘 서버 필터와 조회 범위를 구성한다."""
from backend.notion import read_api
from backend.notion import schema as notion_schema
from backend.notion import transport
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple
import requests
from fastapi import HTTPException
from backend.utils.text import normalize_for_match
from backend.notion.read_api import fetch_notion_users as fetch_notion_users, fetch_notion_database_schema as fetch_notion_database_schema
from backend.notion.transport import notion_post_json as notion_post_json
from backend.settings.notion import NOTION_API_BASE
from backend.notion.schema import notion_property_schema as notion_property_schema
from backend.notion.property_roles import CUSTOMER_NAMES, DEPLOYMENT_NAMES, DUE_NAMES, REGISTRATION_NAMES, responsible_properties, role_properties
from backend.query_filters import values_for_clause, normalize_filter_clauses


_NOTION_USER_ID_HINTS: Dict[str, str] = {}


def notion_search_max_pages(
    constraints: Dict[str, Any],
    *,
    server_filter_applied: bool = False,
    require_complete: bool = False,
) -> int:
    # Structured filters and grouped summaries must scan the complete configured
    # databases. 목록 조회는 서버 필터가 실제 적용된 뒤에만 상한을 낮춘다.
    # 스키마 조회 실패 등으로 필터가 적용되지 않았다면 기존 전체 스캔으로
    # 되돌아가 최근 수정 행만 보고 0건으로 단정하는 일을 막는다.
    has_structured_filter = bool(
        constraints.get("status")
        or constraints.get("excluded_statuses")
        or constraints.get("unassigned_owner")
        or constraints.get("owner")
        or constraints.get("group_by")
        or constraints.get("common_filter_clauses")
    )
    if require_complete:
        return 30
    if has_structured_filter:
        return 5 if server_filter_applied else 30
    return 3


def notion_clause_server_covered(
    clause: Dict[str, Any],
    server_covered_fields: set[str],
    *,
    target_database_label: str,
    exact_title: str,
) -> bool:
    """서버 필터가 로컬 후처리 조건을 빠짐없이 포함하는지 확인한다."""
    field = str(clause.get("field") or "")
    operator = str(clause.get("operator") or "")
    if field in {"status", "customer"}:
        return field in server_covered_fields and operator in {"equals", "in"}
    if field == "owner":
        return field in server_covered_fields and operator in {"equals", "in"}
    if field in {"created_at", "deployed_at"}:
        return field in server_covered_fields and operator in {"between", "equals", "gte", "lte"}
    if field == "database":
        return bool(target_database_label)
    if field == "title":
        return bool(exact_title)
    return False


NOTION_MAX_FILTER_LEVELS = 2


def notion_filter_levels(api_filter: Optional[Dict[str, Any]]) -> int:
    """and/or 복합 필터가 겹친 최대 단계 수를 센다."""
    if not isinstance(api_filter, dict):
        return 0
    for key in ("and", "or"):
        if key in api_filter:
            return 1 + max((notion_filter_levels(item) for item in api_filter[key]), default=0)
    return 0


def notion_filter_fits(api_filter: Optional[Dict[str, Any]]) -> bool:
    return notion_filter_levels(api_filter) <= NOTION_MAX_FILTER_LEVELS


def notion_and_filter(*filters: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """필터를 and로 묶되 안쪽 and는 펼친다. 의미는 같고 중첩 단계만 줄어든다."""
    parts: List[Dict[str, Any]] = []

    def add(api_filter: Optional[Dict[str, Any]]) -> None:
        if not api_filter:
            return
        if set(api_filter) == {"and"}:
            for item in api_filter["and"]:
                add(item)
        else:
            parts.append(api_filter)

    for api_filter in filters:
        add(api_filter)
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else {"and": parts}


def notion_title_query_filter(
    title_filter: Dict[str, Any],
    condition_filter: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """제목 필터에 나머지 조건을 붙인다. 중첩 한도를 넘으면 제목 필터만 보낸다.

    서버가 돌려준 페이지는 모든 조건으로 다시 로컬 검사하므로 결과는 같고,
    조회량만 늘어난다.
    """
    combined = notion_and_filter(title_filter, condition_filter)
    return combined if combined is not None and notion_filter_fits(combined) else title_filter


def notion_database_status_filter(
    database_id: str,
    status: str,
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Return whether a DB has status-like select fields and an exact API filter.

    When the requested status is absent from every status-like option list, the
    caller can safely skip that database. If schema inspection fails, return
    ``False`` so the existing local-scan fallback remains available.
    """
    if not status:
        return False, None
    try:
        properties = read_api.fetch_notion_database_schema(database_id).get("properties", {})
    except (HTTPException, requests.RequestException):
        return False, None
    target = normalize_for_match(status)
    filters: List[Dict[str, Any]] = []
    supported = False
    for prop_name, prop in properties.items():
        prop_type = prop.get("type")
        if prop_type not in {"status", "select"} or not any(
            key in normalize_for_match(prop_name) for key in ("상태", "status", "진행")
        ):
            continue
        supported = True
        for option in (prop.get(prop_type) or {}).get("options", []):
            option_name = str(option.get("name") or "")
            if normalize_for_match(option_name) == target:
                filters.append({"property": prop_name, prop_type: {"equals": option_name}})
    if not filters:
        return supported, None
    return True, filters[0] if len(filters) == 1 else {"or": filters}


def notion_database_customer_filter(
    database_id: str,
    clauses: List[Dict[str, Any]],
    aliases: List[str],
) -> Optional[Dict[str, Any]]:
    """고객사 속성이 확인된 DB에만 안전한 서버 필터를 만든다."""
    from backend.customer_identity import customer_value_matches
    customer_clauses = [
        clause for clause in normalize_filter_clauses(clauses)
        if clause["field"] == "customer" and clause["operator"] in {"equals", "in"}
    ]
    if not customer_clauses:
        return None
    properties = notion_schema.notion_property_schema(database_id)
    if role_properties(properties, CUSTOMER_NAMES, {'rich_text'}):
        # 텍스트 고객사 속성의 정규화 일치를 select 필터로 좁힐 수 없다.
        return None
    fields = role_properties(
        properties,
        CUSTOMER_NAMES,
        {"select", "multi_select"},
    )
    if not fields:
        return None

    clause_filters: List[Dict[str, Any]] = []
    for clause in customer_clauses:
        requested = list(dict.fromkeys([
            *values_for_clause(clause.get("value")),
            *[str(alias) for alias in aliases if str(alias).strip()],
        ]))
        filters: List[Dict[str, Any]] = []
        for name in fields:
            prop_type = str(properties[name].get("type") or "")
            if prop_type in {"select", "multi_select"}:
                options = (properties[name].get(prop_type) or {}).get("options", [])
                for option in options:
                    option_name = str(option.get("name") or "")
                    if not option_name or not customer_value_matches(option_name, requested):
                        continue
                    operator = "equals" if prop_type == "select" else "contains"
                    filters.append({"property": name, prop_type: {operator: option_name}})
        if not filters:
            return None
        # 속성 값이 비어 있으면 제목·메모 등 기존 검색 텍스트가 근거가 된다.
        empty = [{'property': name, str(properties[name]['type']): {'is_empty': True}} for name in fields]
        filters.append(empty[0] if len(empty) == 1 else {'and': empty})
        clause_filters.append(filters[0] if len(filters) == 1 else {"or": filters})
    return clause_filters[0] if len(clause_filters) == 1 else {"and": clause_filters}


def notion_customer_filter_needs_postcheck(api_filter: Dict[str, Any]) -> bool:
    return (any(isinstance(value, dict) and value.get('is_empty') is True for value in api_filter.values())
            or any(notion_customer_filter_needs_postcheck(child)
                   for key in ('and', 'or') for child in api_filter.get(key, [])))


def notion_database_owner_filter(
    database_id: str,
    owner: str,
) -> tuple[bool, Optional[Dict[str, Any]]]:
    """담당자 people 필드가 있으면 사용자 ID로 서버 필터를 만든다.

    첫 반환값이 참이고 필터가 없으면 해당 DB에는 관련자 필드가 없어
    안전하게 건너뛸 수 있다. relation 기반 담당자는 로컬 해석을 유지한다.
    """
    if not owner:
        return False, None
    properties = notion_schema.notion_property_schema(database_id)
    fields = responsible_properties(properties)
    if not fields:
        # 빈 스키마는 조회 실패/테스트 대역일 수 있으므로 로컬 검사로
        # 폴백한다. 실제 속성이 있는데 관련자 필드가 없는 DB만 건너뛴다.
        return bool(properties), None
    people_fields = [name for name in fields if properties[name].get("type") == "people"]
    if not people_fields or len(people_fields) != len(fields):
        # 다른 담당자 속성에만 배정된 항목을 people 필터가 먼저 제외하지 않게 한다.
        return False, None
    normalized_owner = normalize_for_match(owner)
    user_id = _NOTION_USER_ID_HINTS.get(normalized_owner, "") or next(
        (
            user_id
            for user_name, user_id in read_api.fetch_notion_users()
            if normalize_for_match(user_name) == normalized_owner
        ),
        "",
    )
    if not user_id:
        # users.list는 게스트/제한 멤버를 빠뜨릴 수 있지만, 페이지의 people
        # 값에는 이름과 ID가 함께 들어온다. 최근 페이지 한 번만 읽어 ID를
        # 찾아 두면 이후 모든 DB에 정확한 서버 필터를 적용할 수 있다.
        response = transport.notion_post_json(
            f"{NOTION_API_BASE}/databases/{database_id}/query",
            {
                "page_size": 100,
                "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
            },
            timeout=20,
            retries=1,
        )
        if response.status_code < 400:
            for raw_page in response.json().get("results", []):
                raw_properties = raw_page.get("properties") or {}
                for name in people_fields:
                    for person in (raw_properties.get(name) or {}).get("people") or []:
                        person_name = str(person.get("name") or "").strip()
                        person_id = str(person.get("id") or "").strip()
                        if (
                            person_id
                            and normalize_for_match(person_name) == normalized_owner
                        ):
                            user_id = person_id
                            break
                    if user_id:
                        break
                if user_id:
                    break
    if not user_id:
        return False, None
    _NOTION_USER_ID_HINTS[normalized_owner] = user_id
    filters = [
        {"property": name, "people": {"contains": user_id}}
        for name in people_fields
    ]
    return True, filters[0] if len(filters) == 1 else {"or": filters}


def notion_database_date_filter(database_id: str, clause: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """정확한 날짜 속성으로 먼저 좁히고, 우선순위는 행 후처리에서 검사한다."""
    properties = notion_schema.notion_property_schema(database_id)
    names = DUE_NAMES if clause["field"] == "due_at" else DEPLOYMENT_NAMES
    fields = role_properties(properties, names, {"date"})
    values = values_for_clause(clause.get("value"))
    if not fields:
        return None
    filters: List[Dict[str, Any]]
    if clause["field"] == "due_at":
        # Notion의 기간 필터는 시작일을 볼 수 있으므로 마감(끝 날짜)의
        # 하한을 서버에 강제하지 않는다. 최종 비교는 end 기준 로컬 검사다.
        bound = values[1] if clause["operator"] == "between" else values[0]
        condition = {"is_not_empty": True} if clause["operator"] == "gte" else {"on_or_before": bound}
        filters = [{"property": name, "date": condition} for name in fields]
    elif clause["operator"] == "between":
        filters = [{"and": [
            {"property": name, "date": {"on_or_after": values[0]}},
            {"property": name, "date": {"on_or_before": values[1]}},
        ]} for name in fields]
    else:
        operators = {"equals": "equals", "gte": "on_or_after", "lte": "on_or_before"}
        operator = operators.get(clause["operator"])
        if not operator:
            return None
        filters = [{"property": name, "date": {operator: values[0]}} for name in fields]
    return filters[0] if len(filters) == 1 else {"or": filters}


def notion_database_created_sort(database_id: str) -> tuple[Dict[str, str], bool]:
    fields = role_properties(notion_schema.notion_property_schema(database_id), REGISTRATION_NAMES, {"date"})
    if fields:
        # 빈 등록일은 생성 시각으로 대체하므로 한 속성의 API 정렬만으로
        # 전체 상위 N건을 확정할 수 없다. 이 경우 기존 전체 후보 검사를 유지한다.
        return {"property": fields[0], "direction": "descending"}, False
    return {"timestamp": "created_time", "direction": "descending"}, True


def notion_database_created_filter(
    database_id: str, clause: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, str], bool]:
    """등록일 속성을 우선 사용하고 없으면 페이지 생성 시각으로 좁힌다."""
    properties = notion_schema.notion_property_schema(database_id)
    fields = role_properties(properties, REGISTRATION_NAMES, {"date"})
    values = values_for_clause(clause.get("value"))
    operator = clause.get("operator")

    def range_condition(start: str, end: str) -> Dict[str, str]:
        exclusive_end = (date.fromisoformat(end) + timedelta(days=1)).isoformat()
        return {"on_or_after": start, "before": exclusive_end}

    if operator == "between" and len(values) == 2:
        condition = range_condition(values[0], values[1])
    elif operator == "equals" and values:
        condition = range_condition(values[0], values[0])
    elif operator == "gte" and values:
        condition = {"on_or_after": values[0]}
    elif operator == "lte" and values:
        condition = {"before": (date.fromisoformat(values[0]) + timedelta(days=1)).isoformat()}
    else:
        raise ValueError("unsupported created_at filter")

    def date_filter(target: Dict[str, str], value_key: str, bounds: Dict[str, str]) -> Dict[str, Any]:
        # Notion 날짜 조건 하나에는 비교 연산자를 하나만 넣을 수 있다.
        parts = [{**target, value_key: {operator: value}} for operator, value in bounds.items()]
        return parts[0] if len(parts) == 1 else {"and": parts}

    if fields:
        filters = [
            date_filter({"property": name}, "date", condition)
            for name in fields
        ]
        api_filter = filters[0] if len(filters) == 1 else {"or": filters}
        return api_filter, {"property": fields[0], "direction": "descending"}, len(fields) == 1
    timestamp_condition = {
        key: f"{value}T00:00:00+09:00"
        for key, value in condition.items()
    }
    return (
        date_filter({"timestamp": "created_time"}, "created_time", timestamp_condition),
        {"timestamp": "created_time", "direction": "descending"},
        True,
    )
