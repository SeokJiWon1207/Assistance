"""마지막 판정이 있는 단일 이슈에 별도 후속 수정 이력이 섞이지 않도록 한다."""
import json
import unittest

from backend.schemas import ChatRequest
from backend.services.contracts import ChatPipelineContext
from backend.services.grounded_explanation import generate_grounded_explanation, required_topics
from backend.sources.mantis import normalize_issue


class GroundedExplanationScopeTests(unittest.TestCase):
    def run_selection(self, *, explicit=False, comparison=False, first_single=False,
                      complete=True, final='잔존일수가 1일일 때 표시하지 않는 현상으로 정상동작임',
                      delivery=True, references=False):
        query = '어떤 조건 때문이었고 마지막에는 어떻게 판단했어? 중간 QC·전달 이력과 최종 결론을 나눠줘'
        if comparison:
            query = '#81과 #82의 조건과 조치, 마지막 QC·전달 이력을 비교해줘'
        request = ChatRequest(modelId='test', messages=[{'role': 'user', 'content': query}])
        notes = [
            {'id': 1, 'created_at': '2025-11-27', 'text': '[QC 완료]'},
            {'id': 2, 'created_at': '2025-11-28', 'text': '[전달 완료]' if delivery else '[전달 예정]'},
            {'id': 3, 'created_at': '2026-02-23', 'text': final},
        ]
        rows = [normalize_issue({'id': 81, 'summary': '위클리옵션 증거금 미표시',
                                 'project': {'name': '하나'}, 'notes': notes}, evidence_query=query),
                normalize_issue({'id': 82, 'summary': '다른 만기 종목의 증거금 조회',
                                 'project': {'name': '하나'}, 'notes': [
                                     {'id': 4, 'created_at': '2026-05-08',
                                      'text': '새로운 FID 제공받아 별도 수정. QC 완료 후 전달 완료.'}]},
                                evidence_query=query)]
        rows[0]['related_issue_ids'] = [82]
        rows[1]['related_issue_ids'] = [81]
        rows[0]['answer_evidence']['complete'] = complete
        if references:
            rows[1]['source_reference_ids'] = [81]
        constraints = {'issue_ids': [81, 82]} if comparison else {'issue_ids': [81]} if explicit else {}
        context = ChatPipelineContext(query, {}, '', [], query, {'mantis': True}, [], constraints, 5,
                                      intent_plan={'operation': 'explain'}, retrieval_result={
                                          'stages': [{'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': rows}]})
        shown_ids = []
        remembered = {}

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            shown_ids.append([issue['id'] for issue in shown['issues']])
            for issue in shown['issues']:
                remembered[issue['id']] = [e['passage_id'] for e in issue['answer_evidence']['excerpts']]
            # 두 번째 모델이 첫 전체 카탈로그의 관련 이슈를 다시 선택하는 경우도 막아야 한다.
            selected = [81] if first_single and len(shown_ids) == 1 else [81, 82]
            payload = {'selected_issue_ids': selected, 'sections': [
                {'topic': topic, 'evidence_ids': [ref for issue_id in selected for ref in remembered[issue_id]],
                 'unanswered': False} for topic in required_topics(query)]}
            return {'content': json.dumps(payload), 'raw': {},
                    'sources': [{'issue_id': 81}, {'issue_id': 82}]}

        return generate_grounded_explanation(request, context, generate), shown_ids

    def test_complete_primary_final_judgment_excludes_related_later_fix_in_both_passes(self):
        for explicit in [False, True]:
            for first_single in [False, True]:
                with self.subTest(explicit=explicit, first_single=first_single):
                    result, shown_ids = self.run_selection(explicit=explicit, first_single=first_single)
                    self.assertTrue(result['groundingReview']['completed'])
                    self.assertEqual(result['groundingReview']['selectedIssueIds'], [81])
                    self.assertEqual(shown_ids[-1], [81])
                    self.assertIn('정상동작임', result['content'])
                    self.assertIn('2025-11-28', result['content'])
                    self.assertNotIn('새로운 FID', result['content'])
                    self.assertNotIn('#82', result['content'])
                    self.assertEqual(result['sources'], [{'issue_id': 81}])

    def test_missing_primary_final_evidence_keeps_related_completion_available(self):
        for options in [{'complete': False}, {'delivery': False}, {'final': '정상동작 여부는 추가 확인 필요'},
                        {'final': '정상 동작으로 확인되지 않았음'}]:
            with self.subTest(options=options):
                result, _ = self.run_selection(**options)
                self.assertTrue(result['groundingReview']['completed'])
                self.assertEqual(result['groundingReview']['selectedIssueIds'], [81, 82])

    def test_verified_source_reference_keeps_legitimate_connected_history(self):
        result, _ = self.run_selection(references=True)
        self.assertTrue(result['groundingReview']['completed'])
        self.assertEqual(result['groundingReview']['selectedIssueIds'], [81, 82])

    def test_explicit_comparison_keeps_both_issues_even_with_complete_primary(self):
        result, _ = self.run_selection(comparison=True)
        self.assertTrue(result['groundingReview']['completed'])
        self.assertEqual(result['groundingReview']['selectedIssueIds'], [81, 82])
