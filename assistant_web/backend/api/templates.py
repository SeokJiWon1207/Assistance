"""팀 공용 질문 템플릿 API."""

from datetime import date
from threading import Lock
from typing import Any, Dict, Tuple

from fastapi import APIRouter, HTTPException

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


def recent_customer_counts() -> Dict[str, int]:
    """저장된 Mantis 전체 세대에서 고객사별 최근 이슈 수를 센다. 세대·날짜가 같으면 다시 세지 않는다.

    순서를 정하는 데만 쓰므로 갱신이 늦은 저장본도 쓰고, 저장본이 없으면 빈 값(설정 순서)으로 둔다.
    """
    rows = read_complete_store(allow_stale=True, summary_only=True)
    if rows is None:
        return {}
    today = date.today()
    key: Tuple[str, str] = (str(rows.meta.get("fetchedAt") or ""), today.isoformat())
    with _counts_lock:
        if _counts_cache["key"] != key:
            _counts_cache["counts"] = recent_customer_issue_counts(rows, today=today)
            _counts_cache["key"] = key
        return _counts_cache["counts"]


@router.get("/api/templates")
def list_team_templates() -> Dict[str, Any]:
    try:
        return order_customer_options(load_team_templates(), recent_customer_counts())
    except TeamTemplateConfigError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
