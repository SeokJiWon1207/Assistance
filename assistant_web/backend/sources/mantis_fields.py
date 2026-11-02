"""Mantis 원문의 필드·날짜·표시·검색문 정규화. 저장소와 HTTP를 호출하지 않는다."""
import re
from typing import Any, Dict, List, Optional
from backend.utils.dates import format_mantis_custom_date
from backend.settings.values import MANTIS_DATE_CUSTOM_FIELDS
from backend.sources.mantis_evidence import matching_issue_evidence
from backend.effort_fields import issue_effort_people
from backend.display import customer_name

def get_issue_custom_field_value(issue: Dict[str, Any], field_name: str) -> str:
    for custom_field in issue.get("custom_fields", []):
        if custom_field.get("field", {}).get("name") == field_name:
            return str(custom_field.get("value", "")).strip()
    return ""


def mantis_date_search_variants(value: Any) -> str:
    formatted = format_mantis_custom_date(value)
    raw_value = str(value or "").strip()
    variants = [raw_value, formatted]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", formatted):
        year, month, day = formatted.split("-")
        variants.extend(
            [
                f"{year}{month}{day}",
                f"{year}년 {int(month)}월 {int(day)}일",
                f"{year}년 {int(month)}월",
            ]
        )
    return " ".join(dict.fromkeys(variant for variant in variants if variant))


def mantis_enum_label(value: Any, default: str = "") -> str:
    if isinstance(value, dict):
        return str(value.get("name") or value.get("label") or default)
    return str(value or default)


def normalize_issue(issue: Dict[str, Any], *, search_terms: Optional[List[str]] = None, evidence_query: Optional[str] = None) -> Dict[str, Any]:
    media_type = "기타/미상"
    screen_number = "미상"
    work_subject = "미상"
    effort_fields: Dict[str, str] = {}
    date_fields: Dict[str, str] = {}
    resolution_fields: Dict[str, str] = {}
    for custom_field in issue.get("custom_fields", []):
        field_name = custom_field.get("field", {}).get("name")
        field_value = custom_field.get("value", "")
        if field_name == "매체구분":
            media_type = field_value or media_type
        elif field_name == "화면번호":
            screen_number = field_value or screen_number
        elif field_name == "작업주체":
            work_subject = field_value or work_subject
        elif field_name in MANTIS_DATE_CUSTOM_FIELDS:
            date_fields[str(field_name)] = format_mantis_custom_date(field_value)
        elif field_name and "공수" in field_name:
            effort_fields[str(field_name)] = str(field_value)
        elif field_name in {"사유 및 원인", "조치 및 개선"}:
            resolution_fields[str(field_name)] = str(field_value or "")
    if screen_number == "미상":
        title_screen = re.match(r"^\s*\[\s*(\d{3,5})\s*\]", str(issue.get("summary") or ""))
        if title_screen:
            screen_number = title_screen.group(1)

    notes = [
        {
            "id": note.get("id"),
            "reporter": (note.get("reporter") or {}).get("real_name", "미상"),
            "created_at": str(note.get("created_at", ""))[:19],
            "updated_at": str(note.get("updated_at", ""))[:19],
            "text": str(note.get("text", ""))[:2400],
        }
        for note in (issue.get("notes") or [])[-12:]
        if note.get("text")
    ]
    history = [
        {
            "created_at": str(item.get("created_at", ""))[:19],
            "user": (item.get("user") or {}).get("real_name", "미상"),
            "field": (item.get("field") or {}).get("label") or (item.get("field") or {}).get("name") or "",
            "message": str(item.get("message") or ""),
            "change": str(item.get("change") or ""),
        }
        for item in (issue.get("history") or [])[-40:]
    ]

    from backend.sources.mantis_answer_evidence import build_answer_evidence

    related_issue_ids = []
    relationships = issue.get("relationships")
    for relationship in relationships if isinstance(relationships, list) else []:
        if not isinstance(relationship, dict):
            continue
        kind, related = relationship.get("type"), relationship.get("issue")
        if not isinstance(kind, dict) or kind.get("name") != "related-to" or not isinstance(related, dict):
            continue
        related_id = related.get("id")
        if (type(related_id) is int and related_id > 0 and related_id != issue.get("id")
                and related_id not in related_issue_ids):
            related_issue_ids.append(related_id)

    return {
        **({"answer_evidence": build_answer_evidence(issue, evidence_query)} if evidence_query else {}),
        "id": issue.get("id"),
        "related_issue_ids": related_issue_ids,
        "project": (issue.get("project") or {}).get("name", "미상"),
        "summary": issue.get("summary", "제목 없음"),
        "status": mantis_enum_label(issue.get("status"), "상태 미상"),
        "status_label": (issue.get("status") or {}).get("label", "") if isinstance(issue.get("status"), dict) else "",
        "priority": mantis_enum_label(issue.get("priority"), ""),
        "severity": mantis_enum_label(issue.get("severity"), ""),
        "priority_label": (issue.get("priority") or {}).get("label", "") if isinstance(issue.get("priority"), dict) else "",
        "severity_label": (issue.get("severity") or {}).get("label", "") if isinstance(issue.get("severity"), dict) else "",
        "media_type": media_type,
        "screen_number": screen_number,
        "work_subject": work_subject,
        "effort_fields": effort_fields,
        "date_fields": date_fields,
        "resolution_fields": resolution_fields,
        "reporter": (issue.get("reporter") or {}).get("real_name", "미상"),
        "handler": (issue.get("handler") or {}).get("real_name", "담당자 미지정"),
        "created_at": str(issue.get("created_at", ""))[:10],
        "updated_at": str(issue.get("updated_at", ""))[:10],
        "description": str(issue.get("description", ""))[:2400],
        "additional_information": str(issue.get("additional_information") or "")[:2400],
        "notes": notes,
        "history": history,
        **({"matching_evidence": matching_issue_evidence(issue, search_terms)} if search_terms else {}),
    }


def issue_created_year_month(issue: Dict[str, Any]) -> str:
    created_at = str(issue.get("created_at") or "")[:10]
    if re.match(r"\d{4}-\d{2}-\d{2}", created_at):
        return created_at[:7]
    return ""


def issue_to_search_text(issue: Dict[str, Any]) -> str:
    custom_text_parts = []
    for field in issue.get("custom_fields", []):
        field_name = str(field.get("field", {}).get("name", ""))
        field_value = field.get("value", "")
        if field_name in MANTIS_DATE_CUSTOM_FIELDS:
            custom_text_parts.append(f"{field_name} {mantis_date_search_variants(field_value)}")
        else:
            custom_text_parts.append(f"{field_name} {field_value}")
    custom_text = " ".join(custom_text_parts)
    notes_text = " ".join(str(note.get("text") or "") for note in (issue.get("notes") or []))
    return " ".join(
        [
            str((issue.get("project") or {}).get("name", "")),
            str(issue.get("summary", "")),
            str(issue.get("description", "")),
            str(issue.get("additional_information") or ""),
            str(issue.get("steps_to_reproduce", "")),
            str((issue.get("reporter") or {}).get("real_name", "")),
            str((issue.get("handler") or {}).get("real_name", "")),
            custom_text,
            notes_text,
        ]
    )


def mantis_issue_effort_date(issue: Dict[str, Any]) -> str:
    from backend.effort_fields import issue_delivery_date
    return issue_delivery_date(issue)


def build_sources_from_issue_rows(issues: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "type": issue.get("type", "mantis"),
            "source": issue.get("source", ""),
            "issue_id": issue.get("id"),
            "title": issue.get("summary", "제목 없음"),
            "project": issue.get("project", "미상"),
            "customer_name": customer_name(issue.get("project")),
            "status": issue.get("status", "상태 미상"),
            "status_label": issue.get("status_label") or (issue.get("detail") or {}).get("status_label", ""),
            "priority": issue.get("priority", ""),
            "priority_label": issue.get("priority_label") or (issue.get("detail") or {}).get("priority_label", ""),
            "severity": issue.get("severity", ""),
            "handler": issue.get("handler", "담당자 미지정"),
            "created_at": issue.get("created_at", ""),
            "delivered_at": mantis_issue_effort_date(issue),
            "effort_person": issue_effort_people(issue),
            "preview": issue.get("preview") or issue.get("description", ""),
            "detail": issue.get("detail") or issue,
        }
        for issue in issues
    ]
