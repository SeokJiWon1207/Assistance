"""본문 벡터와 저장된 제목·본문 키워드를 결합하는 후보 선정. 현재 근거는 아니다."""
import json
import re

from backend.execution import checkpoint
from backend.notion.body_index import BodyIndexError, open_body_index
from backend.notion.page_ownership import segment_belongs_to_page
from backend.notion.semantic_index import prefer_scope_candidates
from backend.notion.values import notion_title
from backend.notion.related_guides import guide_passage, topic_terms
from backend.utils.text import normalize_for_match

_GENERIC = {'가이드', '문서', '관련', '설명', '방법', '사용', '사용법', '매뉴얼', '절차', '이슈', '확인',
            '추가', '업데이트', '실시간', 'mts', 'hts', 'guide', 'manual'}
_GUIDE = re.compile(r'가이드|매뉴얼|메뉴얼|설명서|사용법|방법|절차|guide|manual', re.I)
_TICKET = re.compile(r'요청|문의|현상|회의|진행상황|오류리스트')


def hybrid_candidates(query: str, database_ids: list[str], *, semantic: list[dict] | None,
                      constraints: dict | None = None, index=None, limit=60) -> list[dict]:
    index = index or open_body_index()
    ranked = {r['page_id']: {**r, 'candidate_only': True} for r in semantic or []}
    if index is None:
        return list(ranked.values())[:limit]
    customer_terms = list((constraints or {}).get('customer_aliases') or [])
    for clause in (constraints or {}).get('filter_clauses') or []:
        if clause.get('field') == 'customer':
            value = clause.get('value')
            customer_terms.extend(value if isinstance(value, list) else [value])
    customer_terms = [normalize_for_match(str(t)) for t in customer_terms if t]
    tokens = [normalize_for_match(t) for t in re.findall(r'[\w-]+', query)
              if len(t) >= 2 and t.lower() not in _GENERIC and normalize_for_match(t) not in customer_terms]
    specific = topic_terms(query, tokens)
    # 화면번호로 찾는 제목 검색은 유지한다. 일반적인 화면·데이터 단어만 후보 근거에서 뺀다.
    terms = [t for t in tokens if t in specific or t.isdecimal()][:6]
    if not terms:
        return list(ranked.values())[:limit]
    try:
        # 제목은 본문 미수집·빈 문서도 포함한다. 완전성 주장에는 쓰지 않는다.
        places, params = index._scope(database_ids)
        if not places:
            return []
        bodies = {}
        for record in index.search(database_ids, terms):
            owned = '\n'.join(s['text'] for s in record['segments']
                              if segment_belongs_to_page(s, record['page_id']))
            bodies[record['page_id']] = owned
        with index._database() as db:
            rows = db.execute(f'SELECT page_id,database_id,raw_page FROM pages WHERE fingerprint=? AND database_id IN ({places})', params)
            for row in rows:
                checkpoint()
                title = notion_title(json.loads(row['raw_page']).get('properties', {}))
                title_key = normalize_for_match(title)
                body = bodies.get(row['page_id'], '')
                hits = sum(t in title_key or t in normalize_for_match(body) for t in terms)
                if not hits:
                    continue
                guide = bool(_GUIDE.search(title) and not _TICKET.search(title))
                passage, body_guide = guide_passage(title, body, specific)
                old = ranked.get(row['page_id'], {})
                ranked[row['page_id']] = {**old, 'page_id': row['page_id'], 'database_id': row['database_id'],
                    'score': old.get('score', 0.0), 'candidate_only': True, 'lexical_hits': hits,
                    'guide_hint': guide or body_guide, 'body_guide_hint': body_guide,
                    **({'text': f'{title}\n{passage}'} if body_guide else {}),
                    'title_hit': any(t in title_key for t in terms), 'matched_terms': terms}
        lexical = sorted((r for r in ranked.values() if r.get('lexical_hits')),
            key=lambda r: (r.get('guide_hint', False), r['lexical_hits'], r.get('title_hit', False), r['score']), reverse=True)
        positions = [{row['page_id']: i + 1 for i, row in enumerate(source)} for source in (semantic or [], lexical)]
        # 키워드 일치 이슈 수백 건이 의미가 가까운 설명서를 전부 밀어내지 않게 두 순위를 결합한다.
        candidates = sorted(ranked.values(), key=lambda row: sum(
            1 / (60 + rank[row['page_id']]) for rank in positions if row['page_id'] in rank)
            + (.025 if row.get('body_guide_hint') else .005 if row.get('guide_hint') else 0), reverse=True)
        return prefer_scope_candidates(index, candidates, constraints or {})[:limit]
    except BodyIndexError:
        return list(ranked.values())[:limit]
