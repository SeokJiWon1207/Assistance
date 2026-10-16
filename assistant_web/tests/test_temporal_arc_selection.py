"""초기 주장과 후속 검증을 묻는 질문이 초기 안내만 가진 별도 이슈로 축소되지 않는다."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from backend.schemas import ChatRequest
from backend.services.contracts import ChatPipelineContext
from backend.services.grounded_explanation import generate_grounded_explanation


class TemporalArcSelectionTests(unittest.TestCase):
    def grounding(self):
        # 완전한 이력이 있는 후보의 이슈 번호와 연도가 더 작아도 그 이력을 선택한다.
        return {'issues': [
            {'id': 91, 'project': '같은 고객', 'title': '화면 오류 초기 문의',
             'related_issue_ids': [], 'answer_evidence': {'complete': True, 'excerpts': [
                 {'field': 'note:100', 'note_id': 100, 'created_at': '2024-01-01',
                  'text': '메모리 재사용 때문에 발생한 것으로 안내했습니다.'},
                 {'field': 'note:101', 'note_id': 101, 'created_at': '2024-01-02',
                  'text': '고객 내부 확인이 필요하여 보류했습니다.'}]}},
            {'id': 27, 'project': '같은 고객', 'title': '화면 오류 재확인 문의',
             'related_issue_ids': [], 'answer_evidence': {'complete': True, 'excerpts': [
                 {'field': 'note:200', 'note_id': 200, 'created_at': '2023-01-01',
                  'text': '처음에는 메모리 재사용 때문이라고 답변했습니다.'},
                 {'field': 'note:201', 'note_id': 201, 'created_at': '2023-01-02',
                  'text': '검증 결과 외부에서 받은 데이터임을 확인했습니다.'}]}}
        ]}

    def run_selection(self, *, query=None, constraints=None, grounding=None, first_ids=None):
        query = query or '화면 오류는 어떻게 결론났어? 처음 메모리 재사용 때문이라는 답변 이후 확인한 내용까지 정리해줘.'
        context = ChatPipelineContext(query, {}, '', [], query, {'mantis': True}, [],
                                      constraints or {}, 5, retrieval_result={'stages': []})
        request = ChatRequest(modelId='fixture', messages=[{'role': 'user', 'content': query}])
        seen = []

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            shown_ids = [i['id'] for i in shown['issues']]
            seen.append({'ids': shown_ids, 'messages': deepcopy(messages)})
            if len(seen) == 1:
                selected = first_ids or [91, 27]
            elif shown_ids == [27]:
                selected = [27]
            elif any('초기' in m['content'] and '후속' in m['content']
                     for m in messages[2:] if m['role'] == 'user'):
                selected = [27]
            else:
                selected = first_ids or ([91, 27] if len(shown_ids) > 1 else shown_ids)
            refs = [e['passage_id'] for i in shown['issues'] if i['id'] in selected
                    for e in i['answer_evidence']['excerpts']]
            return {'content': json.dumps({'selected_issue_ids': selected, 'sections': [
                {'topic': 'timeline', 'evidence_ids': refs, 'unanswered': False}]}),
                    'raw': {}, 'sources': [{'issue_id': 91}, {'issue_id': 27}]}

        with patch('backend.services.grounded_explanation.build_model_retrieval_context',
                   return_value=grounding or self.grounding()):
            result = generate_grounded_explanation(request, context, generate)
        return result, seen

    def test_unlinked_initial_only_candidate_does_not_replace_complete_temporal_arc(self):
        result, seen = self.run_selection()
        self.assertTrue(result['groundingReview']['completed'])
        self.assertEqual(result['groundingReview']['selectedIssueIds'], [27])
        self.assertEqual(seen[1]['ids'], [27])
        self.assertIn('검증 결과 외부에서 받은 데이터임을 확인했습니다.', result['content'])
        self.assertNotIn('고객 내부 확인이 필요하여 보류했습니다.', result['content'])

    def test_single_initial_only_selection_is_retried_with_temporal_requirement(self):
        result, _seen = self.run_selection(first_ids=[91])
        self.assertTrue(result['groundingReview']['completed'])
        self.assertEqual(result['groundingReview']['selectedIssueIds'], [27])

    def test_verified_related_cases_keep_their_legitimate_combined_history(self):
        grounding = self.grounding()
        grounding['issues'][0]['related_issue_ids'] = [27]
        result, _seen = self.run_selection(grounding=grounding)
        self.assertEqual(result['groundingReview']['selectedIssueIds'], [91, 27])

    def test_explicit_target_and_comparison_are_not_retargeted(self):
        for constraints, first_ids in [({'issue_ids': [91]}, [91]), ({'issue_ids': [91, 27]}, [91, 27])]:
            with self.subTest(constraints=constraints):
                result, _seen = self.run_selection(constraints=constraints, first_ids=first_ids)
                self.assertEqual(result['groundingReview']['selectedIssueIds'], first_ids)

    def test_explicit_comparison_language_keeps_both_candidates(self):
        query = '두 이슈를 비교해줘. 처음 메모리 재사용 때문이라는 답변 이후 확인한 내용까지 정리해줘.'
        result, _seen = self.run_selection(query=query)
        self.assertEqual(result['groundingReview']['selectedIssueIds'], [91, 27])

    def test_malformed_model_selection_is_validated_without_crashing(self):
        result, _seen = self.run_selection(first_ids=[{'id': 91}])
        self.assertFalse(result['groundingReview']['completed'])

    def test_missing_initial_evidence_or_ambiguous_complete_arcs_do_not_force_switch(self):
        for mode in ['missing_initial', 'both_complete', 'incomplete_source']:
            grounding = self.grounding()
            if mode == 'missing_initial':
                grounding['issues'][1]['answer_evidence']['excerpts'][0]['text'] = '접수한 뒤 확인을 시작했습니다.'
            elif mode == 'both_complete':
                grounding['issues'][0]['answer_evidence']['excerpts'][1]['text'] = '검증 결과 정상임을 확인했습니다.'
            else:
                grounding['issues'][0]['answer_evidence']['complete'] = False
            with self.subTest(mode=mode):
                result, _seen = self.run_selection(grounding=grounding, first_ids=[91])
                self.assertEqual(result['groundingReview']['selectedIssueIds'], [91])


if __name__ == '__main__':
    unittest.main()
