"""중복 JSON 출력만 줄이고 독립 검토·근거 검증·노트 보강을 유지한다."""
import json
import unittest
from unittest.mock import patch

from backend.schemas import ChatRequest
from backend.services.contracts import ChatPipelineContext
from backend.services.grounded_explanation import generate_grounded_explanation
from backend.sources.mantis import normalize_issue


class ExplanationReviewLatencyTests(unittest.TestCase):
    def run_review(self, acknowledgement, *, partial=False):
        query = '조회 함수와 기본값을 알려줘'
        raw = {'id': 91, 'summary': '조회 안내', 'description': '조회 함수는 ReadValue입니다.',
               'notes': [{'id': 11, 'text': '기본값은 7입니다.', 'created_at': '2026-01-01'},
                         {'id': 12, 'text': '고객 검증은 아직 완료되지 않았습니다.', 'created_at': '2026-01-02'}]}
        row = normalize_issue(raw, evidence_query=query)
        if partial:
            row['answer_evidence']['complete'] = False
            row['answer_evidence']['excerpts'] = [e for e in row['answer_evidence']['excerpts'] if e['field'] != 'note:12']
        req = ChatRequest(modelId='test', messages=[{'role': 'user', 'content': query}])
        ctx = ChatPipelineContext(query, {}, '', [], query, {'mantis': True}, [], {'issue_ids': [91]}, 5,
                                  intent_plan={'operation': 'explain'}, retrieval_result={
                                      'stages': [{'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': [row]}]})
        calls = []
        first = None

        def generate(_request, _model, messages, *_args):
            nonlocal first
            calls.append(messages)
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            if len(calls) % 2:
                passages = shown['issues'][0]['answer_evidence']['excerpts']
                refs = [e['passage_id'] for e in passages if e['field'] in {'description', 'note:11'}]
                first = {'selected_issue_ids': [91], 'sections': [
                    {'topic': 'technical', 'evidence_ids': refs, 'unanswered': False}],
                    'answer_items': [{'question_part': '조회 함수와 기본값', 'evidence_ids': refs, 'unanswered': False}]}
                result = first
            else:
                result = first if acknowledgement == 'full' else acknowledgement
            return {'content': json.dumps(result), 'sources': [{'issue_id': 91}], 'raw': {}}

        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', return_value=raw):
            return generate_grounded_explanation(req, ctx, generate), calls

    def test_unchanged_review_keeps_exact_answer_and_question_evidence_with_two_calls(self):
        full, _ = self.run_review('full')
        compact, calls = self.run_review({'unchanged': True})
        self.assertTrue(compact['groundingReview']['completed'])
        self.assertEqual(len(calls), 2)
        self.assertEqual(compact['content'], full['content'])
        self.assertEqual(compact['sources'], full['sources'])
        self.assertIn('ReadValue', compact['content'])
        self.assertIn('**조회 함수와 기본값**', compact['content'])
        self.assertIn('"unchanged":true', calls[1][0]['content'])

    def test_unchanged_review_still_reads_supplement_and_preserves_later_pending_note(self):
        result, calls = self.run_review({'unchanged': True}, partial=True)
        self.assertTrue(result['groundingReview']['completed'])
        self.assertEqual(len(calls), 2)
        self.assertIn('고객 검증은 아직 완료되지 않았습니다.', calls[1][0]['content'])
        self.assertIn('고객 검증은 아직 완료되지 않았습니다.', result['content'])
        self.assertEqual(result['groundingReview']['noteSupplement']['supplemented_ids'], [91])

    def test_false_numeric_or_mixed_acknowledgement_cannot_bypass_validation(self):
        for value in [{'unchanged': False}, {'unchanged': 1}, {'unchanged': True, 'selected_issue_ids': [92]},
                      {'unchanged': True, 'sections': []}, {'approved': True}]:
            with self.subTest(value=value):
                result, calls = self.run_review(value)
                self.assertFalse(result['groundingReview']['completed'])
                self.assertEqual(len(calls), 4)
                self.assertEqual(result['sources'], [])
