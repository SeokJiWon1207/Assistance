"""적용된 검색 조건의 표시 계약."""
from typing import Any, Dict, List
from backend.settings.values import STATUS_ALIASES, STATUS_MATCH_ALIASES

def display_constraints(constraints: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if constraints.get("customer_key"):
        items.append(
            {
                "id": "customer",
                "label": "고객사",
                "value": constraints["customer_key"],
                "values": list(dict.fromkeys([constraints["customer_key"], *constraints.get("customer_aliases", [])])),
            }
        )
    if constraints.get("platform_key"):
        items.append(
            {
                "id": "platform",
                "label": "플랫폼",
                "value": constraints["platform_key"],
                "values": list(dict.fromkeys([constraints["platform_key"], *constraints.get("platform_aliases", [])])),
            }
        )
    if constraints.get("excluded_platform_keys"):
        values = [str(value).upper() for value in constraints["excluded_platform_keys"]]
        items.append(
            {
                "id": "platform_exclude",
                "label": "플랫폼 제외",
                "value": ", ".join(values),
                "values": values,
            }
        )
    if constraints.get("excluded_customer_keys"):
        values = list(constraints["excluded_customer_keys"])
        items.append(
            {
                "id": "customer_exclude",
                "label": "고객사 제외",
                "value": ", ".join(values),
                "values": values,
            }
        )
    people = constraints.get("people", {}).get("names", [])
    if people:
        people_label = "공수 기입자" if constraints.get("people", {}).get("role") == "effort_person" else "담당자/인물"
        items.append({"id": "people", "label": people_label, "value": ", ".join(people), "values": people})
    elif constraints.get("unassigned_people"):
        items.append(
            {
                "id": "people_unassigned",
                "label": "담당자",
                "value": "미지정",
                "values": ["담당자 미지정"],
            }
        )
    if constraints.get("statuses"):
        status_values = list(
            dict.fromkeys(
                [
                    value
                    for status in constraints["statuses"]
                    for value in [status, *STATUS_ALIASES.get(status, []), *STATUS_MATCH_ALIASES.get(status, [])]
                ]
            )
        )
        items.append(
            {
                "id": "status",
                "label": "상태",
                "value": ", ".join(constraints["statuses"]),
                "values": status_values,
            }
        )
    if constraints.get("excluded_statuses"):
        values = list(constraints["excluded_statuses"])
        items.append(
            {
                "id": "status_exclude",
                "label": "상태 제외",
                "value": ", ".join(values),
                "values": values,
            }
        )
    if constraints.get("issue_ids"):
        issue_ids = list(map(str, constraints["issue_ids"]))
        items.append({"id": "issue", "label": "이슈 ID", "value": ", ".join(issue_ids), "values": issue_ids})
    if constraints.get("screen_numbers"):
        items.append(
            {
                "id": "screen",
                "label": "화면번호",
                "value": ", ".join(constraints["screen_numbers"]),
                "values": constraints["screen_numbers"],
            }
        )
    for field_name, field_value in constraints.get("date_fields", {}).items():
        items.append(
            {
                "id": "date",
                "label": field_name,
                "value": field_value,
                "values": [field_name, field_value],
            }
        )
    delivered_at = constraints.get("delivered_at") or {}
    if delivered_at.get("from") and delivered_at.get("to"):
        items.append({"id": "delivered_at", "label": "고객사전달일",
                      "value": f"{delivered_at['from']}~{delivered_at['to']}",
                      "values": [delivered_at["from"], delivered_at["to"]]})
    created_at = constraints.get("created_at") or {}
    if created_at.get("from") and created_at.get("to"):
        items.append(
            {
                "id": "created_at",
                "label": "등록일",
                "value": f"{created_at['from']}~{created_at['to']}",
                "values": [created_at["from"], created_at["to"]],
            }
        )
    return items
