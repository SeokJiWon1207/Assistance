"""Mantis HTTP 조회·재시도·동시 요청 한도. 저장본과 검색 실행을 알지 못한다."""
from threading import BoundedSemaphore
from typing import Any, Dict, List, Optional
import requests
from fastapi import HTTPException
from backend.config import get_mantis_settings, get_mantis_headers
from backend.execution import bounded_timeout, interruptible_sleep, execution_slot

_mantis_request_slots = BoundedSemaphore(6)


def fetch_mantis_issues(
    params: Dict[str, Any],
    timeout: int = 30,
    attempts: int = 3,
) -> List[Dict[str, Any]]:
    settings = get_mantis_settings()
    safe_attempts = max(1, min(int(attempts), 3))
    for attempt in range(1, safe_attempts + 1):
        try:
            with execution_slot(_mantis_request_slots):
                response = requests.get(
                    settings["url"],
                    headers=get_mantis_headers(),
                    params=params,
                    timeout=bounded_timeout(max(3, min(int(timeout), 60))),
                )
        except (requests.ConnectionError, requests.Timeout):
            if attempt == safe_attempts:
                raise
            interruptible_sleep(2 ** (attempt - 1))
            continue

        retryable_status = response.status_code == 429 or response.status_code >= 500
        if retryable_status and attempt < safe_attempts:
            interruptible_sleep(2 ** (attempt - 1))
            continue
        if response.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"Mantis API failed: {response.status_code} {response.text[:300]}",
            )
        return response.json().get("issues", [])
    return []


def fetch_mantis_issue_by_id(issue_id: int) -> Optional[Dict[str, Any]]:
    if not issue_id:
        return None
    from backend.sources.mantis_refresh import fetch_with_refresh_budget
    return fetch_with_refresh_budget(issue_id, lambda timeout, attempts:
                                     _fetch_mantis_issue_by_id(issue_id, timeout, attempts))


def _fetch_mantis_issue_by_id(issue_id: int, timeout: float, attempts: int) -> Optional[Dict[str, Any]]:

    settings = get_mantis_settings()
    issue_url = f"{settings['url'].rstrip('/')}/{issue_id}"
    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                issue_url,
                headers=get_mantis_headers(),
                timeout=bounded_timeout(timeout),
            )
        except (requests.ConnectionError, requests.Timeout):
            if attempt == attempts:
                raise
            interruptible_sleep(2 ** (attempt - 1))
            continue

        retryable_status = response.status_code == 429 or response.status_code >= 500
        if retryable_status and attempt < attempts:
            interruptible_sleep(2 ** (attempt - 1))
            continue
        if retryable_status:
            raise HTTPException(
                status_code=502,
                detail=f"Mantis API failed: {response.status_code} {response.text[:300]}",
            )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise HTTPException(status_code=response.status_code, detail="Mantis 이슈 접근이 거절되었습니다.")

        issues = response.json().get("issues", [])
        return issues[0] if issues else None
    return None
