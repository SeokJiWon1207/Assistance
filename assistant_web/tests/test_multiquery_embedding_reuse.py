"""다중 검색의 같은 문장 재계산을 줄이되 요청·모델·원문 경계를 지킨다."""
import json
import unittest
from unittest.mock import patch

import requests

from backend.sources.mantis_hybrid import embed_texts
from backend.sources.mantis_multiquery import search_explanation_queries


class MultiqueryEmbeddingReuseTests(unittest.TestCase):
    def run_search(self, texts, responder=None):
        sent = []
        received = []

        def post(url, *, json, timeout):
            sent.append((url, json['model'], list(json['input'])))
            response = requests.Response()
            response.status_code = 200
            vectors = (responder(json['input']) if responder else
                       [[float(len(text)), 1.0] for text in json['input']])
            response._content = __import__('json').dumps({'embeddings': vectors}).encode()
            return response

        def search(query, limit, constraints, *, semantic):
            received.append(embed_texts(texts[query]))
            return {'status': 'ok', 'documents': [{'issue_id': 1}], 'meta': {}}

        with patch('backend.sources.mantis_hybrid.requests.post', side_effect=post):
            result = search_explanation_queries('원문', ['보조'], 5, {}, search=search)
        return sent, received, result

    def test_exact_passage_reused_without_changing_vector_order_or_values(self):
        sent, received, _ = self.run_search({'원문': ['원문', '동일 근거'], '보조': ['보조', '동일 근거', '추가']})
        self.assertEqual([s[2] for s in sent], [['원문', '동일 근거'], ['보조', '추가']])
        self.assertEqual(received, [[[2.0, 1.0], [5.0, 1.0]], [[2.0, 1.0], [5.0, 1.0], [2.0, 1.0]]])

    def test_changed_text_is_embedded_and_next_request_does_not_reuse_previous_data(self):
        texts = {'원문': ['원문', '기존 근거'], '보조': ['보조', '변경 근거']}
        for _ in range(2):
            sent, received, _ = self.run_search(texts)
            self.assertEqual([s[2] for s in sent], list(texts.values()))
            self.assertEqual(len(received), 2)

    def test_invalid_batch_is_not_reused_by_the_next_query(self):
        calls = 0

        def vectors(texts):
            nonlocal calls
            calls += 1
            return [[float('nan'), 1.0] for _ in texts] if calls == 1 else [[1.0, 1.0] for _ in texts]

        sent = []

        def post(url, **kwargs):
            texts = kwargs['json']['input']
            sent.append(texts)
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({'embeddings': vectors(texts)}).encode()
            return response

        def search(query, *args, **kwargs):
            from backend.sources.mantis_hybrid import _cosines
            try:
                _cosines(embed_texts([query, '동일 근거']), 2)
            except ValueError:
                pass  # 실제 하이브리드 검색도 재순위화 오류 시 RRF 후보를 유지한다.
            return {'status': 'ok', 'documents': [{'issue_id': 1}], 'meta': {}}

        with patch('backend.sources.mantis_hybrid.requests.post', side_effect=post):
            search_explanation_queries('원문', ['보조'], 5, {}, search=search)
        self.assertEqual(sent, [['원문', '동일 근거'], ['보조', '동일 근거']])

    def test_model_change_cannot_reuse_a_different_embedding_space(self):
        sent = []

        def post(url, **kwargs):
            sent.append(kwargs['json'])
            response = requests.Response()
            response.status_code = 200
            response._content = json.dumps({'embeddings': [[1.0, 1.0] for _ in kwargs['json']['input']]}).encode()
            return response

        def search(query, *args, **kwargs):
            with patch.dict('os.environ', {'CHROMA_EMBEDDING_MODEL': query}):
                embed_texts(['공통 질문', '동일 근거'])
            return {'status': 'ok', 'documents': [{'issue_id': 1}], 'meta': {}}

        with patch('backend.sources.mantis_hybrid.requests.post', side_effect=post):
            search_explanation_queries('모델A', ['모델B'], 5, {}, search=search)
        self.assertEqual([s['model'] for s in sent], ['모델A', '모델B'])
        self.assertEqual([s['input'] for s in sent], [['공통 질문', '동일 근거']] * 2)
