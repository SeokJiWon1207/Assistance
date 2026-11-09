"""고객사 JSON 로더와 명시적으로 초기화되는 공유 목록.

앱 lifespan과 CLI 준비 단계에서 initialize_customer_catalogs를 호출한다.
이전 import 경로에 노출된 사전은 교체하지 않아 기존 참조가 같은 객체를 사용한다.
"""
import json
import re
import sys
from threading import Lock
from typing import Any, Dict, List

from backend.settings.paths import CONFIG_DIR
from backend.settings.values import DEFAULT_CUSTOMER_ALIASES
from backend.utils.text import normalize_for_match


CUSTOMER_ALIASES: Dict[str, List[str]] = {}
CUSTOMER_DIRECTORY: Dict[str, Any] = {}
CUSTOMER_PROJECT_DISPLAY_NAMES: Dict[str, str] = {}
# 고객사 선택 목록(팀 템플릿·검색 자동완성·화면번호 필터)에 보이는 이름과 순서. 이름 -> 별칭 설정의 고객사 키.
CUSTOMER_PICKER: Dict[str, str] = {}
# 고객사 키 -> backend/static/assets/customers/ 아래 로고 파일 이름.
CUSTOMER_LOGOS: Dict[str, str] = {}
_initialized = False
_initialization_lock = Lock()


def load_customer_aliases() -> Dict[str, List[str]]:
    aliases_path = CONFIG_DIR / "customer_aliases.json"
    if not aliases_path.exists():
        return DEFAULT_CUSTOMER_ALIASES
    try:
        data = json.loads(aliases_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        print(f"[warn] customer_aliases.json 로딩 실패, 내장 기본값 사용: {exc}", file=sys.stderr)
        return DEFAULT_CUSTOMER_ALIASES
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and isinstance(values, list) and all(isinstance(v, str) for v in values)
        for key, values in data.items()
    ):
        print("[warn] customer_aliases.json 형식 오류(키:문자열, 값:문자열 배열), 내장 기본값 사용", file=sys.stderr)
        return DEFAULT_CUSTOMER_ALIASES
    return data


def load_customer_directory() -> Dict[str, Any]:
    path = CONFIG_DIR / "customer_directory.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    customers = data.get("customers", {})
    if not isinstance(customers, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict)
        or not isinstance(value.get("name"), str) or not value["name"].strip()
        or any(not isinstance(item, str) for item in value.get("projects", []) + value.get("aliases", []))
        for key, value in customers.items()
    ):
        raise ValueError("customer_directory.json 고객사 이름표 형식이 올바르지 않습니다.")
    return customers


def load_customer_project_display_names() -> Dict[str, str]:
    """기존 질의 별칭에 추가하지 않는 프로젝트 표시명 보정."""
    path = CONFIG_DIR / "customer_directory.json"
    entries = json.loads(path.read_text(encoding="utf-8")).get("project_display_names", {}) if path.exists() else {}
    if not isinstance(entries, dict) or any(
        not isinstance(key, str) or not isinstance(value, dict)
        or not isinstance(value.get("name"), str) or not value["name"].strip()
        for key, value in entries.items()
    ):
        raise ValueError("customer_directory.json 프로젝트 표시명 형식이 올바르지 않습니다.")
    return {key: value["name"] for key, value in entries.items()}


def load_customer_picker_names() -> List[str]:
    """선택 목록에 보일 고객사 이름. 비어 있으면 별칭 설정 전체를 쓴다."""
    path = CONFIG_DIR / "customer_directory.json"
    names = json.loads(path.read_text(encoding="utf-8")).get("picker_names", []) if path.exists() else []
    if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
        raise ValueError("customer_directory.json picker_names는 비어 있지 않은 문자열 배열이어야 합니다.")
    return list(dict.fromkeys(name.strip() for name in names))


def resolve_picker_names(names: List[str], aliases: Dict[str, List[str]]) -> Dict[str, str]:
    """선택 목록 이름마다 별칭 설정의 고객사 키를 찾는다. 모르는 이름이나 같은 고객사 중복은 설정 오류다."""
    owners: Dict[str, str] = {}
    for name in names:
        normalized = normalize_for_match(name)
        owner = next((key for key, values in aliases.items()
                      if normalized in {normalize_for_match(value) for value in [key, *values]}), None)
        if owner is None:
            raise ValueError(f"customer_directory.json picker_names의 '{name}'는 고객사 별칭에 없습니다.")
        if owner in owners.values():
            raise ValueError(f"customer_directory.json picker_names의 '{name}'는 다른 이름과 같은 고객사입니다.")
        owners[name] = owner
    return owners


def load_customer_logos(aliases: Dict[str, List[str]]) -> Dict[str, str]:
    """고객사 키별 로고 파일. 별칭 설정에 없는 키나 안전하지 않은 파일 이름은 설정 오류다."""
    path = CONFIG_DIR / "customer_directory.json"
    logos = json.loads(path.read_text(encoding="utf-8")).get("customer_logos", {}) if path.exists() else {}
    if not isinstance(logos, dict):
        raise ValueError("customer_directory.json customer_logos는 객체여야 합니다.")
    for key, file_name in logos.items():
        if key not in aliases:
            raise ValueError(f"customer_directory.json customer_logos의 '{key}'는 고객사 별칭에 없습니다.")
        if not isinstance(file_name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.(?:png|webp)", file_name):
            raise ValueError(f"customer_directory.json customer_logos의 '{key}' 파일 이름이 올바르지 않습니다.")
    return dict(logos)


def initialize_customer_catalogs() -> None:
    """세 목록을 기존 우선순위로 한 번 읽고, 모두 유효할 때 공유 사전에 반영한다."""
    global _initialized
    with _initialization_lock:
        if _initialized:
            return
        aliases = load_customer_aliases()
        display_names = load_customer_project_display_names()
        directory = load_customer_directory()
        for key, entry in directory.items():
            aliases[key] = list(dict.fromkeys([
                entry["name"], *aliases.get(key, []),
                *entry.get("projects", []), *entry.get("aliases", []),
            ]))
        picker = resolve_picker_names(load_customer_picker_names(), aliases)
        logos = load_customer_logos(aliases)
        CUSTOMER_ALIASES.clear()
        CUSTOMER_ALIASES.update(aliases)
        CUSTOMER_PICKER.clear()
        CUSTOMER_PICKER.update(picker)
        CUSTOMER_LOGOS.clear()
        CUSTOMER_LOGOS.update(logos)
        CUSTOMER_PROJECT_DISPLAY_NAMES.clear()
        CUSTOMER_PROJECT_DISPLAY_NAMES.update(display_names)
        CUSTOMER_DIRECTORY.clear()
        CUSTOMER_DIRECTORY.update(directory)
        _initialized = True


def _require_initialized() -> None:
    if not _initialized:
        raise RuntimeError("고객사 목록 초기화가 필요합니다. initialize_customer_catalogs()를 먼저 호출하세요.")


def get_customer_aliases() -> Dict[str, List[str]]:
    _require_initialized()
    return CUSTOMER_ALIASES


def get_customer_directory() -> Dict[str, Any]:
    _require_initialized()
    return CUSTOMER_DIRECTORY


def get_customer_project_display_names() -> Dict[str, str]:
    _require_initialized()
    return CUSTOMER_PROJECT_DISPLAY_NAMES


def get_customer_picker() -> Dict[str, str]:
    """선택 목록 이름 -> 고객사 키. 설정 순서를 유지한다."""
    _require_initialized()
    return CUSTOMER_PICKER


def get_customer_logos() -> Dict[str, str]:
    """고객사 키 -> 로고 파일 이름."""
    _require_initialized()
    return CUSTOMER_LOGOS
