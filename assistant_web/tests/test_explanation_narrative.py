"""보정 모델의 문장도 원문·범위·독립 의미 검토를 모두 통과해야 한다."""
import json
import unittest
from copy import deepcopy
from unittest.mock import Mock

import requests

from backend.execution import RequestStopped
from backend.schemas import ChatRequest
from backend.services.contracts import ChatPipelineContext
from backend.services.explanation_narrative import repair_summary
import test_explanation_summary as summary_fixture


def reply(value):
    return {'content': json.dumps(value, ensure_ascii=False), 'modelId': 'gemini', 'usedFallbackModel': 'gemini'}


class NarrativeRepairTests(unittest.TestCase):
    def setUp(self):
        self.validated, self.grounding = summary_fixture.ExplanationSummaryTests().data('100건을 수정했습니다.')
        self.question = '어떤 조치를 했어?'
        self.request = ChatRequest(modelId='luna', messages=[{'role': 'user', 'content': self.question}], stream=True)
        self.context = ChatPipelineContext(self.question, {'id': 'luna'}, '', [], self.question,
                                           {'mantis': True}, [], {'issue_ids': [91]}, 5)

    def run_repair(self, answer='캐시 덮어쓰기 로직을 수정했습니다.', verdict=None):
        generate = Mock(side_effect=[reply({'answers': [answer]}),
                                    reply({'supported': [True]} if verdict is None else verdict)])
        result = repair_summary(self.request, self.context, self.validated, self.grounding, generate)
        return *result, generate

    def test_success_keeps_original_evidence_and_reports_actual_author_model(self):
        original = deepcopy(self.validated)
        items, meta, generate = self.run_repair()
        self.assertEqual(meta['status'], 'accepted')
        self.assertEqual(meta['calls'], 2)
        self.assertEqual(meta['modelId'], 'gemini')
        self.assertEqual(items[0]['evidence'], original['answer_items'][0]['evidence'])
        self.assertEqual(self.validated, original)
        self.assertFalse(generate.call_args_list[0].args[0].stream)

    def test_invented_numbers_identifiers_and_other_issue_are_rejected_before_review(self):
        for answer in ('100건을 수정했습니다.', 'UpdateCacheNew를 수정했습니다.', '#92의 캐시를 수정했습니다.'):
            with self.subTest(answer=answer):
                items, meta, generate = self.run_repair(answer)
                self.assertEqual(items, [])
                self.assertEqual(meta['status'], 'rejected_by_checks')
                generate.assert_called_once()

    def test_later_qc_failure_blocks_completion_even_if_reviewer_would_approve(self):
        self.validated['answer_items'][0]['evidence'][0]['text'] = '[QC 완료]'
        self.grounding['issues'][0]['answer_evidence']['excerpts'].append(
            {'field': 'note:12', 'note_id': 12, 'created_at': '2026-06-11', 'text': 'QC NG로 재요청했습니다.'})
        items, meta, generate = self.run_repair('QC 완료가 확인됩니다.')
        self.assertEqual(items, [])
        self.assertEqual(meta['calls'], 1)

    def test_uncertain_cause_cannot_be_promoted_when_semantic_review_rejects(self):
        self.validated['answer_items'][0]['evidence'][0]['text'] = '캐시 충돌일 가능성을 검토 중입니다.'
        items, meta, generate = self.run_repair('원인은 캐시 충돌입니다.', {'supported': [False]})
        self.assertEqual(items, [])
        self.assertEqual(meta['status'], 'rejected_by_review')
        self.assertEqual(generate.call_count, 2)

    def test_review_sees_unselected_later_corrections_from_the_same_issue(self):
        self.grounding['issues'][0]['answer_evidence']['excerpts'].append(
            {'field': 'note:12', 'text': '수정 후에도 증상이 지속되어 재검토합니다.'})
        _, _, generate = self.run_repair(verdict={'supported': [False]})
        self.assertIn('수정 후에도 증상이 지속', generate.call_args_list[1].args[2][1]['content'])

    def test_initial_request_repair_uses_only_description_but_keeps_later_history(self):
        self.context.latest_user_text = '최초 요청한 기본값을 설명해줘'
        initial = {'issue_id': 91, 'field': 'description', 'text': '요청한 기본값은 7입니다.'}
        later = {'issue_id': 91, 'field': 'note:12', 'text': '이후 기본값을 8로 변경 요청했습니다.'}
        self.validated['answer_items'][0].update(
            question_part='최초 요청한 기본값', evidence=[initial, later], draft_answer='기본값은 8입니다.')
        self.grounding['issues'][0]['answer_evidence']['excerpts'] = [initial, later]
        original = deepcopy(self.validated)
        items, meta, generate = self.run_repair('최초 요청한 기본값은 7입니다.')
        self.assertEqual(meta['status'], 'accepted')
        self.assertEqual(items[0]['evidence'], [initial])
        prompt = json.loads(generate.call_args_list[0].args[2][1]['content'])
        self.assertEqual(prompt['items'][0]['evidence'], [initial])
        self.assertIn('8로 변경', str(prompt['history']))
        self.assertEqual(self.validated, original)

    def test_ambiguous_numeric_partial_or_mixed_approval_is_not_approval(self):
        for verdict in ({'supported': [1]}, {'supported': ['true']}, {'supported': []},
                        {'supported': [True, True]}, {'supported': [True], 'answers': ['새 답']}, ['true']):
            with self.subTest(verdict=verdict):
                items, meta, _ = self.run_repair(verdict=verdict)
                self.assertEqual(items, [])
                self.assertEqual(meta['status'], 'rejected_by_review')

    def test_empty_or_rebound_draft_cannot_change_question_items_or_sources(self):
        for draft in ({'answers': []}, {'answers': ['답'], 'evidence': ['다른 근거']}, {'answers': [3]}):
            with self.subTest(draft=draft):
                generate = Mock(return_value=reply(draft))
                items, _ = repair_summary(self.request, self.context, self.validated, self.grounding, generate)
                self.assertEqual(items, [])
                generate.assert_called_once()

    def test_transport_or_json_failure_falls_back_without_more_retries(self):
        for step in (0, 1):
            for error in (requests.Timeout(), {'content': 'invalid json'}):
                with self.subTest(step=step, error=type(error).__name__):
                    generate = Mock(side_effect=[reply({'answers': ['캐시 덮어쓰기 로직을 수정했습니다.']})]*step+[error])
                    items, meta = repair_summary(self.request, self.context, self.validated, self.grounding, generate)
                    self.assertEqual(items, [])
                    self.assertEqual(meta['status'], 'error')
                    self.assertEqual(generate.call_count, step+1)

    def test_cancel_is_propagated(self):
        generate = Mock(side_effect=RequestStopped(cancelled=True))
        with self.assertRaises(RequestStopped):
            repair_summary(self.request, self.context, self.validated, self.grounding, generate)

    def test_absent_drafts_and_oversized_history_do_not_trigger_more_model_calls(self):
        generate = Mock()
        self.validated['answer_items'][0]['draft_answer'] = ''
        items, meta = repair_summary(self.request, self.context, self.validated, self.grounding, generate)
        self.assertEqual(items, [])
        self.assertEqual(meta['calls'], 0)
        self.validated['answer_items'][0]['draft_answer'] = '100건을 수정했습니다.'
        self.grounding['issues'][0]['answer_evidence']['excerpts'][0]['text'] = '근거 '*40000
        items, meta = repair_summary(self.request, self.context, self.validated, self.grounding, generate)
        self.assertEqual(meta['status'], 'context_too_large')
        generate.assert_not_called()
