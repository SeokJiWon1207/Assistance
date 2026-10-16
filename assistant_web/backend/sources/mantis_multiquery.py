"""같은 조건에서 원문 질의와 보조 질의의 검색 후보를 합친다."""
from collections import defaultdict
from copy import deepcopy
import os
from time import perf_counter
from typing import Any, Callable

import requests
from fastapi import HTTPException

from backend.execution import checkpoint, RequestStopped


def _rerank_union(query: str, ranked: list[int], documents: dict[int, dict[str, Any]],
                  scores: dict[int, float]) -> tuple[list[int], str]:
    """보조 질의 한 곳에서 찾은 구체적 근거가 반복 등장한 일반 후보에 밀리지 않게 한다."""
    if (not ranked or os.getenv('MANTIS_HYBRID_RERANK_ENABLED', 'true').lower() == 'false'
            or any(not documents[identifier].get('chunk_evidence') for identifier in ranked)):
        return ranked, 'not_needed'
    from backend.sources.mantis_chunks import embedding_text
    from backend.sources.mantis_hybrid import _cosines, embed_texts
    try:
        checkpoint()
        # 앞선 검색에서 이미 계산한 query/원문 벡터를 같은 요청 캐시에서 재사용한다.
        vectors = embed_texts([query[:1200], *(embedding_text(documents[i]['chunk_evidence']) for i in ranked)])
        similarities = _cosines(vectors, len(ranked) + 1)
        checkpoint()
        maximum = max(scores.values())
        relevance = {identifier: 0.85 * similarity + 0.15 * scores[identifier] / maximum
                     for identifier, similarity in zip(ranked, similarities, strict=True)}
        return sorted(ranked, key=lambda identifier: -relevance[identifier]), 'original_query_cosine'
    except RequestStopped:
        raise
    except (requests.RequestException, ValueError, TypeError, KeyError, OverflowError):
        return ranked, 'rrf_fallback'


def search_explanation_queries(query: str, queries: list[str], limit: int, constraints: dict[str, Any], *,
                               search: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    from backend.sources.mantis_hybrid import rerank_embedding_scope
    with rerank_embedding_scope() as reuse:
        result = _search_explanation_queries(query, queries, limit, constraints, search=search)
        if 'multiquery' in result.get('meta', {}):
            result['meta']['multiquery']['embedding_reuse'] = {
                'reused_passages': reuse.reused, 'embedded_passages': reuse.embedded}
        return result


def _search_explanation_queries(query: str, queries: list[str], limit: int, constraints: dict[str, Any], *,
                                search: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    started = perf_counter()
    original = search(query, limit, deepcopy(constraints), semantic=True)
    if os.getenv('MANTIS_MULTIQUERY_ENABLED', 'true').lower() == 'false':
        return original
    auxiliary = list(dict.fromkeys(q for q in queries if isinstance(q, str) and q.strip() and q != query))[:2]
    if not auxiliary:
        return original
    stages = [original]
    failures = 0
    for candidate in auxiliary:
        checkpoint()
        try:
            stage = search(candidate, limit, deepcopy(constraints), semantic=True)
            if stage.get('status') == 'error':
                failures += 1
            else:
                stages.append(stage)
        except (requests.RequestException, HTTPException, OSError, ValueError):
            checkpoint()
            failures += 1
    scores: dict[int, float] = defaultdict(float)
    documents: dict[int, dict[str, Any]] = {}
    excluded: set[int] = set()
    scanned: set[int] = set()
    refresh_failures = 0
    for stage in stages:
        meta = stage.get('meta') or {}
        checked = {int(identifier) for identifier in meta.get('scanned_issue_ids', [])}
        matched = {int(identifier) for identifier in meta.get('matched_issue_ids', [])}
        rejected = checked - matched
        scanned.update(checked)
        excluded.update(rejected)
        refresh_failures += int(meta.get('mantis_refresh_failures') or 0)
        for identifier in rejected:
            scores.pop(identifier, None)
            documents.pop(identifier, None)
        seen = set()
        for rank, document in enumerate(stage.get('documents') or [], 1):
            identifier = int(document.get('issue_id') or 0)
            live = identifier in checked & matched and document.get('latest_source') == 'mantis'
            if live:
                excluded.discard(identifier)
            if identifier in excluded:
                continue
            if not identifier or identifier in seen:
                continue
            seen.add(identifier)
            scores[identifier] += 1 / (60 + rank)
            # 같은 이슈의 서로 다른 시점 발췌를 이어 붙이지 않는다.
            if live or identifier not in documents:
                documents[identifier] = document
    ranked = sorted(scores, key=lambda identifier: -scores[identifier])
    ranked, union_reranking = _rerank_union(query, ranked, documents, scores)
    selected = ranked[:limit]
    protected = [int(d['issue_id']) for d in (original.get('documents') or [])[:min(2, limit)]]
    for identifier in protected:
        if identifier in documents and identifier not in selected:
            replace = next((i for i in range(len(selected) - 1, -1, -1) if selected[i] not in protected), None)
            if replace is not None:
                selected[replace] = identifier
    selected.sort(key=ranked.index)
    return {**original, 'status': 'ok' if selected else original.get('status', 'not_found'),
            'documents': [{**documents[identifier], 'similarity_rank': rank}
                          for rank, identifier in enumerate(selected, 1)],
            'meta': {**original.get('meta', {}), 'search_complete': False, 'coverage': 'top_k',
                     'displayed_count': len(selected), 'matched_issue_ids': selected,
                     'scanned_issue_ids': sorted(scanned), 'mantis_refresh_failures': refresh_failures,
                     'result_window_complete': not failures and not refresh_failures,
                     'multiquery': {'queries': [query, *auxiliary], 'failed_queries': failures,
                                    'union_reranking': union_reranking,
                                    'candidate_count': len(documents), 'protected_issue_ids': protected,
                                    'excluded_issue_ids': sorted(excluded),
                                    'elapsed_ms': round((perf_counter() - started) * 1000, 1)}}}
