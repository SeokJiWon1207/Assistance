"""구간 BM25와 기존 이슈 벡터 순위를 결합하고 후보 구간을 의미 재순위화한다."""
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from time import perf_counter
from typing import Any, Callable

import requests

from backend.common import clamp_int, normalize_for_match
from backend.config import RUNTIME_DIR
from backend.execution import bounded_timeout, checkpoint, RequestStopped
from backend.sources.mantis import issue_matches_constraints, issue_to_search_text, normalize_issue
from backend.sources.mantis_chunk_index import open_index, lexical_chunks, start_index_build
from backend.sources.mantis_chunks import embedding_text, split_issue
from backend.sources.mantis_store import connection_key, store_path

INDEX_PATH = RUNTIME_DIR / "mantis_chunks.sqlite3"
VectorSearch = Callable[..., tuple[list[dict[str, Any]], dict[str, Any]]]


@dataclass
class EmbeddingReuse:
    vectors: dict[tuple[str, str, str], list[float]] = field(default_factory=dict)
    reused: int = 0
    embedded: int = 0


_EMBEDDING_REUSE: ContextVar[EmbeddingReuse | None] = ContextVar('mantis_embedding_reuse', default=None)


@contextmanager
def rerank_embedding_scope():
    """한 번의 다중 검색에서만 같은 원문의 계산을 재사용한다."""
    reuse = EmbeddingReuse()
    token = _EMBEDDING_REUSE.set(reuse)
    try:
        yield reuse
    finally:
        _EMBEDDING_REUSE.reset(token)


_FILTER_REUSE: ContextVar[dict | None] = ContextVar('mantis_header_filter_reuse', default=None)


@contextmanager
def hybrid_filter_scope():
    token = _FILTER_REUSE.set({})
    try:
        yield
    finally:
        _FILTER_REUSE.reset(token)


def _scope_candidate_ids(raw_db, constraints: dict) -> set[int] | None:
    narrow = ('customer_exact', 'customer_aliases', 'excluded_customer_aliases',
              'platform_aliases', 'excluded_platform_aliases')
    if not any(constraints.get(key) for key in narrow):
        return None
    keys = (*narrow, 'mantis_team_scope', 'exclude_issue_ids', 'max_issue_id_exclusive')
    subset = {key: constraints[key] for key in keys if key in constraints}
    allowed = set()
    # 같은 원본 트랜잭션을 순차로 읽는다. 본문·노트·이력은 Python에 적재하지 않는다.
    for row in raw_db.execute("SELECT id, json_extract(payload,'$.project'), "
                              "json_extract(payload,'$.handler'), json_extract(payload,'$.custom_fields') FROM issues"):
        checkpoint()
        header = {'id': row[0], 'project': json.loads(row[1] or '{}'),
                  'handler': json.loads(row[2] or '{}'), 'custom_fields': json.loads(row[3] or '[]')}
        if issue_matches_constraints(header, subset, search_text=''):
            allowed.add(row[0])
    return allowed


def _identifier_tail_terms(query: str) -> list[str]:
    """분리 표기된 클래스·함수명을 찾도록 점 뒤 식별자도 독립 검색한다."""
    terms = []
    for match in re.finditer(r"(?<![\w.])([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))+", query):
        head, tail = match.group(1), match.group(2)
        if len(tail) >= 5 and (re.search(r"[A-Z_0-9]", head) or re.search(r"[A-Z_0-9]", tail)):
            terms.append(tail)
    return list(dict.fromkeys(terms))[:3]


def embed_texts(texts: list[str]) -> list[list[float]]:
    checkpoint()
    endpoint = f"{os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')}/api/embed"
    model = os.getenv("CHROMA_EMBEDDING_MODEL", "bge-m3:latest")
    reuse = _EMBEDDING_REUSE.get()
    keys = [(endpoint, model, text) for text in texts]
    cached = reuse.vectors if reuse is not None else {}
    missing = list(dict.fromkeys(key for key in keys if key not in cached))
    if reuse is not None:
        reuse.reused += sum(key in cached for key in keys)
    if not missing:
        return [cached[key] for key in keys]
    timeout = clamp_int(os.getenv("MANTIS_RERANK_TIMEOUT_SECONDS", "12"), 2, 60) or 12
    response = requests.post(
        endpoint, json={"model": model, "input": [key[2] for key in missing]},
        timeout=bounded_timeout(timeout),
    )
    response.raise_for_status()
    vectors = response.json()["embeddings"]
    # 실패·부분 응답·유효하지 않은 벡터는 후속 검색의 재사용 대상이 아니다.
    _cosines(vectors, len(missing))
    fresh = dict(zip(missing, vectors, strict=True))
    result = [cached[key] if key in cached else fresh[key] for key in keys]
    _cosines(result, len(keys))
    if reuse is not None:
        reuse.embedded += len(missing)
        for key, vector in fresh.items():
            if len(cached) >= 256:
                break
            cached[key] = vector
    return result


def _cosines(vectors, expected: int) -> list[float]:
    if not isinstance(vectors, list) or len(vectors) != expected or not vectors or not vectors[0]:
        raise ValueError("임베딩 개수가 다릅니다.")
    dimension = len(vectors[0])
    normalized = []
    for vector in vectors:
        if len(vector) != dimension:
            raise ValueError("임베딩 차원이 다릅니다.")
        row = [float(value) for value in vector]
        norm = math.sqrt(sum(value * value for value in row))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("유효하지 않은 임베딩입니다.")
        normalized.append([value / norm for value in row])
    return [sum(a * b for a, b in zip(normalized[0], row, strict=True)) for row in normalized[1:]]


def attach_chunk_evidence(normalized: dict, raw: dict, chunk: dict) -> None:
    """최신 재조회 후에도 같은 원문인 구간만 답변 근거로 전달한다."""
    for current in split_issue(raw):
        if (current["field"] == chunk.get("field") and current["start"] == chunk.get("start")
                and current["text"] == chunk.get("text")):
            evidence = {key: chunk[key] for key in ("field", "start", "end", "text")}
            if chunk["field"].startswith("note:"):
                evidence["note_id"] = chunk["field"].split(":", 1)[1]
            normalized["matching_evidence"] = [evidence, *normalized.get("matching_evidence", [])][:6]
            return


def search_hybrid(
    query: str, limit: int, constraints: dict, vector_search: VectorSearch, *,
    source: Path | None = None, target: Path | None = None, identity: str | None = None,
    embed: Callable | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    source, target = source or store_path(), target or INDEX_PATH
    if not source.is_file():
        return None
    identity = connection_key() if identity is None else identity
    started = perf_counter()
    with open_index(source, target, identity) as opened:
        if opened is None:
            start_index_build(source, target, identity)
            return None
        db, raw_db, index_meta = opened
        candidate_limit = min(max(limit * 6, 30), 80)
        raw_issues: dict[int, dict | None] = {}
        scope_started = perf_counter()
        scope_cache = _FILTER_REUSE.get()
        scope_key = (str(source.resolve()), index_meta['fetchedAt'], index_meta['count'],
                     json.dumps(constraints, sort_keys=True, ensure_ascii=False))
        scope_reused = scope_cache is not None and scope_key in scope_cache
        allowed_ids = scope_cache[scope_key] if scope_reused and scope_cache is not None else _scope_candidate_ids(raw_db, constraints)
        if scope_cache is not None and not scope_reused:
            scope_cache[scope_key] = allowed_ids
        scope_ms = (perf_counter() - scope_started) * 1000
        raw_reads = 0

        def eligible(issue_id):
            nonlocal raw_reads
            if allowed_ids is not None and issue_id not in allowed_ids:
                return None
            if issue_id not in raw_issues:
                checkpoint()
                raw_reads += 1
                row = raw_db.execute("SELECT payload FROM issues WHERE id=?", [issue_id]).fetchone()
                raw = json.loads(row[0]) if row else None
                raw_issues[issue_id] = raw if raw and issue_matches_constraints(raw, constraints) else None
            return raw_issues[issue_id]

        chunks: dict[int, list[dict]] = {}
        lexical_ids = []
        identifier_candidates = 0
        if constraints.get("answer_query"):
            for term in _identifier_tail_terms(query):
                focused = lexical_chunks(db, term, issue_ids=allowed_ids)
                try:
                    for chunk in focused:
                        issue_id = chunk["issue_id"]
                        if issue_id in chunks or not eligible(issue_id):
                            continue
                        chunks[issue_id] = [chunk]
                        lexical_ids.append(issue_id)
                        identifier_candidates += 1
                        if identifier_candidates >= min(candidate_limit, 8):
                            break
                finally:
                    focused.close()
                if identifier_candidates >= min(candidate_limit, 8):
                    break
        with_closing = lexical_chunks(db, query, issue_ids=allowed_ids)
        try:
            for chunk in with_closing:
                issue_id = chunk["issue_id"]
                if issue_id in chunks or not eligible(issue_id):
                    continue
                chunks[issue_id] = [chunk]
                lexical_ids.append(issue_id)
                if len(lexical_ids) >= candidate_limit:
                    break
        finally:
            with_closing.close()

        checkpoint()
        lexical_ms = (perf_counter() - started) * 1000
        vector_started = perf_counter()
        vector_error = ""
        try:
            vector_docs, vector_meta = vector_search(query, limit, constraints)
        except RequestStopped:
            raise
        except (OSError, sqlite3.Error, requests.RequestException, ValueError):
            vector_docs, vector_meta = [], {}
            vector_error = "vector_unavailable"
        vector_ms = (perf_counter() - vector_started) * 1000
        vector_ids = []
        for document in vector_docs:
            issue_id = int(document.get("issue_id") or 0)
            if not issue_id or issue_id in vector_ids or not eligible(issue_id):
                continue
            if issue_id not in chunks:
                # 키워드 일치가 없는 후보도 제목뿐 아니라 실제 본문을 의미 비교한다.
                rows = db.execute("SELECT * FROM chunks WHERE issue_id=? ORDER BY (field='summary'), id LIMIT 2", [issue_id])
                chunks[issue_id] = [dict(row) for row in rows]
            if chunks[issue_id]:
                vector_ids.append(issue_id)
            if len(vector_ids) >= candidate_limit:
                break
        scores: dict[int, float] = defaultdict(float)
        for ids in (lexical_ids, vector_ids):
            for rank, issue_id in enumerate(ids, 1):
                scores[issue_id] += 1.0 / (60 + rank)
        focus_terms = [term for term in constraints.get("semantic_focus_terms", []) if len(normalize_for_match(term)) >= 2]
        focus_term = None
        if focus_terms and scores:
            texts = {issue_id: normalize_for_match(issue_to_search_text(raw_issues[issue_id] or {})) for issue_id in scores}
            frequencies = {term: sum(normalize_for_match(term) in text for text in texts.values()) for term in focus_terms}
            meaningful = [term for term in focus_terms if frequencies[term]]
            if meaningful:
                focus_term = min(meaningful, key=lambda term: frequencies[term])
                scores = {issue_id: score for issue_id, score in scores.items()
                          if normalize_for_match(focus_term) in texts[issue_id]}
            elif constraints.get("require_semantic_focus"):
                scores = {}
        fusion_order = sorted(scores, key=lambda issue_id: (-scores[issue_id], -issue_id))
        ranked = fusion_order[:candidate_limit]
        reranking, rerank_error = "not_needed", ""
        semantic_scores: dict[int, float] = {}
        selected = {issue_id: chunks[issue_id][0] for issue_id in ranked}
        rerank_started = perf_counter()
        if ranked and os.getenv("MANTIS_HYBRID_RERANK_ENABLED", "true").lower() != "false":
            top = ranked[:24]
            passages = [chunk for issue_id in top for chunk in chunks[issue_id]]
            try:
                checkpoint()
                vectors = (embed or embed_texts)([query[:1200], *(embedding_text(chunk) for chunk in passages)])
                similarities = _cosines(vectors, len(passages) + 1)
                checkpoint()
                for chunk, score in zip(passages, similarities, strict=True):
                    issue_id = chunk["issue_id"]
                    if score > semantic_scores.get(issue_id, -math.inf):
                        semantic_scores[issue_id] = score
                        selected[issue_id] = chunk
                max_rrf = max(scores.values())
                ranked = sorted(top, key=lambda issue_id: (
                    -(0.85 * semantic_scores[issue_id] + 0.15 * scores[issue_id] / max_rrf),
                    fusion_order.index(issue_id),
                )) + ranked[len(top):]
                reranking = "chunk_embedding_cosine"
            except RequestStopped:
                raise
            except (requests.RequestException, ValueError, TypeError, KeyError, OverflowError):
                reranking, rerank_error = "rrf_fallback", "embedding_unavailable_or_invalid"
        elif ranked:
            reranking = "disabled"
        # 설명에서는 정확한 원문 검색 1위가 의미 순위에 밀려 근거에서 사라지지 않게 한다.
        # 조건·주제 필터를 통과한 후보만 마지막 표시 슬롯에 보존하며 앞선 의미 순위는 유지한다.
        # 표시 슬롯이 하나뿐이면 원문 검색 1위를 우선한다. 일반 검색에는 적용하지 않는다.
        lexical_leader = lexical_ids[0] if lexical_ids else None
        if (constraints.get("answer_query") and lexical_leader in scores and limit > 0
                and lexical_leader not in ranked[:limit]):
            ranked = [issue_id for issue_id in ranked if issue_id != lexical_leader]
            ranked.insert(min(limit - 1, len(ranked)), lexical_leader)
            ranked = ranked[:candidate_limit]
            selected.setdefault(lexical_leader, chunks[lexical_leader][0])
        documents = []
        for rank, issue_id in enumerate(ranked, 1):
            raw = raw_issues[issue_id]
            assert raw is not None
            chunk = selected[issue_id]
            normalized = normalize_issue(raw, evidence_query=constraints.get("answer_query") or constraints.get("resolution_evidence_query"))
            attach_chunk_evidence(normalized, raw, chunk)
            content = (f"[Mantis 이슈 정보]\n- 이슈 ID: #{issue_id}\n"
                       f"- 프로젝트(고객사): {normalized['project']}\n"
                       f"[제목]\n{normalized['summary']}\n[검색 근거]\n{chunk['text']}")
            documents.append({"issue_id": issue_id, "content": content, "similarity_rank": rank,
                              "fusion_rank": fusion_order.index(issue_id) + 1,
                              "rrf_score": scores[issue_id], "rerank_score": semantic_scores.get(issue_id),
                              "chunk_evidence": chunk, "snapshot_issue": normalized})
        return documents, {
            "ranking": "hybrid_chunk_rrf", "reranking": reranking, "reranking_fallback_reason": rerank_error,
            "vector_ranking": vector_meta.get("ranking", vector_error),
            "vector_fallback_reason": vector_meta.get("ranking_fallback_reason", vector_error),
            "lexical_candidates": len(lexical_ids), "vector_candidates": len(vector_ids),
            "identifier_candidates": identifier_candidates,
            "candidate_count": len(documents), "fetchedAt": index_meta["fetchedAt"],
            "semantic_focus_term": focus_term,
            "scope_filter_ms": round(scope_ms, 1), "scope_filter_reused": scope_reused,
            "scope_candidate_count": len(allowed_ids) if allowed_ids is not None else None,
            "raw_issue_reads": raw_reads,
            "lexical_ms": round(lexical_ms, 1), "vector_ms": round(vector_ms, 1),
            "reranking_ms": round((perf_counter() - rerank_started) * 1000, 1),
            "hybrid_search_ms": round((perf_counter() - started) * 1000, 1),
        }
