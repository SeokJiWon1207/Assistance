"""임베딩 불가 시 원문 전체 적재 없이 같은 후보 순서를 계산한다."""
import sqlite3
import sys
import tempfile
import tracemalloc
import types
import unittest
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch

import requests
from backend.sources import chroma


class LexicalMemoryTests(unittest.TestCase):
    def test_chunk_ranking_sorts_only_identifiers_and_loads_selected_text_lazily(self):
        from backend.sources.mantis_chunk_index import lexical_chunks
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory=sqlite3.Row
            db.executescript("CREATE TABLE chunks(id INTEGER PRIMARY KEY,issue_id INTEGER,title TEXT,field TEXT,start INTEGER,end INTEGER,text TEXT); CREATE VIRTUAL TABLE chunks_fts USING fts5(tokens);")
            for identifier in range(1,41):
                db.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?)',(identifier,identifier,'제목','note:1',0,100000,'x'*100000))
                db.execute('INSERT INTO chunks_fts(rowid,tokens) VALUES (?,?)',(identifier,'wmemory wleak' if identifier%2 else 'wmemory wleak wleak'))
            expected=[dict(row) for row in db.execute("SELECT c.*, bm25(chunks_fts) AS lexical_score FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid WHERE chunks_fts MATCH ? ORDER BY lexical_score,c.id",['"wmemory" OR "wleak"'])]
            statements=[];db.set_trace_callback(statements.append)
            cursor=lexical_chunks(db,'memory leak')
            first=next(cursor)
            self.assertEqual(first,expected[0])
            ranking=next(sql for sql in statements if 'MATCH' in sql)
            self.assertNotIn('c.*',ranking,'긴 본문 전체를 정렬 대상으로 전달하지 않아야 합니다')
            remaining=list(cursor)
            self.assertEqual([first,*remaining],expected)
            statements.clear()
            narrowed=list(lexical_chunks(db,'memory leak',issue_ids={1,2}))
            self.assertEqual(narrowed,[row for row in expected if row['issue_id'] in {1,2}])
            self.assertNotIn('JOIN chunks',next(sql for sql in statements if 'MATCH' in sql),
                             '범위를 좁혀도 순위 계산 중 긴 본문 테이블을 결합하지 않아야 합니다')

    def test_title_projection_reads_bodies_only_for_the_selected_candidates(self):
        from backend.sources import chroma_lexical
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'chroma.sqlite3';target=Path(directory)/'titles.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                db.execute('CREATE TABLE embedding_metadata(key TEXT,string_value TEXT)')
                for identifier in range(1,401):
                    db.execute('INSERT INTO embedding_metadata VALUES (?,?)',('chroma:document',
                        f'이슈 ID: #{identifier}\n[제목]\n메모리 누수\n\n본문\n'+'x'*100_000))
            build=getattr(chroma_lexical,'build_title_projection',None)
            if build:
                build(path,target)
            read_bytes=[]
            class Cursor(sqlite3.Cursor):
                def __next__(self):
                    row=super().__next__()
                    read_bytes.extend(len(v) for v in row if isinstance(v,str) and len(v)>10_000)
                    return row
                def fetchone(self):
                    row=super().fetchone()
                    if row:
                        read_bytes.extend(len(v) for v in row if isinstance(v,str) and len(v)>10_000)
                    return row
            class Connection(sqlite3.Connection):
                def execute(self,sql,parameters=()):
                    return self.cursor(factory=Cursor).execute(sql,parameters)
            connect=sqlite3.connect
            def tracked(*args,**kwargs):
                return connect(*args,**kwargs,factory=Connection)
            with patch.object(chroma_lexical,'TITLE_INDEX_PATH',target,create=True), \
                 patch.object(chroma_lexical.sqlite3,'connect',side_effect=tracked):
                documents,_=chroma_lexical.rank_lexical_documents(path,'메모리 누수',100)
            self.assertEqual([d['issue_id'] for d in documents],list(range(1,101)))
            self.assertLess(sum(read_bytes),15_000_000,'전체 400건의 본문 대신 선택된 100건만 읽어야 합니다')

    def test_wal_change_between_queries_invalidates_request_local_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'chroma.sqlite3'
            with closing(sqlite3.connect(path)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE embedding_metadata(key TEXT,string_value TEXT)')
                db.execute('INSERT INTO embedding_metadata VALUES (?,?)',('chroma:document','이슈 ID: #1\n[제목]\n가나다\n\n본문'))
                db.commit()
                with chroma.lexical_ranking_scope(['가나다']):
                    first,_=chroma.rank_lexical_documents(path,'가나다',10)
                    db.execute('UPDATE embedding_metadata SET string_value=?',('이슈 ID: #2\n[제목]\n가나다\n\n본문',))
                    db.commit()
                    second,meta=chroma.rank_lexical_documents(path,'가나다',10)
                self.assertEqual([d['issue_id'] for d in first],[1])
                self.assertEqual([d['issue_id'] for d in second],[2])
                self.assertFalse(meta['lexical_scan_reused'])

    def test_similar_report_keeps_both_queries_but_reuses_the_same_fallback_scan(self):
        from backend.reports import similar
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'chroma.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                db.execute('CREATE TABLE embedding_metadata(key TEXT,string_value TEXT)')
                for identifier in range(1,11):
                    db.execute('INSERT INTO embedding_metadata VALUES (?,?)',('chroma:document',
                               f'이슈 ID: #{identifier}\n[제목]\n리소스 증가 메모리 누수\n\n본문'))
            def raw(identifier):
                return {'id':identifier,'summary':'리소스 증가 메모리 누수','project':{'name':'테스트증권'},
                        'status':{'name':'closed'},'notes':[{'id':1,'text':'타이머 삭제 수정 완료'}]}
            with patch.object(chroma,'CHROMA_DB_PATH',path), \
                 patch.dict('os.environ',{'MANTIS_HYBRID_SEARCH_ENABLED':'false'}), \
                 patch.dict(sys.modules,{'chromadb':types.SimpleNamespace()}), \
                 patch.object(chroma.requests,'post',side_effect=requests.ConnectionError('offline')), \
                 patch.object(chroma,'fetch_mantis_issue_by_id',side_effect=raw), \
                 patch.object(similar,'fetch_mantis_issue_by_id',side_effect=raw):
                r=similar.build_similar_resolution_report('#9000과 비슷한 과거 이슈 원인과 조치 비교해줘')
            self.assertEqual({s['issue_id'] for s in r['sources']},{9000,1,2,3})
            searches=r['retrieval']['stages'][0]['meta']['searches']
            self.assertEqual(len(searches),2)
            self.assertTrue(searches[1]['lexical_scan_reused'])

    def test_large_bodies_do_not_accumulate_while_top_candidates_keep_tie_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'chroma.sqlite3'
            with closing(sqlite3.connect(path)) as db, db:
                db.execute('CREATE TABLE embedding_metadata(key TEXT,string_value TEXT)')
                for identifier in range(1, 401):
                    content = f'이슈 ID: #{identifier}\n[제목]\n메모리 누수\n\n[고객사 본문 요청 내용]\n' + 'x' * 100_000
                    db.execute('INSERT INTO embedding_metadata VALUES (?,?)', ('chroma:document',content))
            with patch.object(chroma,'CHROMA_DB_PATH',path), \
                 patch.dict(sys.modules,{'chromadb':types.SimpleNamespace()}), \
                 patch.object(chroma.requests,'post',side_effect=requests.ConnectionError('offline')):
                tracemalloc.start()
                try:
                    documents,_ = chroma._rank_legacy_documents_semantically('메모리 누수',1,{})
                    _,peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            self.assertEqual([d['issue_id'] for d in documents],list(range(1,101)))
            self.assertLess(peak,8_000_000,'상위 후보만 보존해야 하며 40MB 본문을 모두 적재하면 안 됩니다')

    def test_two_queries_keep_independent_scores_and_stable_ties_with_one_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'chroma.sqlite3'
            with closing(sqlite3.connect(path)) as db,db:
                db.execute('CREATE TABLE embedding_metadata(key TEXT,string_value TEXT)')
                for identifier,title in [(7,'가나'),(4,'가나다'),(9,'가나다'),(2,'다라마')]:
                    db.execute('INSERT INTO embedding_metadata VALUES (?,?)',
                               ('chroma:document',f'이슈 ID: #{identifier}\n[제목]\n{title}\n\n본문'))
            scope=getattr(chroma,'lexical_ranking_scope',lambda queries:nullcontext())
            with patch.object(chroma,'CHROMA_DB_PATH',path), \
                 patch.dict(sys.modules,{'chromadb':types.SimpleNamespace()}), \
                 patch.object(chroma.requests,'post',side_effect=requests.ConnectionError('offline')):
                for _ in range(2):
                    with scope(['가나다','다라마']):
                        a,first=chroma._rank_legacy_documents_semantically('가나다',1,{})
                        b,second=chroma._rank_legacy_documents_semantically('다라마',1,{})
                    self.assertEqual([d['issue_id'] for d in a],[4,9,7])
                    self.assertEqual([d['similarity_score'] for d in a],[1.0,1.0,0.666667])
                    self.assertEqual([d['issue_id'] for d in b],[2])
                    self.assertFalse(first.get('lexical_scan_reused',False))
                    self.assertTrue(second.get('lexical_scan_reused',False))

    def test_required_focus_legacy_search_does_not_fill_with_unrelated_cases(self):
        with patch.object(chroma, 'rank_lexical_documents', return_value=([
                {'issue_id': 1, 'content': '이슈 ID: #1\n일반 개선', 'distance': 0.1}], {})), \
             patch.object(chroma.requests, 'post', side_effect=requests.ConnectionError('offline')), \
             patch.dict(sys.modules, {'chromadb': types.SimpleNamespace()}):
            documents, _ = chroma._rank_legacy_documents_semantically('개선', 3,
                {'semantic_focus_terms': ['CPU'], 'require_semantic_focus': True})
        self.assertEqual(documents, [])
