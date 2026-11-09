"""의미 검색은 본문 벡터로 후보만 찾고 연결·모델·소유 문서 경계를 지킨다."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from test_notion_body_index import page, segment


def document(identifier, body, database='db'):
    return {'page_id': identifier, 'database_id': database, 'raw_page': page(identifier, database=database, title='기술 문서'),
            'segments': [segment(body, owner=identifier)], 'indexed_edited_time': '2026-11-01T00:00:00Z'}


class NotionSemanticIndexTests(unittest.TestCase):
    def setUp(self):
        from backend.notion.semantic_index import SemanticIndex
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'vectors.sqlite3'
        self.index=SemanticIndex(self.path,'connection-a','model-a')

    def test_cosine_can_find_a_body_without_the_query_words_and_respects_database_scope(self):
        docs=[document('guide','종목 추가 버튼을 누르면 그래프를 겹쳐 볼 수 있습니다.'),
              document('other','점심 회의에서 비용 정산을 논의합니다.'),
              document('private','접근 범위 밖의 차트 사용 절차',database='secret')]
        self.index.sync(docs,embed=lambda texts:[[1.,0.],[0.,1.],[1.,0.]],max_chunks=10)
        found=self.index.search([1.,0.],['db'],limit=1)
        self.assertEqual([r['page_id'] for r in found],['guide'])
        self.assertGreater(found[0]['score'],.99)
        self.assertNotIn('비교차트',found[0]['text'])

    def test_unchanged_chunks_reuse_vectors_and_changed_or_deleted_pages_are_reconciled(self):
        calls=[]
        def embed(texts):calls.extend(texts);return [[1.,0.] for _ in texts]
        docs=[document('first','처음 본문'),document('second','다른 본문')]
        self.index.sync(docs,embed=embed,max_chunks=10)
        self.index.sync(docs,embed=embed,max_chunks=10)
        self.assertEqual(len(calls),2)
        docs[0]['segments'][0]['text']='수정한 본문'
        self.index.sync(docs[:1],embed=embed,max_chunks=10)
        self.assertEqual(len(calls),3)
        self.assertEqual([r['page_id'] for r in self.index.search([1.,0.],['db'])],['first'])
        self.assertIn('수정한 본문',self.index.search([1.,0.],['db'])[0]['text'])

    def test_other_connections_models_and_invalid_vectors_cannot_reuse_the_index(self):
        from backend.notion.semantic_index import SemanticIndex
        self.index.sync([document('first','현재 본문')],embed=lambda texts:[[1.,0.]],max_chunks=10)
        for connection,model in [('connection-b','model-a'),('connection-a','model-b')]:
            other=SemanticIndex(self.path,connection,model)
            self.assertEqual(other.search([1.,0.],['db']),[])
        for values in [[[float('nan'),0.]],[[0.,0.]],[[1.,0.],[0.,1.]]]:
            with self.subTest(values=values),self.assertRaises(ValueError):
                self.index.sync([document('first','바뀐 본문')],embed=lambda texts, values=values:values,max_chunks=10)
        self.assertIn('현재 본문',self.index.search([1.,0.],['db'])[0]['text'])
        with self.assertRaises(ValueError):self.index.search([1.,0.,0.],['db'])

    def test_children_are_not_indexed_as_the_parents_body_and_long_blocks_are_chunked(self):
        from backend.notion.semantic_index import chunks
        doc=document('parent','가'*2700)
        doc['segments'].append(segment('다른 페이지의 비공개 비밀',identifier='child-block',owner='child'))
        parts=chunks(doc)
        self.assertGreater(len(parts),2)
        self.assertTrue(all(len(p['text'])<=1400 for p in parts))
        self.assertNotIn('비공개',''.join(p['text'] for p in parts))
        self.assertEqual(chunks(document('empty','')),[])

    def test_batch_limit_is_resumable_and_a_failed_batch_is_not_published(self):
        docs=[document(str(i),f'본문 {i}') for i in range(3)]
        result=self.index.sync(docs,embed=lambda texts:[[1.,0.] for _ in texts],max_chunks=1)
        self.assertEqual(result['pending_chunks'],2)
        result=self.index.sync(docs,embed=lambda texts:[[1.,0.] for _ in texts],max_chunks=10)
        self.assertEqual(result['pending_chunks'],0)
        changed=deepcopy(docs);changed[0]['segments'][0]['text']='수정'
        with self.assertRaises(ValueError):self.index.sync(changed,embed=lambda texts:[],max_chunks=10)
        self.assertEqual(len(self.index.search([1.,0.],['db'])),3)

    def test_embedding_stays_local_and_masks_credentials_before_sending(self):
        from backend.notion import semantic_index
        parts=semantic_index.chunks(document('p','문'*994+'\nPass: fake-credential\n'+'문'*1300))
        self.assertNotIn('fake-credential',str(parts))
        with patch.dict('os.environ',{'OLLAMA_BASE_URL':'https://external.example'}):
            with self.assertRaises(ValueError):semantic_index.embedding_config()
        response=MagicMock();response.is_redirect=False;response.json.return_value={'embeddings':[[1.,0.]]}
        response.__enter__.return_value=response
        with (patch.dict('os.environ',{'OLLAMA_BASE_URL':'http://127.0.0.1:11434'}),
              patch.object(semantic_index.requests,'post',return_value=response) as send):
            semantic_index.embed_local(['Pass: fake-credential'])
            self.assertNotIn('fake-credential',str(send.call_args))
            self.assertIs(send.call_args.kwargs['allow_redirects'],False)
            response.is_redirect=True
            with self.assertRaises(ValueError):semantic_index.embed_local(['본문'])

    def test_customer_scope_ranks_candidates_without_discarding_stale_metadata(self):
        from backend.notion.semantic_index import prefer_scope_candidates
        from backend.notion.body_index import BodyIndex
        body=BodyIndex(Path(self.tmp.name)/'body.sqlite3',fingerprint='connection-a')
        for identifier,title in [('first','다른 고객사'),('second','한투 사용 방법')]:
            body.upsert_page('db',page(identifier,database='db',title=title))
        candidates=[{'page_id':'first','score':.9},{'page_id':'second','score':.8}]
        found=prefer_scope_candidates(body,candidates,{'filter_clauses':[{'field':'customer','operator':'equals','value':'한투'}]})
        self.assertEqual([r['page_id'] for r in found],['second','first'])


if __name__=='__main__':unittest.main()
