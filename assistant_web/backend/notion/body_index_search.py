"""로컬 본문 색인은 후보 발견에만 쓰고 반환할 근거는 API에서 다시 읽는다."""

import sqlite3
import time
from typing import Any

import requests
from fastapi import HTTPException

from backend.common import normalize_for_match
from backend.execution import CURRENT_BUDGET, checkpoint
from backend.notion.body_index import BodyIndexError, segment_belongs_to_page


def open_body_index():
    from backend.notion.body_index import open_body_index as open_existing
    return open_existing()


def validate_index_candidate(record, database_ids, *, index, deadline):
    from backend.notion.body_index_sync import validate_index_candidate as validate
    return validate(record, database_ids, index=index, deadline=deadline)


def find_indexed_body_candidates(database_ids: list[str], terms: list[str], *,
                                 body_clauses: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    try:
        index = open_body_index()
        if index is None:
            return None
        records = index.search(database_ids, terms)
        coverage = index.status(database_ids)
    except (BodyIndexError, sqlite3.Error, OSError, ValueError):
        # 손상/호환되지 않는 저장본 때문에 기존 실시간 검색을 중단하지 않는다.
        return None
    records = list({str(row["page_id"]).replace("-", ""): row for row in records}.values())
    positive_clauses = [clause for clause in body_clauses or [] if clause.get("operator") == "contains"]

    def priority(row):
        owner = str(row["page_id"]).replace("-", "")
        typed_match = all(any(
            segment_belongs_to_page(block, owner)
            and (not clause.get("block_type") or block.get("type") == clause["block_type"])
            and normalize_for_match(str(clause["value"])) in normalize_for_match(str(block.get("text", "")))
            for block in row.get("segments", [])
        ) for clause in positive_clauses)
        return typed_match, str((row.get("raw_page") or {}).get("last_edited_time", ""))

    # 유형이 다른 최신 본문 30개가 뒤쪽 코드·표의 알려진 후보를 밀어내지 않게 한다.
    # 나머지 후보도 버리지 않는다. 수집 후 유형/본문이 바뀌었을 수 있다.
    records.sort(key=priority, reverse=True)
    reasons: set[str] = set()
    candidates = []
    deadline = time.monotonic() + 60
    budget = CURRENT_BUDGET.get()
    if budget is not None:
        deadline = min(deadline, budget.deadline - 5)
    for record in records[:30]:
        checkpoint()
        if time.monotonic() >= deadline - 1:
            reasons.add("body_index_validation_limit")
            break
        try:
            current = validate_index_candidate(record, database_ids, index=index, deadline=deadline)
        except (BodyIndexError, requests.RequestException, HTTPException, ValueError, sqlite3.Error, OSError):
            reasons.add("body_index_validation_error")
            continue
        if current is not None:
            candidates.append(current)
            if not current.get("complete"):
                reasons.add("body_index_content_incomplete")
        else:
            reasons.add("body_index_candidate_unavailable")
    if len(records) > 30:
        reasons.add("body_index_validation_limit")
    return {
        "candidates": candidates,
        "candidate_count": len(records),
        "coverage": coverage,
        "validation_complete": not reasons,
        "incomplete_reasons": sorted(reasons),
    }
