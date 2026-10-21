"""대체 어휘 검색을 스트리밍하며 한 요청의 여러 질의에 같은 읽기 결과를 재사용한다."""
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import heapq
from pathlib import Path
import re
import sqlite3
from time import perf_counter

from backend.common import extract_issue_id_from_text, extract_title_from_chroma_text
from backend.config import RUNTIME_DIR
from backend.execution import checkpoint
from backend.sources.chroma_titles import (
    build_title_projection as build_title_projection,
    source_signature as _signature,
    open_title_projection,
)

TITLE_INDEX_PATH=RUNTIME_DIR/'chroma_titles.sqlite3'


def _grams(text: str) -> set[str]:
    normalized=re.sub(r'[^0-9a-z가-힣]+','',text.lower())
    return {normalized[i:i+2] for i in range(max(0,len(normalized)-1))}


def _score(query: set[str], document: set[str]) -> float:
    return 2*len(query & document)/(len(query)+len(document)) if query and document else 0.0


def semantic_character_score(query: str, document: str) -> float:
    return _score(_grams(query),_grams(document))


@dataclass
class _LexicalReuse:
    queries: tuple[str,...]
    signature: tuple | None = None
    rankings: dict[str,list[dict]] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


_REUSE: ContextVar[_LexicalReuse | None] = ContextVar('chroma_lexical_reuse',default=None)


@contextmanager
def lexical_ranking_scope(queries: list[str]):
    token=_REUSE.set(_LexicalReuse(tuple(dict.fromkeys(queries))[:3]))
    try:
        yield
    finally:
        _REUSE.reset(token)


def _projected_rankings(path: Path,queries: tuple[str,...],retained: int):
    with open_title_projection(path,TITLE_INDEX_PATH) as db:
        if db is None:
            return None
        heaps: dict[str,list[tuple]]={q:[] for q in queries}
        grams={q:_grams(q) for q in queries};scanned=0
        for position,rowid,identifier,text in db.execute('SELECT * FROM titles ORDER BY position'):
            checkpoint();scanned+=1;title=_grams(text)
            for query,query_grams in grams.items():
                score=_score(query_grams,title);heap=heaps[query]
                if score<=0 or (len(heap)>=retained and (score,-position)<=heap[0][:2]):
                    continue
                entry=(score,-position,identifier,rowid)
                if len(heap)<retained:heapq.heappush(heap,entry)
                else:heapq.heapreplace(heap,entry)
    wanted={item[3] for heap in heaps.values() for item in heap}
    content={}
    with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as raw,raw:
        keys=sorted(wanted)
        for start in range(0,len(keys),200):
            checkpoint();batch=keys[start:start+200]
            for rowid,text in raw.execute('SELECT rowid,string_value FROM embedding_metadata WHERE rowid IN ('+
                                         ','.join('?' for _ in batch)+')',batch):
                content[rowid]=(extract_issue_id_from_text(text),text[:2400])
    if any(rowid not in content or content[rowid][0]!=identifier
           for heap in heaps.values() for _score_value,_position,identifier,rowid in heap):
        return None
    rankings={q:[{'issue_id':identifier,'content':content[rowid][1],'similarity_score':round(score,6),
                  'similarity_rank':rank}
                 for rank,(score,_position,identifier,rowid) in enumerate(sorted(heap,reverse=True),1)]
              for q,heap in heaps.items()}
    return rankings,{'lexical_scanned_count':scanned,'lexical_body_reads':len(wanted),
                     'lexical_retained_candidates':sum(len(heap) for heap in heaps.values()),
                     'lexical_index_used':True}


def rank_lexical_documents(path: Path, query: str, limit: int) -> tuple[list[dict],dict]:
    reuse=_REUSE.get();signature=_signature(path)
    if reuse and reuse.signature==signature and query in reuse.rankings:
        return reuse.rankings[query][:limit],{**reuse.meta,'lexical_scan_reused':True}
    queries=reuse.queries if reuse and query in reuse.queries else (query,)
    retained=400 if reuse and query in reuse.queries else limit
    started=perf_counter()
    projected=_projected_rankings(path,queries,retained)
    if projected is not None and signature==_signature(path):
        rankings,meta=projected
        meta={**meta,'lexical_scan_reused':False,'lexical_scan_ms':round((perf_counter()-started)*1000,1)}
        if reuse and query in reuse.queries:
            reuse.signature=signature;reuse.rankings=rankings;reuse.meta=meta
        return rankings[query][:limit],meta
    grams={q:_grams(q) for q in queries}
    heaps: dict[str,list[tuple]]={q:[] for q in queries}
    started=perf_counter();scanned=0
    with closing(sqlite3.connect(path)) as db,db:
        cursor=db.execute("SELECT string_value FROM embedding_metadata WHERE key='chroma:document'")
        try:
            for position,(content,) in enumerate(cursor):
                checkpoint();scanned+=1
                title=_grams(extract_title_from_chroma_text(content))
                for text,query_grams in grams.items():
                    score=_score(query_grams,title)
                    heap=heaps[text]
                    if score<=0 or (len(heap)>=retained and (score,-position)<=heap[0][:2]):
                        continue
                    # 원문 본문은 보존하지 않는다. 표시할 상위 후보의 기존 2400자만 유지한다.
                    entry=(score,-position,extract_issue_id_from_text(content),content[:2400])
                    if len(heap)<retained:
                        heapq.heappush(heap,entry)
                    else:
                        heapq.heapreplace(heap,entry)
        finally:
            cursor.close()
    rankings={text:[{'issue_id':identifier,'content':content,'similarity_score':round(score,6),
                     'similarity_rank':rank}
                    for rank,(score,_position,identifier,content) in enumerate(sorted(heap,reverse=True),1)]
              for text,heap in heaps.items()}
    meta={'lexical_scan_reused':False,'lexical_scanned_count':scanned,
          'lexical_index_used':False,'lexical_body_reads':scanned,
          'lexical_scan_ms':round((perf_counter()-started)*1000,1),
          'lexical_retained_candidates':sum(len(heap) for heap in heaps.values())}
    if reuse and query in reuse.queries and signature==_signature(path):
        reuse.signature=signature;reuse.rankings=rankings;reuse.meta=meta
    return rankings[query][:limit],meta
