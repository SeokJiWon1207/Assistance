"""팀 공용 질문 템플릿 API."""

import re

from datetime import date
from threading import Lock
from typing import Any, Dict, Tuple

from fastapi import APIRouter, HTTPException

from backend.config import CUSTOMER_ALIASES, CUSTOMER_DIRECTORY, CUSTOMER_PROJECT_DISPLAY_NAMES
from backend.display import customer_name
from backend.sources.mantis_store import read_complete_store
from backend.team_templates import (
    TeamTemplateConfigError,
    load_team_templates,
    order_customer_options,
    recent_customer_issue_counts,
)


router = APIRouter()
_counts_lock = Lock()
_counts_cache: Dict[str, Any] = {"key": None, "counts": {}}


def recent_customer_statistics() -> Dict[str, Any]:
    """저장된 Mantis 전체 세대에서 고객사별 최근 이슈 수를 센다. 세대·날짜가 같으면 다시 세지 않는다.

    순서를 정하는 데만 쓰므로 갱신이 늦은 저장본도 쓰고, 저장본이 없으면 빈 값(설정 순서)으로 둔다.
    """
    rows = read_complete_store(allow_stale=True, summary_only=True)
    if rows is None:
        return {"available": False, "counts": {}, "asOf": ""}
    today = date.today()
    key: Tuple[str, str] = (str(rows.meta.get("fetchedAt") or ""), today.isoformat())
    with _counts_lock:
        if _counts_cache["key"] != key:
            _counts_cache["counts"] = recent_customer_issue_counts(rows, today=today)
            _counts_cache["key"] = key
        return {"available": True, "counts": _counts_cache["counts"], "asOf": key[0]}


def recent_customer_counts() -> Dict[str, int]:
    return recent_customer_statistics()["counts"]


@router.get("/api/templates")
def list_team_templates() -> Dict[str, Any]:
    try:
        statistics = recent_customer_statistics()
        return order_customer_options(load_team_templates(), statistics["counts"], statistics=statistics)
    except TeamTemplateConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/api/customer-names")
def list_customer_names() -> Dict[str, Any]:
    """표시 전용 이름표. 원본 프로젝트·검색 별칭은 변경하지 않는다."""
    exact = {alias: customer_name(alias) for key, aliases in CUSTOMER_ALIASES.items()
             for alias in [key, *aliases]}
    # 이름표 이전부터 별칭으로만 등록됐던 프로젝트도 같은 표시 함수를 쓴다.
    projects = {}
    for alias, name in exact.items():
        code = re.match(r"^(\d{3}[A-Za-z가-힣][\w-]*)", alias)
        if code:
            project = re.sub(r"_Project$", "", code[1], flags=re.IGNORECASE)
            projects[project] = name
    projects.update({project: item["name"] for item in CUSTOMER_DIRECTORY.values()
                     for project in item.get("projects", [])})
    projects.update(CUSTOMER_PROJECT_DISPLAY_NAMES)
    exact.update(CUSTOMER_PROJECT_DISPLAY_NAMES)
    return {"exact": exact, "projects": projects}
