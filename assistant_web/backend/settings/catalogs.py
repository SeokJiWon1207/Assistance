"""고객사 JSON 로더와 명시적으로 초기화되는 공유 목록.

앱 lifespan과 CLI 준비 단계에서 initialize_customer_catalogs를 호출한다.
이전 import 경로에 노출된 사전은 교체하지 않아 기존 참조가 같은 객체를 사용한다.
"""
import json
import sys
from threading import Lock
from typing import Any, Dict, List

from backend.settings.paths import CONFIG_DIR
from backend.settings.values import DEFAULT_CUSTOMER_ALIASES


CUSTOMER_ALIASES: Dict[str, List[str]] = {}
CUSTOMER_DIRECTORY: Dict[str, Any] = {}
CUSTOMER_PROJECT_DISPLAY_NAMES: Dict[str, str] = {}
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
        CUSTOMER_ALIASES.clear()
        CUSTOMER_ALIASES.update(aliases)
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
