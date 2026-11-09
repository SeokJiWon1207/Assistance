"""페이지 탐색은 링크가 가리키는 대상의 권한·소속·조건을 따로 확인한다."""
from copy import deepcopy
import time
import unittest
from unittest.mock import patch

from test_notion_body_index import page, segment

A='a'*32
B='b'*32
C='c'*32
D='d'*32


class NotionPageNavigationTests(unittest.TestCase):
    def test_structural_child_mention_relation_and_notion_url_links_are_extracted_only(self):
        from backend.notion.page_links import page_links
        objects=[{'id':B,'type':'child_page','child_page':{'title':'하위 설명'}},
                 {'id':'block','type':'paragraph','paragraph':{'rich_text':[
                     {'type':'mention','mention':{'type':'page','page':{'id':C}}},
                     {'type':'text','text':{'content':'가이드','link':{'url':f'https://www.notion.so/guide-{D}?x=1'}}}]}},
                 {'properties':{'관계':{'type':'relation','relation':[{'id':B}]}}},
                 {'id':'b','type':'link_to_page','link_to_page':{'type':'page_id','page_id':C}},
                 {'type':'bookmark','bookmark':{'url':f'https://evil.test/{A}'}}]
        links=[link for obj in objects for link in page_links(obj)]
        self.assertEqual({link['page_id'] for link in links},{B,C,D})
        self.assertEqual({link['kind'] for link in links},{'child','mention','link','relation'})
        self.assertEqual(page_links({'parent':{'type':'page_id','page_id':A}}),[])
        self.assertEqual(page_links({'type':'bookmark','bookmark':{'url':'https://[invalid'}}),[])

    def test_navigation_denies_moved_deleted_and_wrong_customer_targets(self):
        from backend.notion import page_links as navigation
        valid=page(B,database='db',title='한투 설정 방법')
        wrong=page(C,database='db',title='NH 설정 방법')
        outside=page(D,database='private',title='한투 비공개')
        source={'id':A,'_verified_page_links':[{'page_id':key,'kind':'relation'} for key in [B,C,D,A]]}
        objects={B:valid,C:wrong,D:outside}
        def request(path,budget,**kw):return deepcopy(objects[path.split('/')[-1]])
        with patch.object(navigation.crawl_transport,'request_json',side_effect=request):
            found,meta=navigation.follow_page_links(source,{'db':'db'},
                {'filter_clauses':[{'field':'customer','operator':'equals','value':'한투'}]},deadline=time.monotonic()+20)
        self.assertEqual([p['id'] for p in found],[B])
        self.assertEqual(found[0]['navigation']['from_page_id'],A)
        self.assertFalse(meta['search_complete'])
        objects[B]['archived']=True
        with patch.object(navigation.crawl_transport,'request_json',side_effect=request):
            found,_=navigation.follow_page_links(source,{'db':'db'},{},deadline=time.monotonic()+20)
        self.assertEqual([p['id'] for p in found],[C])

    def test_current_body_preserves_its_owner_and_never_uses_a_stale_vector_excerpt(self):
        from backend.notion import semantic_search
        current={'raw_page':page(A,database='db',title='한투 사용 절차'),'database_id':'db',
                 'segments':[segment('현재 절차는 추가 버튼을 선택합니다.',owner=A),segment('외부 비밀',owner=B)],
                 'accessible_content_complete':True,'incomplete_reasons':[],
                 'page_links':[{'page_id':B,'kind':'child'}]}
        record={'page_id':A,'database_id':'db','text':'만료된 문구','score':.9}
        with patch.object(semantic_search,'validate_index_candidate',return_value=current):
            found=semantic_search.read_current_page(record,{'db':'db'},{},deadline=time.monotonic()+20)
        self.assertEqual(found['content'],'현재 절차는 추가 버튼을 선택합니다.')
        self.assertNotIn('외부 비밀',str(found));self.assertNotIn('만료된 문구',str(found))
        self.assertEqual(found['_verified_page_links'],current['page_links'])
        with patch.object(semantic_search,'validate_index_candidate',return_value=None):
            self.assertIsNone(semantic_search.read_current_page(record,{'db':'db'},{},deadline=time.monotonic()+20))

    def test_empty_semantic_top_k_or_missing_index_never_proves_global_absence(self):
        from backend.notion import semantic_search
        from backend.tools.ledger import proves_empty
        for candidates in (None,[]):
            with (patch.object(semantic_search,'resolve_notion_settings',return_value={'database_ids':['db'],'token':'t'}),
                  patch.object(semantic_search,'resolve_notion_read_scope',return_value=({'db':'db'},True)),
                  patch.object(semantic_search,'semantic_candidates',return_value=candidates)):
                result=semantic_search.search_notion_body('뜻으로 검색',2,{})
            self.assertFalse(proves_empty(result));self.assertFalse(result['meta']['search_complete'])
            self.assertEqual(result['notion_pages'],[])

    def test_disabled_body_reading_does_not_contact_the_source(self):
        from backend.notion import semantic_search
        with (patch.dict('os.environ',{'NOTION_FETCH_PAGE_BLOCKS':'false'}),
              patch.object(semantic_search,'resolve_notion_settings') as settings,
              patch.object(semantic_search,'validate_index_candidate') as validate):
            result=semantic_search.search_notion_body('접속 절차',2,{})
            self.assertIn('body_disabled',result['meta']['incomplete_reasons'])
            self.assertIsNone(semantic_search.read_current_page({'page_id':A},{},{},deadline=time.monotonic()+20))
        settings.assert_not_called();validate.assert_not_called()

    def test_missing_completion_metadata_is_incomplete_and_verified_chunk_is_located_in_current_body(self):
        from backend.notion import semantic_search
        passage='종목을 추가하려면 왼쪽 목록을 선택합니다.'
        current={'raw_page':page(A,database='db',title='한투 사용 절차'),'database_id':'db',
                 'segments':[segment('안내\n'+passage,owner=A)]}
        record={'page_id':A,'database_id':'db','text':'이전 제목\n'+passage,'score':.9}
        with patch.object(semantic_search,'validate_index_candidate',return_value=current):
            found=semantic_search.read_current_page(record,{'db':'db'},{},deadline=time.monotonic()+20)
        self.assertTrue(found['content_incomplete'])
        self.assertEqual(found['semantic_excerpt'],passage)
        self.assertNotIn('이전 제목',str(found))
        current.update(accessible_content_complete=False,incomplete_reasons=['unsupported_child_pages'])
        with patch.object(semantic_search,'validate_index_candidate',return_value=current):
            found=semantic_search.read_current_page(record,{'db':'db'},{},deadline=time.monotonic()+20)
        self.assertFalse(found['content_incomplete'])

    def test_database_filter_is_checked_against_the_verified_database_label(self):
        from backend.notion import semantic_search, read_api
        current={'raw_page':page(A,database='db',title='접속 방법'),'database_id':'db',
                 'segments':[segment('VPN을 연결합니다.',owner=A)],'accessible_content_complete':True}
        filters={'filter_clauses':[{'field':'database','operator':'equals','value':'기술가이드'}]}
        with (patch.object(semantic_search,'validate_index_candidate',return_value=current),
              patch.object(read_api,'fetch_notion_database_title',return_value='기술가이드')):
            found=semantic_search.read_current_page({'page_id':A},{'db':'db'},filters,deadline=time.monotonic()+20)
        self.assertIsNotNone(found)
        self.assertEqual(found['database_label'],'기술가이드')


if __name__=='__main__':unittest.main()
