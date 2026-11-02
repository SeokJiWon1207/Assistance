"""저장된 Chroma 문서의 필드와 이슈 값을 해석한다. 저장소 조회는 하지 않는다."""
import re
from typing import Any, Dict, Optional
from backend.settings.values import MANTIS_DATE_CUSTOM_FIELDS
from backend.utils.dates import format_mantis_custom_date


def extract_chroma_field(text: str, field_name: str) -> str:
    match = re.search(rf"{re.escape(field_name)}:[ \t]*([^\n\r]*)", text)
    if match:
        return match.group(1).strip()
    return ""


def extract_chroma_date(text: str) -> str:
    match = re.search(r"등록일:\s*(\d{4}-\d{2}-\d{2}|미상)", text)
    if match:
        return match.group(1)
    match = re.search(r"(\d{4}-\d{2}-\d{2})\s+메일\s+수신", text)
    if match:
        return match.group(1)
    return ""


def extract_issue_id_from_text(text: str) -> int:
    match = re.search(r"이슈 ID:\s*#?(\d+)", text)
    if match:
        return int(match.group(1))
    return 0


def extract_title_from_chroma_text(text: str) -> str:
    match = re.search(r"\[제목\]\s*([\s\S]*?)(\n\n|\[고객사 본문 요청 내용\])", text)
    if match:
        return match.group(1).strip()
    return "제목 없음"


def extract_project_from_chroma_text(text: str) -> str:
    match = re.search(r"프로젝트\(고객사\):\s*(.*)", text)
    if match:
        return match.group(1).strip()
    return "프로젝트 미상"


def normalize_chroma_issue_document(text: str) -> Optional[Dict[str, Any]]:
    issue_id = extract_issue_id_from_text(text)
    if not issue_id:
        return None
    created_at = extract_chroma_date(text)
    return {
        "id": issue_id,
        "project": extract_project_from_chroma_text(text),
        "summary": extract_title_from_chroma_text(text),
        "status": extract_chroma_field(text, "현재 상태") or "상태 미상",
        "status_label": extract_chroma_field(text, "상태 표시명"),
        "priority": extract_chroma_field(text, "우선순위"),
        "priority_label": extract_chroma_field(text, "우선순위 표시명"),
        "severity": extract_chroma_field(text, "이슈 유형"),
        "severity_label": extract_chroma_field(text, "이슈 유형 표시명"),
        "media_type": extract_chroma_field(text, "매체구분") or "기타/미상",
        "screen_number": extract_chroma_field(text, "화면번호") or "미상",
        "work_subject": extract_chroma_field(text, "작업주체") or "미상",
        "effort_fields": {},
        "date_fields": {name: format_mantis_custom_date(value) for name in MANTIS_DATE_CUSTOM_FIELDS
                        if (value := extract_chroma_field(text, name))},
        "reporter": extract_chroma_field(text, "요청자") or "미상",
        "handler": extract_chroma_field(text, "담당자") or "담당자 미지정",
        "created_at": created_at,
        "updated_at": "",
        "description": text[:800],
    }
