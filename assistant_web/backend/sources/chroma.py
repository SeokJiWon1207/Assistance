"""Chroma 벡터 검색 소스 어댑터 — 의미검색·랭킹·문서 조회(Layer 3).

chromadb 벡터스토어 조회(지연 import; 없으면 어휘 기반 폴백), 제약조건 기반
where 필터 구성, 의미 유사도 스코어링·재랭킹, 이슈 문서 정규화/조회를 담당한다.

의존: config/common + understanding(제약 매칭·키워드) + mantis(이슈 정규화/조회).
"""

import os
import sqlite3
from contextlib import closing
from time import perf_counter
from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException

from backend.execution import bounded_timeout, RequestStopped
from backend.timing import measured
from backend.intent.search_terms import keyword_term_aliases, text_matches_keyword_terms
from backend.common import (
    clamp_int,
    extract_issue_id_from_text,
    normalize_for_match,
)
from backend.config import CHROMA_DB_PATH, CHROMA_PERSIST_DIR, STATUS_MATCH_ALIASES
from backend.sources.mantis_evidence import constraint_evidence_terms
from backend.sources.chroma_lexical import (
    semantic_character_score as semantic_character_score,
    lexical_ranking_scope as lexical_ranking_scope,
    rank_lexical_documents,
)
from backend.sources.mantis import (
    fetch_mantis_issue_by_id,
    issue_matches_constraints,
    issue_to_search_text,
    normalize_issue,
)
from backend.understanding import (
    build_keyword_terms,
    chroma_text_matches_constraints,
    has_constraint,
)


def build_chroma_where(
    terms: List[str],
    constraints: Optional[Dict[str, Any]] = None,
    include_terms: bool = True,
) -> tuple[str, List[Any]]:
    where_parts = ["key = 'chroma:document'"]
    params: List[Any] = []

    if constraints and constraints.get("customer_aliases"):
        customer_parts = []
        for alias in constraints["customer_aliases"]:
            customer_parts.append("string_value LIKE ?")
            params.append(f"%{alias}%")
        where_parts.append("(" + " OR ".join(customer_parts) + ")")

    if constraints and constraints.get("platform_aliases"):
        platform_parts = []
        for alias in constraints["platform_aliases"]:
            platform_parts.append("string_value LIKE ?")
            params.append(f"%{alias}%")
        where_parts.append("(" + " OR ".join(platform_parts) + ")")

    if constraints and constraints.get("people", {}).get("names"):
        person_parts = []
        for name in constraints["people"]["names"]:
            person_parts.append("string_value LIKE ?")
            params.append(f"%{name}%")
        where_parts.append("(" + " OR ".join(person_parts) + ")")

    if constraints and constraints.get("statuses"):
        status_parts = []
        for status in constraints["statuses"]:
            for alias in STATUS_MATCH_ALIASES.get(status, [status]):
                status_parts.append("string_value LIKE ?")
                params.append(f"%{alias}%")
        where_parts.append("(" + " OR ".join(status_parts) + ")")

    if constraints and constraints.get("issue_ids"):
        issue_parts = []
        for issue_id in constraints["issue_ids"]:
            issue_parts.append("string_value LIKE ?")
            params.append(f"%이슈 ID: #{issue_id}%")
        where_parts.append("(" + " OR ".join(issue_parts) + ")")

    if constraints and constraints.get("screen_numbers"):
        screen_parts = []
        for screen_number in constraints["screen_numbers"]:
            screen_parts.append("string_value LIKE ?")
            params.append(f"%{screen_number}%")
        where_parts.append("(" + " OR ".join(screen_parts) + ")")

    if include_terms:
        for term in terms[:4]:
            aliases = keyword_term_aliases(term)
            where_parts.append("(" + " OR ".join("string_value LIKE ?" for _ in aliases) + ")")
            params.extend(f"%{alias}%" for alias in aliases)

    return " AND ".join(where_parts), params


def rank_chroma_documents_semantically(
    search_keyword: str,
    limit: int,
    constraints: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    if os.getenv("MANTIS_HYBRID_SEARCH_ENABLED", "true").lower() != "false":
        from backend.sources.mantis_hybrid import search_hybrid
        try:
            result = search_hybrid(search_keyword, limit, constraints, _rank_legacy_documents_semantically)
        except HTTPException:
            result = None  # Mantis 연결 미설정 환경에서도 기존 Chroma 검색은 사용할 수 있다.
        if result is not None:
            return result
    return _rank_legacy_documents_semantically(search_keyword, limit, constraints)


def _rank_legacy_documents_semantically(
    search_keyword: str,
    limit: int,
    constraints: Dict[str, Any],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Rank Chroma documents by bge-m3 distance, with a lexical semantic fallback."""
    multiplier = 80 if has_constraint(constraints) else 20
    candidate_limit = min(max(limit * multiplier, 100), 400)
    ranking_error = ""
    ranked: List[Dict[str, Any]]
    try:
        import chromadb  # Optional at runtime; lexical fallback keeps the app usable without it.

        embedding_base_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
        embedding_model = os.getenv("CHROMA_EMBEDDING_MODEL", "bge-m3:latest")
        timeout = clamp_int(os.getenv("CHROMA_EMBEDDING_TIMEOUT_SECONDS", "12"), 2, 60) or 12
        response = requests.post(
            f"{embedding_base_url}/api/embeddings",
            json={"model": embedding_model, "prompt": search_keyword},
            timeout=bounded_timeout(timeout),
        )
        response.raise_for_status()
        embedding = response.json().get("embedding") or []
        if not embedding:
            raise ValueError("임베딩 응답이 비어 있습니다.")

        client = chromadb.PersistentClient(path=str(CHROMA_PERSIST_DIR))
        collection = client.get_collection(os.getenv("CHROMA_COLLECTION_NAME", "langchain"))
        result = collection.query(
            query_embeddings=[embedding],
            n_results=min(candidate_limit, collection.count()),
            include=["documents", "distances"],
        )
        documents = (result.get("documents") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        ranked = [
            {
                "issue_id": extract_issue_id_from_text(content),
                "content": content[:2400],
                "distance": float(distance),
                "similarity_rank": index,
            }
            for index, (content, distance) in enumerate(
                zip(documents, distances, strict=False), start=1
            )
        ]
        ranking_meta = {"ranking": "semantic_vector", "embedding_model": embedding_model}
    except RequestStopped:
        raise
    except Exception as exc:  # The fallback is intentional for deployments without Ollama/Chroma.
        ranking_error = f"{type(exc).__name__}: {exc}"[:300]
        ranked, lexical_meta = rank_lexical_documents(CHROMA_DB_PATH, search_keyword, candidate_limit)
        ranking_meta = {"ranking": "semantic_lexical_fallback", "ranking_fallback_reason": ranking_error, **lexical_meta}

    filtered: List[Dict[str, Any]] = []
    seen_ids = set()
    for document in ranked:
        issue_id = int(document.get("issue_id") or 0)
        if not issue_id or issue_id in seen_ids:
            continue
        if not chroma_text_matches_constraints(document.get("content", ""), constraints):
            continue
        seen_ids.add(issue_id)
        filtered.append(document)
    focus_terms = [
        term for term in constraints.get("semantic_focus_terms", []) if len(normalize_for_match(term)) >= 2
    ]
    if focus_terms and filtered:
        frequencies = {
            term: sum(
                normalize_for_match(term) in normalize_for_match(document.get("content", ""))
                for document in filtered
            )
            for term in focus_terms
        }
        meaningful_terms = [term for term in focus_terms if frequencies.get(term, 0) > 0]
        if meaningful_terms:
            # Explicit follow-up topics such as "메모리 개선" should not be diluted by a
            # generic word like "개선". Keep candidates containing the rarer focus term.
            focus_term = min(meaningful_terms, key=lambda term: frequencies[term])
            filtered = [
                document
                for document in filtered
                if normalize_for_match(focus_term) in normalize_for_match(document.get("content", ""))
            ]
            vector_distances = [
                float(document["distance"]) for document in filtered if document.get("distance") is not None
            ]
            if vector_distances:
                distance_cutoff = min(vector_distances) * 1.15
                filtered = [
                    document
                    for document in filtered
                    if float(document.get("distance", distance_cutoff)) <= distance_cutoff
                ]
            ranking_meta["semantic_focus_term"] = focus_term
        elif constraints.get("require_semantic_focus"):
            filtered = []
            ranking_meta["semantic_focus_missing"] = "no_matching_candidates"
    return filtered, {**ranking_meta, "candidate_count": len(filtered)}


@measured("chroma_search")
def execute_chroma_search(
    search_keyword: str,
    limit: int = 5,
    constraints: Optional[Dict[str, Any]] = None,
    semantic: bool = False,
) -> Dict[str, Any]:
    if not CHROMA_DB_PATH.exists():
        return {
            "source": "chroma_db",
            "status": "skipped",
            "reason": f"Chroma DB 파일이 없습니다: {CHROMA_DB_PATH}",
            "documents": [],
            "meta": {"displayed_count": 0},
        }

    constraints = constraints or {}
    terms = build_keyword_terms(search_keyword, constraints)
    ranking_meta: Dict[str, Any] = {"ranking": "latest_issue_id"}
    candidate_documents: List[Dict[str, Any]]
    candidate_scope_complete = False
    if semantic:
        try:
            candidate_documents, ranking_meta = rank_chroma_documents_semantically(
                search_keyword,
                limit,
                constraints,
            )
        except sqlite3.Error as exc:
            return {
                "source": "chroma_db",
                "status": "error",
                "reason": f"Chroma DB 조회 실패: {exc}",
                "documents": [],
                "meta": {"displayed_count": 0},
            }
    else:
        if not terms and not has_constraint(constraints):
            terms = [search_keyword]
        where, params = build_chroma_where(terms, constraints, include_terms=True)
        sql = f"SELECT string_value FROM embedding_metadata WHERE {where} LIMIT 500"
        try:
            with closing(sqlite3.connect(CHROMA_DB_PATH)) as connection, connection:
                rows = connection.execute(sql, params).fetchall()
                candidate_scope_complete = len(rows) < 500
        except sqlite3.Error as exc:
            return {
                "source": "chroma_db",
                "status": "error",
                "reason": f"Chroma DB 조회 실패: {exc}",
                "documents": [],
                "meta": {"displayed_count": 0},
            }
        candidate_documents = []
        for (content,) in rows:
            if not chroma_text_matches_constraints(content, constraints):
                continue
            candidate_documents.append({"issue_id": extract_issue_id_from_text(content), "content": content[:1600]})
        candidate_documents.sort(key=lambda item: item["issue_id"], reverse=True)

    documents: List[Dict[str, Any]] = []
    refresh_started = perf_counter()
    refreshed_count = 0
    refreshed_issue_ids: set[int] = set()
    skipped_after_refresh = 0
    live_refresh_failures = 0
    live_refresh_available = True
    candidate_window = max(limit * 3, 30)
    for document in candidate_documents[:candidate_window]:
        issue_id = int(document.get("issue_id") or 0)
        if not issue_id:
            continue
        latest_issue = None
        if live_refresh_available:
            try:
                latest_issue = fetch_mantis_issue_by_id(issue_id)
            except (HTTPException, requests.RequestException) as exc:
                if isinstance(exc, HTTPException) and exc.status_code < 500 and exc.status_code != 429:
                    # 접근 거절/확인된 부재는 연결 장애의 오프라인 폴백과 다르다.
                    refreshed_issue_ids.add(issue_id)
                    skipped_after_refresh += 1
                    continue
                live_refresh_failures += 1
                live_refresh_available = False
            else:
                refreshed_issue_ids.add(issue_id)
                if latest_issue is None:
                    skipped_after_refresh += 1
                    continue
        if latest_issue:
            refreshed_count += 1
            refreshed_issue_ids.add(issue_id)
            if (has_constraint(constraints) or document.get("chunk_evidence")) and not issue_matches_constraints(latest_issue, constraints):
                skipped_after_refresh += 1
                continue
            if not semantic:
                if not text_matches_keyword_terms(issue_to_search_text(latest_issue), terms):
                    skipped_after_refresh += 1
                    continue
            document["latest_issue"] = normalize_issue(latest_issue, search_terms=terms + constraint_evidence_terms(constraints),
                                                       evidence_query=constraints.get("answer_query") or constraints.get("resolution_evidence_query"))
            if document.get("chunk_evidence"):
                from backend.sources.mantis_hybrid import attach_chunk_evidence
                attach_chunk_evidence(document["latest_issue"], latest_issue, document["chunk_evidence"])
            document["latest_source"] = "mantis"
        elif document.get("snapshot_issue"):
            document["latest_issue"] = document["snapshot_issue"]
            document["latest_source"] = "mantis_snapshot"
        else:
            document["latest_source"] = "chroma_snapshot"
        document.pop("snapshot_issue", None)
        documents.append(document)
        if len(documents) >= limit:
            break

    return {
        "source": "chroma_db",
        "status": "ok",
        "meta": {
            "search_keyword": search_keyword,
            "query_complete": True,
            "coverage": "top_k" if semantic else "snapshot_matches",
            "search_complete": bool(not semantic and candidate_scope_complete and not live_refresh_failures
                                    and len(candidate_documents) <= candidate_window),
            "result_window_complete": bool(not live_refresh_failures and (
                (len(documents) >= limit and (semantic or candidate_scope_complete)) or (not semantic and candidate_scope_complete
                                           and len(candidate_documents) <= candidate_window))),
            "displayed_count": len(documents),
            "candidate_count": ranking_meta.get("candidate_count", len(candidate_documents)),
            "mantis_refreshed_count": refreshed_count,
            "scanned_issue_ids": sorted(refreshed_issue_ids),
            "matched_issue_ids": [int(doc["issue_id"]) for doc in documents],
            "mantis_refresh_failures": live_refresh_failures,
            "refresh_ms": round((perf_counter() - refresh_started) * 1000, 1),
            "skipped_after_refresh": skipped_after_refresh,
            "terms": terms[:4],
            "constraints": constraints,
            **ranking_meta,
        },
        "documents": documents,
    }


def get_chroma_document(issue_id: int) -> Optional[Dict[str, Any]]:
    if not CHROMA_DB_PATH.exists():
        return None

    try:
        with closing(sqlite3.connect(CHROMA_DB_PATH)) as connection, connection:
            row = connection.execute(
                """
                SELECT doc.string_value
                FROM embedding_metadata id_meta
                JOIN embedding_metadata doc
                  ON doc.id = id_meta.id
                 AND doc.key = 'chroma:document'
                WHERE id_meta.key = 'id'
                  AND id_meta.int_value = ?
                LIMIT 1
                """,
                (issue_id,),
            ).fetchone()
    except sqlite3.Error:
        return None

    if not row:
        return None
    return {
        "issue_id": issue_id,
        "content": row[0],
    }
