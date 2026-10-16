import copy
import json
import unittest
from unittest.mock import Mock, patch

import requests

from backend.execution import RequestStopped


class ExplanationQueryTests(unittest.TestCase):
    def plan(self):
        return {'operation': 'explain', 'resolved_constraints': {
            'customer_key': '가상증권', 'statuses': ['open'], 'issue_ids': [],
            'filter_clauses': [{'field': 'status', 'operator': 'in', 'value': ['open']}]}}

    def generate(self, content, plan=None, query='가상증권 7312 화면이 멈춘 원인과 조치 설명해줘'):
        from backend.services.explanation_queries import prepare_explanation_queries
        response = requests.Response()
        response.status_code = 200
        response._content = json.dumps({'choices': [{'message': {'content': content}}]}).encode()
        with patch('backend.services.explanation_queries.post_model_chat', return_value=response):
            return prepare_explanation_queries(query, plan or self.plan(),
                                               {'model': 'test'}, 'key', {'mantis': True, 'vectorDb': True})

    def test_auxiliary_queries_cannot_change_the_resolved_filter_plan(self):
        plan = self.plan()
        before = copy.deepcopy(plan)
        result = self.generate(json.dumps({'queries': ['7312 응답 지연 원인', '7312 멈춤 수정'],
                                          'resolved_constraints': {'statuses': []}}), plan)
        self.assertEqual(result['explanation_queries'], ['7312 응답 지연 원인', '7312 멈춤 수정'])
        self.assertEqual(result['resolved_constraints'], before['resolved_constraints'])
        self.assertEqual(plan, before)

    def test_rejects_invented_numbers_duplicate_and_unbounded_queries(self):
        result = self.generate(json.dumps({'queries': ['9999 오류', '7312 응답 지연',
                                                      '7312 응답 지연', '7312 멈춤 수정', '세 번째 검색']}))
        self.assertEqual(result['explanation_queries'], ['7312 응답 지연', '7312 멈춤 수정'])
        for payload in ['not json', '{"queries":"all"}', '{"queries":[null, 7, "x", "' + 'a' * 300 + '"]}']:
            self.assertEqual(self.generate(payload)['explanation_queries'], [])

    def test_counts_direct_ids_and_disabled_tools_do_not_call_query_model(self):
        from backend.services.explanation_queries import prepare_explanation_queries
        variants = []
        for operation in ('count', 'list', 'aggregate'):
            variants.append(({**self.plan(), 'operation': operation}, {'mantis': True, 'vectorDb': True}))
        ids = self.plan()
        ids['resolved_constraints']['issue_ids'] = [12345]
        variants.extend([(ids, {'mantis': True, 'vectorDb': True}),
                         (self.plan(), {'mantis': False, 'vectorDb': True})])
        with patch('backend.services.explanation_queries.post_model_chat', side_effect=AssertionError('unexpected model')):
            for plan, tools in variants:
                self.assertEqual(prepare_explanation_queries('원인 설명', plan, {}, '', tools), plan)

    def test_query_model_timeout_falls_back_but_cancellation_propagates(self):
        from backend.services.explanation_queries import prepare_explanation_queries
        args = ('멈춤 원인', self.plan(), {'model': 'test'}, 'key', {'mantis': True, 'vectorDb': True})
        with patch('backend.services.explanation_queries.post_model_chat', side_effect=requests.Timeout):
            result = prepare_explanation_queries(*args)
        self.assertEqual(result['explanation_queries'], [])
        self.assertEqual(result['explanation_query_generation']['status'], 'fallback')
        with patch('backend.services.explanation_queries.post_model_chat', side_effect=RequestStopped()):
            with self.assertRaises(RequestStopped):
                prepare_explanation_queries(*args)


class ExplanationFusionTests(unittest.TestCase):
    def stage(self, ids):
        return {'source': 'chroma_db', 'status': 'ok', 'meta': {'search_complete': False},
                'documents': [{'issue_id': identifier, 'latest_issue': {'id': identifier},
                               'content': f'issue {identifier}'} for identifier in ids]}

    def test_combines_new_candidate_without_losing_original_leaders_or_filters(self):
        from backend.sources.mantis_multiquery import search_explanation_queries
        constraints = {'customer_key': '가상', 'answer_query': '원래 질문', 'statuses': ['open']}
        seen = []

        def search(query, limit, scoped, *, semantic):
            seen.append(copy.deepcopy(scoped))
            self.assertTrue(semantic)
            return self.stage({'원래 질문': [1, 2, 3, 4, 5], '의역': [8, 3, 4], '원인': [8, 7, 6]}[query])

        result = search_explanation_queries('원래 질문', ['의역', '원인'], 5, constraints, search=search)
        ids = [d['issue_id'] for d in result['documents']]
        self.assertIn(8, ids)
        self.assertTrue({1, 2}.issubset(ids))
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(ids), 5)
        self.assertEqual(seen, [constraints] * 3)
        self.assertFalse(result['meta']['search_complete'])

    def test_reranks_union_against_original_question_before_candidate_cutoff(self):
        from backend.sources.mantis_multiquery import search_explanation_queries

        def stage(ids):
            result = self.stage(ids)
            for document in result['documents']:
                document['chunk_evidence'] = {'title': '증상 조사', 'field': 'note:1',
                                              'text': '고유 증상 원인' if document['issue_id'] == 99 else '일반 안내'}
            return result

        def embed(texts):
            return [[1.0, 0.0] if i == 0 or '고유 증상 원인' in text else [0.0, 1.0]
                    for i, text in enumerate(texts)]

        with patch('backend.sources.mantis_hybrid.embed_texts', side_effect=embed):
            result = search_explanation_queries('원래 질문', ['부분 질문', '다른 표현'], 5, {}, search=Mock(
                side_effect=[stage([1, 2, 3, 4, 5]), stage([3, 4, 5, 6, 7]), stage([3, 4, 5, 99, 8])]))
        ids = [document['issue_id'] for document in result['documents']]
        self.assertIn(99, ids)
        self.assertTrue({1, 2}.issubset(ids))
        self.assertEqual(len(ids), 5)

    def test_union_reranking_failure_preserves_rrf_and_cancel_propagates(self):
        from backend.sources.mantis_multiquery import search_explanation_queries
        def stage():
            result = self.stage([1, 2])
            for doc in result['documents']:
                doc['chunk_evidence'] = {'title': '증상', 'field': 'description', 'text': '원문'}
            return result
        with patch('backend.sources.mantis_hybrid.embed_texts', side_effect=requests.Timeout):
            result = search_explanation_queries('질문', ['보조'], 5, {}, search=lambda *a, **k: stage())
        self.assertEqual([doc['issue_id'] for doc in result['documents']], [1, 2])
        with patch('backend.sources.mantis_hybrid.embed_texts', side_effect=RequestStopped()):
            with self.assertRaises(RequestStopped):
                search_explanation_queries('질문', ['보조'], 5, {}, search=lambda *a, **k: stage())

    def test_auxiliary_failure_keeps_original_and_never_retries_forever(self):
        from backend.sources.mantis_multiquery import search_explanation_queries
        search = Mock(side_effect=[self.stage([1, 2]), requests.Timeout(), self.stage([3])])
        result = search_explanation_queries('원래 질문', ['첫 검색', '둘째 검색', '초과 검색'], 5, {}, search=search)
        self.assertEqual({d['issue_id'] for d in result['documents']}, {1, 2, 3})
        self.assertEqual(search.call_count, 3)
        self.assertEqual(result['meta']['multiquery']['failed_queries'], 1)

    def test_disabled_or_empty_auxiliaries_preserve_original_stage(self):
        from backend.sources.mantis_multiquery import search_explanation_queries
        original = self.stage([1, 2])
        for queries, enabled in [([], 'true'), (['다른 질문'], 'false')]:
            with patch.dict('os.environ', {'MANTIS_MULTIQUERY_ENABLED': enabled}):
                result = search_explanation_queries('원래 질문', queries, 5, {}, search=lambda *a, **k: original)
            self.assertEqual(result, original)

    def test_auxiliary_cancellation_propagates(self):
        from backend.sources.mantis_multiquery import search_explanation_queries
        search = Mock(side_effect=[self.stage([1]), RequestStopped()])
        with self.assertRaises(RequestStopped):
            search_explanation_queries('원래 질문', ['보조 질문'], 5, {}, search=search)

    def test_confirmed_exclusion_cannot_be_restored_by_auxiliary_snapshot(self):
        from backend.sources.mantis_multiquery import search_explanation_queries
        denied = self.stage([])
        denied['meta'].update(scanned_issue_ids=[9], matched_issue_ids=[])
        snapshot = self.stage([9])
        snapshot['documents'][0]['latest_source'] = 'mantis_snapshot'
        snapshot['meta'].update(scanned_issue_ids=[], matched_issue_ids=[9], mantis_refresh_failures=1)
        for stages in ([denied, snapshot], [snapshot, denied]):
            with self.subTest(order=[s['documents'] for s in stages]):
                result = search_explanation_queries('원래 질문', ['보조 질문'], 5, {}, search=Mock(side_effect=stages))
                self.assertEqual(result['documents'], [])
                self.assertEqual(result['meta']['matched_issue_ids'], [])
                self.assertEqual(result['meta']['mantis_refresh_failures'], 1)

    def test_a_subsequent_live_match_can_restore_a_previously_excluded_candidate(self):
        from backend.sources.mantis_multiquery import search_explanation_queries
        denied = self.stage([])
        denied['meta'].update(scanned_issue_ids=[9], matched_issue_ids=[])
        live = self.stage([9])
        live['documents'][0]['latest_source'] = 'mantis'
        live['meta'].update(scanned_issue_ids=[9], matched_issue_ids=[9])
        result = search_explanation_queries('원래 질문', ['보조 질문'], 5, {}, search=Mock(side_effect=[denied, live]))
        self.assertEqual(result['documents'][0]['issue_id'], 9)
        self.assertEqual(result['documents'][0]['latest_source'], 'mantis')

    def test_explanation_pipeline_consumes_auxiliaries_with_original_constraints(self):
        from backend.retrieval import run_retrieval_pipeline
        constraints = {'customer_key': None, 'issue_ids': [], 'statuses': ['open'],
                       'filter_clauses': [{'field': 'status', 'operator': 'in', 'value': ['open']}]}
        plan = {'operation': 'explain', 'resolved_constraints': constraints,
                'explanation_queries': ['응답 지연']}
        with patch('backend.retrieval.execute_chroma_search',
                   side_effect=[self.stage([1, 2]), self.stage([3])]) as search:
            result = run_retrieval_pipeline('화면이 멈춘 원인을 설명해줘', 5,
                                            {'mantis': True, 'vectorDb': True}, plan)
        self.assertEqual({d['issue_id'] for d in result['stages'][0]['documents']}, {1, 2, 3})
        self.assertEqual(result['constraints'], constraints)
        for call in search.call_args_list:
            self.assertEqual(call.args[2]['statuses'], ['open'])
            self.assertEqual(call.args[2]['answer_query'], '화면이 멈춘 원인을 설명해줘')


if __name__ == '__main__':
    unittest.main()
