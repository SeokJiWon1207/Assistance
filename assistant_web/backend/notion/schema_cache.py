"""재시작 후에도 최근 DB 스키마를 재사용한다. 페이지·토큰은 저장하지 않는다."""

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

from backend.settings.paths import RUNTIME_DIR
from backend.settings.notion import get_notion_headers


def _path(database_id: str, headers: dict[str, str]) -> Path:
    identity = [database_id.replace("-", "").lower(), headers]
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return RUNTIME_DIR / "notion_schemas" / f"{key}.json"


def save_schema(database_id: str, headers: dict[str, str], data: dict[str, Any]) -> None:
    if data.get("object") != "database" or not data.get("properties"):
        return
    # DB 객체의 구조와 이름만 필요하다. 응답의 다른 데이터는 보관하지 않는다.
    schema = {key: data[key] for key in ("id", "object", "title", "properties") if key in data}
    path = _path(database_id, headers)
    temporary = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            temporary = file.name
            json.dump({"fetchedAt": time.time(), "schema": schema}, file, ensure_ascii=False)
        os.replace(temporary, path)
    except OSError:
        # 저장 공간이나 권한 문제가 있어도 실시간 조회 결과는 그대로 사용한다.
        if temporary:
            try:
                Path(temporary).unlink(missing_ok=True)
            except OSError:
                pass


def load_schema(database_id: str, headers: dict[str, str], max_age: float) -> tuple[float, dict[str, Any]] | None:
    try:
        data = json.loads(_path(database_id, headers).read_text(encoding="utf-8"))
        age = time.time() - float(data["fetchedAt"])
        schema = data["schema"]
        if not 0 <= age < max_age or not isinstance(schema, dict):
            return None
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties or not all(
            isinstance(prop, dict) and isinstance(prop.get("type"), str) for prop in properties.values()
        ):
            return None
        if str(schema.get("id", "")).replace("-", "").lower() != database_id.replace("-", "").lower():
            return None
        return age, schema
    except (OSError, ValueError, TypeError, KeyError):
        return None


_schema_seeds: Dict[tuple[str, str], tuple[float, Dict[str, Any]]] = {}


def remember_notion_database_schema(data: Dict[str, Any]) -> None:
    """범위 조회가 이미 받은 스키마를 재사용해 DB마다 같은 요청을 반복하지 않는다."""
    key = (str(data.get("id", "")).replace("-", ""), get_notion_headers().get("Authorization", ""))
    if key[0] and data.get("object") == "database" and "properties" in data:
        _schema_seeds[key] = (time.monotonic() + 300, data)
        while len(_schema_seeds) > 128:
            _schema_seeds.pop(next(iter(_schema_seeds)))


def _seed_schema(database_id: str) -> Dict[str, Any]:
    cached = _schema_seeds.get((database_id.replace("-", ""), get_notion_headers().get("Authorization", "")))
    return cached[1] if cached and cached[0] > time.monotonic() else {}
