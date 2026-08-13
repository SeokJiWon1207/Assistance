"""팀 공용 질문 템플릿 설정 로딩과 검증."""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import CONFIG_DIR


TEAM_TEMPLATES_PATH = CONFIG_DIR / "team_templates.json"
TEMPLATE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,63}$")
FIELD_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
PLACEHOLDER_PATTERN = re.compile(r"\{([A-Za-z][A-Za-z0-9_]*)(?:([|?])([^{}]*))?\}")
ALLOWED_FIELD_TYPES = {"text", "select", "number"}
ALLOWED_NORMALIZERS = {"issue-id"}
ALLOWED_ICONS = {"today", "customer", "profile", "spike", "aging", "similar", "effort", "report"}


class TeamTemplateConfigError(ValueError):
    """팀 템플릿 설정이 안전하게 표시될 수 없을 때 발생한다."""


def _required_text(value: Any, label: str, max_length: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TeamTemplateConfigError(f"{label}은 비어 있지 않은 문자열이어야 합니다.")
    text = value.strip()
    if len(text) > max_length:
        raise TeamTemplateConfigError(f"{label}은 {max_length}자를 넘을 수 없습니다.")
    return text


def _optional_text(value: Any, label: str, max_length: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TeamTemplateConfigError(f"{label}은 문자열이어야 합니다.")
    if len(value) > max_length:
        raise TeamTemplateConfigError(f"{label}은 {max_length}자를 넘을 수 없습니다.")
    return value


def _validate_field(raw: Any, template_id: str) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise TeamTemplateConfigError(f"{template_id}의 fields 항목은 객체여야 합니다.")
    name = _required_text(raw.get("name"), f"{template_id}.field.name", 64)
    if not FIELD_NAME_PATTERN.fullmatch(name):
        raise TeamTemplateConfigError(f"{template_id}의 필드명 형식이 올바르지 않습니다: {name}")
    field_type = _required_text(raw.get("type"), f"{template_id}.{name}.type", 16)
    if field_type not in ALLOWED_FIELD_TYPES:
        raise TeamTemplateConfigError(f"{template_id}.{name}의 type을 지원하지 않습니다: {field_type}")

    field: Dict[str, Any] = {
        "name": name,
        "label": _required_text(raw.get("label"), f"{template_id}.{name}.label", 80),
        "type": field_type,
    }
    if raw.get("required") is not None:
        if not isinstance(raw["required"], bool):
            raise TeamTemplateConfigError(f"{template_id}.{name}.required는 boolean이어야 합니다.")
        field["required"] = raw["required"]
    for key in ("value", "placeholder"):
        value = _optional_text(raw.get(key), f"{template_id}.{name}.{key}", 200)
        if value:
            field[key] = value

    if field_type == "select":
        options = raw.get("options")
        if not isinstance(options, list) or not options or len(options) > 30:
            raise TeamTemplateConfigError(f"{template_id}.{name}.options는 1~30개의 배열이어야 합니다.")
        field["options"] = [
            _required_text(option, f"{template_id}.{name}.options", 100)
            for option in options
        ]
        if len(set(field["options"])) != len(field["options"]):
            raise TeamTemplateConfigError(f"{template_id}.{name}.options에 중복 값이 있습니다.")
        if field.get("value") and field["value"] not in field["options"]:
            raise TeamTemplateConfigError(f"{template_id}.{name}.value가 options에 없습니다.")

    for key in ("min", "max"):
        if raw.get(key) is not None:
            if field_type != "number" or not isinstance(raw[key], (int, float)):
                raise TeamTemplateConfigError(f"{template_id}.{name}.{key} 값이 올바르지 않습니다.")
            field[key] = raw[key]
    if "min" in field and "max" in field and field["min"] > field["max"]:
        raise TeamTemplateConfigError(f"{template_id}.{name}의 min이 max보다 큽니다.")

    normalizer = raw.get("normalize")
    if normalizer is not None:
        if normalizer not in ALLOWED_NORMALIZERS:
            raise TeamTemplateConfigError(f"{template_id}.{name}.normalize를 지원하지 않습니다.")
        field["normalize"] = normalizer
    return field


def _validate_template(raw: Any) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise TeamTemplateConfigError("templates 항목은 객체 배열이어야 합니다.")
    template_id = _required_text(raw.get("id"), "template.id", 64)
    if not TEMPLATE_ID_PATTERN.fullmatch(template_id):
        raise TeamTemplateConfigError(f"템플릿 ID 형식이 올바르지 않습니다: {template_id}")
    icon = _required_text(raw.get("icon"), f"{template_id}.icon", 32)
    if icon not in ALLOWED_ICONS:
        raise TeamTemplateConfigError(f"{template_id}에서 지원하지 않는 아이콘입니다: {icon}")
    question_template = _required_text(
        raw.get("questionTemplate"), f"{template_id}.questionTemplate", 800
    )
    if "{" in PLACEHOLDER_PATTERN.sub("", question_template) or "}" in PLACEHOLDER_PATTERN.sub("", question_template):
        raise TeamTemplateConfigError(f"{template_id}.questionTemplate의 자리표시자 형식이 올바르지 않습니다.")

    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields or len(raw_fields) > 12:
        raise TeamTemplateConfigError(f"{template_id}.fields는 1~12개의 배열이어야 합니다.")
    fields = [_validate_field(field, template_id) for field in raw_fields]
    names = [field["name"] for field in fields]
    if len(set(names)) != len(names):
        raise TeamTemplateConfigError(f"{template_id}.fields에 중복 필드명이 있습니다.")
    placeholder_names = {match.group(1) for match in PLACEHOLDER_PATTERN.finditer(question_template)}
    unknown = placeholder_names.difference(names)
    if unknown:
        raise TeamTemplateConfigError(f"{template_id}에 정의되지 않은 자리표시자가 있습니다: {', '.join(sorted(unknown))}")
    missing_required = {
        field["name"] for field in fields if field.get("required") and field["name"] not in placeholder_names
    }
    if missing_required:
        raise TeamTemplateConfigError(f"{template_id}의 필수 필드가 질문에 사용되지 않습니다.")

    return {
        "id": template_id,
        "category": _required_text(raw.get("category"), f"{template_id}.category", 80),
        "title": _required_text(raw.get("title"), f"{template_id}.title", 100),
        "description": _required_text(raw.get("description"), f"{template_id}.description", 240),
        "icon": icon,
        "questionTemplate": question_template,
        "fields": fields,
    }


@lru_cache(maxsize=8)
def load_team_templates(path: Optional[Path] = None) -> Dict[str, Any]:
    """JSON 파일을 한 번 읽고 검증한 결과를 프로세스 수명 동안 재사용한다."""
    config_path = path or TEAM_TEMPLATES_PATH
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TeamTemplateConfigError(f"팀 템플릿 설정을 읽지 못했습니다: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise TeamTemplateConfigError("team_templates.json의 version은 1이어야 합니다.")
    raw_templates = payload.get("templates")
    if not isinstance(raw_templates, list) or not raw_templates or len(raw_templates) > 50:
        raise TeamTemplateConfigError("templates는 1~50개의 배열이어야 합니다.")
    templates: List[Dict[str, Any]] = [_validate_template(item) for item in raw_templates]
    ids = [template["id"] for template in templates]
    if len(set(ids)) != len(ids):
        raise TeamTemplateConfigError("templates에 중복 ID가 있습니다.")
    return {"version": 1, "templates": templates}
