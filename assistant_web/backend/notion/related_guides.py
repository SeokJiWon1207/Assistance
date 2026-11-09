"""이슈의 기능명을 제목·본문 색인에서 찾고, 다른 가이드의 현재 본문과 소속을 확인한다."""
import json
import os
import re
import sqlite3
import time
from typing import Any

import requests
from fastapi import HTTPException

from backend.execution import CURRENT_BUDGET, checkpoint
from backend.notion.body_index import BodyIndexError, open_body_index
from backend.notion.body_index_sync import CrawlLimits, validate_index_candidate
from backend.notion.discovery import resolve_notion_settings
from backend.notion.mapping import normalize_notion_page
from backend.notion.page_enrichment import notion_page_matches_and_enrich
from backend.notion.page_ownership import segment_belongs_to_page
from backend.notion.read_api import NOTION_CONTENT_INCOMPLETE, fetch_notion_database_title
from backend.notion.read_scope import resolve_notion_read_scope
from backend.notion.values import notion_title
from backend.utils.text import compact_text, normalize_for_match


_GUIDE = re.compile(r"가이드|매뉴얼|메뉴얼|설명서|사용법|방법|절차|이용\s*안내|\b(?:manual|guide)\b", re.I)
_TICKET = re.compile(r"요청|문의|접수|현상|장애\s*이력")
_STEP = re.compile(r"클릭|선택|누르|입력|설치|실행|접속|등록|설정|열어|처리|분석|\d+[.)]\s|해야|하세요")
_ACTION = re.compile(r"클릭|선택|누르|입력|설치|실행|접속|열어")
_ARTIFACT = re.compile(r'\b[\w%$.-]+\.(?:dat|ini|xml|json|cfg|conf|dll|cpp|py|h)\b|\b\w+\([^\n)]*\)', re.I)
_INSTRUCTION = re.compile(r'(?:로드|변환|복사|적용|수정|저장).*(?:합니다|한다|함|처리|사용)|'
                          r'(?:로드|변환|복사|적용|수정|저장)(?:하여|해|하세요)|→.*(?:처리|변환)|'
                          r'\b(?:set|load|save|init)\w*\(', re.I)
_MAX_CHECKS = 3
_GENERIC = {"이슈", "오류", "추가", "추가된", "변경", "확인", "요청", "관련", "실시간", "업데이트", "동작", "현상", "기능",
            "화면", "종목", "데이터", "장중", "되지", "않는", "안되는", "안드로이드", "ios", "mts", "hts"}


def topic_terms(summary: str, grounded: list[str]) -> list[str]:
    title = normalize_for_match(summary)
    terms = list(dict.fromkeys(t for t in grounded if len(t.strip()) >= 2))
    specific = [t for t in terms if normalize_for_match(t) in title and normalize_for_match(t) not in _GENERIC
                and not t.isdecimal()]
    return specific or [t for t in terms if normalize_for_match(t) not in _GENERIC and not t.isdecimal()]


def _technical_passage(lines: list[str], positions: list[int], terms: list[str]) -> str:
    """진행 기록 안에도 기능별 설정 절차가 있다. 다른 기능의 구간과 예정·요청은 제외한다."""
    for pos in positions:
        if normalize_for_match(lines[pos].lstrip('# ').rstrip(':')) not in terms:
            continue
        section = []
        for line in lines[pos + 1:pos + 15]:
            if (re.fullmatch(r'[\w ]*(?:차트|검색)(?:\s*(?:기능|설정|화면))?', line)
                    and not any(t in normalize_for_match(line) for t in terms)):
                break
            section.append(line)
        instructions = [i for i, line in enumerate(section) if _INSTRUCTION.search(line)
                        and not re.search(r'예정|요청|문의|검토|가능\s*여부', line)]
        if len(instructions) >= 2 and _ARTIFACT.search(' '.join(section)):
            return compact_text(' '.join(section[max(0, instructions[0] - 1):]), 400)
    return ''


def guide_passage(title: str, body: str, terms: list[str]) -> tuple[str, bool]:
    """제목이나 속성만으로 가이드 본문을 대신하지 않는다. 해당 기능의 가까운 절차를 발췌한다."""
    title_key = normalize_for_match(title)
    lines = [line.strip() for line in body.replace(NOTION_CONTENT_INCOMPLETE, "").splitlines()
             if line.strip() and not line.strip().lower().startswith("untitled")]
    normalized = [normalize_for_match(t) for t in terms if t.strip()]
    if not lines or not normalized:
        return "", False
    title_hit = any(t in title_key for t in normalized)
    positions = [i for i, line in enumerate(lines) if any(t in normalize_for_match(line) for t in normalized)]
    if not (positions or title_hit):
        return "", False
    title_guide = bool(_GUIDE.search(title) and not _TICKET.search(title))
    anchors = positions or list(range(len(lines)))
    for i in sorted(anchors, key=lambda pos: not bool(_ACTION.search(lines[pos]))):
        window = lines[i:i + 4]
        passage = compact_text(" ".join(window), 400)
        heading = bool(_GUIDE.search(lines[i]) and not _TICKET.search(lines[i]))
        if ((title_guide and title_hit) or heading) and _STEP.search(passage) and normalize_for_match(passage) != title_key:
            return passage, True
    technical = _technical_passage(lines, positions, normalized)
    if technical:
        return technical, True
    return compact_text(" ".join(lines[(positions or [0])[0]:][:3]), 400), False


def _candidate_ids(index, scope, terms, constraints, excluded, known):
    # 저장된 제목·소속·본문은 순서 결정에만 쓴다. 원문 접근을 확인한 후에만 결과를 내보낸다.
    from backend.notion.matching import notion_page_matches_common_filters
    places, params = index._scope(list(scope))
    ranked = {}
    with index._database() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        hits: set[str] = set()
        predicates = " OR ".join("instr(normalized_text, ?) > 0" for _ in terms)
        for table in ("blocks", "partial_blocks", "synced_segments"):
            if table in tables:
                owned = "" if table == "synced_segments" else " AND owner_page_id=page_id"
                hits.update(r[0] for r in db.execute(
                    f"SELECT DISTINCT page_id FROM {table} WHERE fingerprint=?{owned} AND ({predicates})",
                    [index.fingerprint, *terms]))
        rows = db.execute(f"SELECT page_id, database_id, raw_page FROM pages WHERE fingerprint=? "
                          f"AND database_id IN ({places})", params)
        customer = [c for c in constraints.get("common_filter_clauses", []) if c["field"] == "customer"]
        for identifier, database_id, payload in rows:
            checkpoint()
            if identifier in excluded:
                continue
            raw = json.loads(payload)
            title = notion_title(raw.get("properties", {}))
            title_hit = any(t in normalize_for_match(title) for t in terms)
            if identifier not in hits and not title_hit:
                continue
            page = normalize_notion_page(raw, include_blocks=False, database_id=database_id)
            scope_hint = notion_page_matches_common_filters(page, customer)
            ranked[identifier] = ((scope_hint, bool(_GUIDE.search(title) and not _TICKET.search(title)),
                                  identifier in hits, title_hit, str(raw.get("last_edited_time", ""))), database_id)
    for identifier, database_id in known.items():
        if identifier not in excluded and database_id in scope:
            ranked.setdefault(identifier, ((True, False, False, True, ""), database_id))
    return [(identifier, ranked[identifier][1]) for identifier in sorted(ranked, key=lambda key: ranked[key][0], reverse=True)]


def find_alternate_guide(terms: list[str], constraints: dict, excluded: set[str], known: dict[str, str]
                         ) -> tuple[dict | None, str, dict[str, Any]]:
    from backend.notion.search import merge_common_notion_constraints
    report: dict[str, Any] = {"outcome": "no_verified_guide", "candidate_count": 0, "checked_count": 0,
                              "readable_count": 0, "empty_body_count": 0}
    if os.getenv("NOTION_FETCH_PAGE_BLOCKS", "true").lower() in {"0", "false", "no"}:
        return None, "", {**report, "outcome": "body_disabled"}
    active = CURRENT_BUDGET.get()
    deadline = time.monotonic() + min(60.0, max(0.0, active.remaining() - 2.0) if active else 60.0)
    terms = list(dict.fromkeys(normalize_for_match(t) for t in terms if t.strip()))[:6]
    filters = merge_common_notion_constraints({}, constraints)
    try:
        settings = resolve_notion_settings()
        scope, complete = resolve_notion_read_scope(settings["database_ids"], settings["token"])
        if not complete or not scope or not terms:
            return None, "", {**report, "outcome": "unverified_scope"}
        index = open_body_index()
        excluded = {v.replace("-", "").lower() for v in excluded}
        known = {k.replace("-", "").lower(): v.replace("-", "").lower() for k, v in known.items()}
        candidates = [(k, v) for k, v in known.items() if k not in excluded and v in scope]
        if index:
            try:
                candidates = _candidate_ids(index, scope, terms, filters, excluded, known)
            except (BodyIndexError, sqlite3.Error, OSError, ValueError):
                report["outcome"] = "index_unavailable"
        report["candidate_count"] = len(candidates)
        for identifier, database_id in candidates[:_MAX_CHECKS]:
            checkpoint()
            if time.monotonic() >= deadline:
                report["outcome"] = "time_limit"
                break
            report["checked_count"] += 1
            try:
                current = validate_index_candidate({"page_id": identifier, "database_id": database_id}, list(scope),
                    deadline=deadline, limits=CrawlLimits(max_blocks=160, max_depth=8, max_requests=60, max_seconds=25))
            except (HTTPException, requests.RequestException):
                continue
            if not current or not current.get("accessible_content_complete"):
                continue
            report["readable_count"] += 1
            body = "\n".join(s["text"] for s in current["segments"] if s.get("text") and segment_belongs_to_page(s, identifier))
            if not body.strip():
                report["empty_body_count"] += 1
                continue
            raw = current["raw_page"]
            excerpt, is_guide = guide_passage(notion_title(raw["properties"]), body, terms)
            if not is_guide:
                continue
            label = fetch_notion_database_title(database_id)
            root = scope[database_id]
            if root != database_id:
                label = f"{fetch_notion_database_title(root)} / {label}"
            page = normalize_notion_page(raw, include_blocks=False, database_id=database_id, database_label=label)
            page["content"] = body
            if notion_page_matches_and_enrich(page, filters):
                report["outcome"] = "found"
                return page, excerpt, report
    except (BodyIndexError, sqlite3.Error, OSError, ValueError, HTTPException, requests.RequestException):
        report["outcome"] = "lookup_unavailable"
    return None, "", report
