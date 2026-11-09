"""로컬 Notion 본문 벡터 후보 색인. 벡터/저장 텍스트 자체는 현재 근거가 아니다."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit

import numpy as np
import requests

from backend.config import RUNTIME_DIR
from backend.execution import bounded_timeout, checkpoint
from backend.notion.body_index import BodyIndexError, open_body_index
from backend.notion.page_ownership import _id, segment_belongs_to_page
from backend.notion.values import notion_title
from backend.storage.profiles import NOTION_BODY, NOTION_BODY_INIT
from backend.storage.sqlite import open_sqlite
from backend.tools.redaction import redact_text


def chunks(record: dict) -> list[dict]:
    identifier = _id(record['page_id'])
    # 새 부분본이 같은 블록의 마지막 완전본을 대체한다. 외부 소유 블록은 섞지 않는다.
    segments = {s['block_id']: s for s in record.get('segments', [])
                if s.get('block_id') and s.get('text') and segment_belongs_to_page(s, identifier)}
    # 구간 경계에서 인증정보의 이름과 값이 갈라지기 전에 전체 본문을 가린다.
    body = redact_text('\n'.join(s['text'] for s in segments.values()))
    if not body.strip():
        return []
    title = redact_text(notion_title((record.get('raw_page') or {}).get('properties', {})))[:160]
    result: list[dict] = []
    for start in range(0, len(body), 1000):
        text = redact_text(f'{title}\n{body[start:start + 1200]}')
        result.append({'page_id': identifier, 'database_id': _id(record['database_id']),
                       'ordinal': len(result), 'text': text,
                       'digest': hashlib.sha256(text.encode()).hexdigest()})
        if start + 1200 >= len(body):
            break
    return result


def _vectors(values, expected: int) -> np.ndarray:
    vectors = np.asarray(values, dtype=np.float32)
    if vectors.ndim != 2 or vectors.shape[0] != expected or not vectors.shape[1] or not np.isfinite(vectors).all():
        raise ValueError('임베딩 응답의 개수·차원·값이 올바르지 않습니다.')
    norms = np.linalg.norm(vectors.astype(np.float64), axis=1, keepdims=True)
    if (norms <= 0).any():
        raise ValueError('빈 임베딩입니다.')
    return (vectors / norms).astype(np.float32)


class SemanticIndex:
    def __init__(self, path: Path, connection: str, model: str, *, create: bool = True):
        self.path, self.connection, self.model = path, connection, model
        if create:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with closing(open_sqlite(path, profile=NOTION_BODY_INIT)) as db, db:
                path.chmod(0o600)
                db.execute('''CREATE TABLE IF NOT EXISTS vectors (
                    connection TEXT NOT NULL, model TEXT NOT NULL, page_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
                    database_id TEXT NOT NULL, digest TEXT NOT NULL, text TEXT NOT NULL, vector BLOB NOT NULL,
                    PRIMARY KEY(connection,model,page_id,ordinal))''')

    @contextmanager
    def _database(self, *, write=False):
        with closing(open_sqlite(self.path.resolve().as_uri() + ('?mode=rw' if write else '?mode=ro'),
                                 profile=NOTION_BODY)) as db, db:
            yield db

    def sync(self, documents: list[dict], *, embed, max_chunks=256, max_seconds=30) -> dict:
        deadline = time.monotonic() + max_seconds
        parts = [part for document in documents for part in chunks(document)]
        with self._database() as db:
            old = {(r['page_id'], r['ordinal']): dict(r) for r in db.execute(
                'SELECT page_id,ordinal,digest,length(vector) AS size FROM vectors WHERE connection=? AND model=?',
                (self.connection, self.model))}
        pending = [p for p in parts if old.get((p['page_id'], p['ordinal']), {}).get('digest') != p['digest']]
        published = 0
        dimension = next((r['size'] // 4 for r in old.values()), None)
        for offset in range(0, min(len(pending), max(0, max_chunks)), 8):
            checkpoint()
            if time.monotonic() >= deadline:
                break
            batch = pending[offset:min(offset + 8, max_chunks)]
            vectors = _vectors(embed([p['text'] for p in batch]), len(batch))
            if dimension is not None and vectors.shape[1] != dimension:
                raise ValueError('저장된 모델의 임베딩 차원이 변경되었습니다.')
            dimension = vectors.shape[1]
            with self._database(write=True) as db:
                db.executemany('INSERT OR REPLACE INTO vectors VALUES (?,?,?,?,?,?,?,?)', [
                    (self.connection, self.model, p['page_id'], p['ordinal'], p['database_id'], p['digest'],
                     p['text'], vector.astype('<f4').tobytes()) for p, vector in zip(batch, vectors, strict=True)])
            published += len(batch)
        wanted = {(p['page_id'], p['ordinal']) for p in parts}
        with self._database(write=True) as db:
            db.executemany('DELETE FROM vectors WHERE connection=? AND model=? AND page_id=? AND ordinal=?',
                           [(self.connection, self.model, *key) for key in old if key not in wanted])
            # 소속 변경만으로 동일한 본문을 다시 임베딩하지 않는다.
            db.executemany('UPDATE vectors SET database_id=? WHERE connection=? AND model=? AND page_id=? AND ordinal=?',
                           [(p['database_id'], self.connection, self.model, p['page_id'], p['ordinal']) for p in parts])
        return {'embedded_chunks': published, 'pending_chunks': len(pending) - published,
                'source_chunks': len(parts), 'source_pages': len({p['page_id'] for p in parts})}

    def search(self, vector: list[float], database_ids: list[str], *, limit=10) -> list[dict]:
        if not database_ids:
            return []
        values = [_id(value) for value in database_ids]
        with self._database() as db:
            rows = db.execute('SELECT * FROM vectors WHERE connection=? AND model=? AND database_id IN (' +
                              ','.join('?' for _ in values) + ')', [self.connection, self.model, *values]).fetchall()
        if not rows:
            return []
        query = _vectors([vector], 1)[0]
        matrix = np.stack([np.frombuffer(r['vector'], dtype='<f4') for r in rows])
        if matrix.shape[1] != len(query) or not np.isfinite(matrix).all():
            raise ValueError('색인의 임베딩 차원·값이 올바르지 않습니다.')
        scores = matrix @ query
        seen: set[str] = set()
        result = []
        for pos in np.argsort(-scores):
            row = rows[int(pos)]
            if row['page_id'] in seen:
                continue
            seen.add(row['page_id'])
            result.append({k: row[k] for k in ('page_id', 'database_id', 'text', 'digest')})
            result[-1]['score'] = float(scores[pos])
            if len(result) >= max(1, limit):
                break
        return result


def embedding_config() -> tuple[str, str, str]:
    base = os.getenv('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
    url = urlsplit(base)
    if url.scheme not in {'http', 'https'} or url.hostname not in {'127.0.0.1', 'localhost', '::1'} or url.username or url.password:
        raise ValueError('Notion 본문 임베딩은 로컬 서비스만 허용합니다.')
    model = os.getenv('CHROMA_EMBEDDING_MODEL', 'bge-m3:latest')
    key = hashlib.sha256(json.dumps([base, model, 'notion-chunks-v1']).encode()).hexdigest()
    return base + '/api/embed', model, key


def embed_local(texts: list[str]) -> list[list[float]]:
    endpoint, model, _ = embedding_config()
    checkpoint()
    with requests.post(endpoint, json={'model': model, 'input': [redact_text(t) for t in texts], 'truncate': False},
                       timeout=bounded_timeout(30), allow_redirects=False) as response:
        if response.is_redirect:
            raise ValueError('임베딩 리디렉션을 허용하지 않습니다.')
        response.raise_for_status()
        vectors = response.json()['embeddings']
    _vectors(vectors, len(texts))
    return vectors


def semantic_path() -> Path:
    return RUNTIME_DIR / 'notion_semantic.sqlite3'


def source_documents(index, database_ids: list[str]) -> list[dict]:
    places, params = index._scope(database_ids)
    if not places:
        return []
    with index._database() as db:
        tables = ['blocks'] + (['partial_blocks'] if index._has_partial_table(db) else [])
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='synced_segments'").fetchone():
            tables.append('synced_segments')
        has_body = ' OR '.join(f'EXISTS (SELECT 1 FROM {table} b WHERE b.fingerprint=p.fingerprint AND b.page_id=p.page_id)'
                               for table in tables)
        rows = db.execute(f'SELECT p.* FROM pages p WHERE fingerprint=? AND database_id IN ({places}) AND ({has_body})', params)
        result = []
        for row in rows:
            checkpoint()
            record = index._record(db, row)
            result.append({**record, 'segments': record['candidate_segments']})
        return result


def sync_semantic_index(*, max_chunks=256, max_seconds=30) -> dict:
    from backend.notion.discovery import resolve_notion_settings
    from backend.notion.read_scope import resolve_notion_read_scope
    if os.getenv('NOTION_SEMANTIC_SEARCH_ENABLED', 'true').lower() in {'false', '0', 'off'}:
        return {'status': 'disabled'}
    index = open_body_index()
    if index is None:
        return {'status': 'body_index_unavailable'}
    settings = resolve_notion_settings()
    scope, complete = resolve_notion_read_scope(settings['database_ids'], settings['token'])
    if not complete:
        return {'status': 'scope_incomplete'}
    _, _, model = embedding_config()
    vectors = SemanticIndex(semantic_path(), index.fingerprint, model)
    result = vectors.sync(source_documents(index, list(scope)), embed=embed_local,
                          max_chunks=max_chunks, max_seconds=max_seconds)
    return {'status': 'ready' if not result['pending_chunks'] else 'partial', **result}


def prefer_scope_candidates(index, candidates: list[dict], constraints: dict) -> list[dict]:
    """저장된 고객사·매체는 순서 결정에만 쓴다. 실제 조건은 현재 페이지에서 재검증한다."""
    from backend.notion.mapping import normalize_notion_page
    from backend.notion.matching import notion_page_matches_common_filters
    from backend.notion.filter_conditions import merge_common_notion_constraints
    filters = [c for c in merge_common_notion_constraints({}, constraints).get('common_filter_clauses', [])
               if c.get('field') in {'customer', 'platform'}]
    if not filters or not candidates:
        return candidates
    wanted = {r['page_id'] for r in candidates}
    matched: set[str] = set()
    with index._database() as db:
        for row in db.execute('SELECT page_id,database_id,raw_page FROM pages WHERE fingerprint=?', (index.fingerprint,)):
            if row['page_id'] not in wanted:
                continue
            page = normalize_notion_page(json.loads(row['raw_page']), include_blocks=False, database_id=row['database_id'])
            if notion_page_matches_common_filters(page, filters):
                matched.add(row['page_id'])
    return sorted(candidates, key=lambda p: p['page_id'] not in matched)


def semantic_candidates(query: str, database_ids: list[str], *, limit=15, constraints: dict | None = None) -> list[dict] | None:
    if os.getenv('NOTION_SEMANTIC_SEARCH_ENABLED', 'true').lower() in {'false', '0', 'off'} or not semantic_path().exists():
        return None
    index = open_body_index()
    if index is None:
        return None
    try:
        _, _, model = embedding_config()
        candidates = SemanticIndex(semantic_path(), index.fingerprint, model, create=False)
        ranked = candidates.search(embed_local([query])[0], database_ids, limit=max(limit, 5000) if constraints else limit)
        return prefer_scope_candidates(index, ranked, constraints or {})[:limit]
    except (BodyIndexError, sqlite3.Error, OSError, ValueError, KeyError, requests.RequestException):
        return None
