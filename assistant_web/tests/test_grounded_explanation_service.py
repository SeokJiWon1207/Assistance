import json
import unittest
from unittest.mock import Mock, patch

from backend.schemas import ChatRequest
from backend.services.contracts import ChatPipelineContext


class GroundedExplanationTests(unittest.TestCase):
    def test_model_catalog_omits_offsets_but_keeps_text_dates_and_source_catalog(self):
        from backend.services.grounded_explanation import evidence_catalog
        excerpt = {'field': 'note:17', 'note_id': 17, 'start': 40, 'end': 49,
                   'text': '확인 후 다시 요청', 'created_at': '2026-01-01',
                   'updated_at': '2026-01-03', 'reporter': '작성자'}
        grounding = {'issues': [{'id': 91, 'answer_evidence': {'complete': False, 'excerpts': [excerpt]}}]}
        shown, catalog = evidence_catalog(grounding)
        presented = shown['issues'][0]['answer_evidence']['excerpts'][0]
        self.assertNotIn('start', presented)
        self.assertNotIn('end', presented)
        self.assertEqual(presented, {'field': 'note:17', 'note_id': 17, 'text': '확인 후 다시 요청',
                                    'created_at': '2026-01-01', 'updated_at': '2026-01-03',
                                    'reporter': '작성자', 'passage_id': 'p1'})
        self.assertEqual(catalog['p1'], {'issue_id': 91, 'field': 'note:17', 'quote': '확인 후 다시 요청'})
        self.assertEqual(excerpt['start'], 40)
        self.assertEqual(excerpt['end'], 49)
        self.assertFalse(shown['issues'][0]['answer_evidence']['complete'])

    def test_only_explanations_use_the_validated_quote_path(self):
        from backend.services.chat_service import chat_service
        from backend.services.contracts import StageResult
        request = ChatRequest(modelId='test', messages=[{'role': 'user', 'content': '질문'}])
        for operation in ['explain', 'count']:
            context = ChatPipelineContext('질문', {}, '', [], '질문', {}, [], {}, 5,
                                          intent_plan={'source': 'mantis', 'operation': operation}, retrieval_result={'stages': []})
            with patch('backend.services.chat_service.plan_chat_query', return_value=StageResult.from_response(None)), patch(
                'backend.services.chat_service.retrieve_chat_evidence', return_value=StageResult.from_response(None)
            ), patch('backend.services.chat_service.generate_model_response', return_value={'content': '기존 답변', 'raw': {}}), patch(
                'backend.services.grounded_explanation.generate_grounded_explanation', return_value={'content': '검증 인용', 'raw': {}}
            ) as grounded:
                result = chat_service._complete_response(request, context, {}, lambda x: x)
            self.assertEqual(grounded.call_count, int(operation == 'explain'))
            self.assertEqual(result['content'], '검증 인용' if operation == 'explain' else '기존 답변')

    def test_chronology_keeps_ng_and_pending_after_delivery(self):
        from backend.services.grounded_explanation import timeline_evidence
        grounding = {'issues': [{'id': 9, 'answer_evidence': {'excerpts': [
            {'field': 'note:1', 'note_id': 1, 'created_at': '2025-01-01', 'text': '[고객사 전달]\n전달했습니다.'},
            {'field': 'note:2', 'note_id': 2, 'created_at': '2025-01-02', 'text': '[QC NG]\n다시 수정 요청하겠습니다.'},
            {'field': 'note:3', 'note_id': 3, 'created_at': '2025-01-03', 'text': '롤백했습니다. 다음 주 반영 예정입니다.'},
        ]}}]}
        evidence = timeline_evidence(grounding, [9])
        self.assertEqual([e['field'] for e in evidence], ['note:1', 'note:2', 'note:3'])
        self.assertIn('예정', evidence[-1]['quote'])
        self.assertEqual(timeline_evidence(grounding, [10]), [])

    def test_history_keeps_test_limitations_before_later_delivery(self):
        from backend.services.grounded_explanation import timeline_evidence
        excerpts = [{'field': 'note:1', 'note_id': 1, 'text': '개발서버 접속 불가하여 고객사에서 직접 테스트'}]
        excerpts += [{'field': f'note:{i}', 'note_id': i, 'text': '[전달]'} for i in range(2, 7)]
        result = timeline_evidence({'issues': [{'id': 9, 'answer_evidence': {'excerpts': excerpts}}]}, [9])
        self.assertIn('개발서버 접속 불가하여 고객사에서 직접 테스트', [e['quote'] for e in result])

    def test_history_restores_whole_event_note_and_initial_guidance(self):
        from backend.services.grounded_explanation import timeline_evidence
        excerpts = [{'field': 'note:1', 'note_id': 1, 'text': '초기 안내는 A입니다.'},
                    {'field': 'note:2', 'note_id': 2, 'text': '[반영 전후 동작]\n\n조건 로드는 폭 유지\n탭 변경은 폭 유지'}]
        excerpts += [{'field': f'note:{i}', 'note_id': i, 'text': '[전달]'} for i in range(3, 8)]
        result = timeline_evidence({'issues': [{'id': 9, 'answer_evidence': {'excerpts': excerpts}}]}, [9])
        self.assertIn('초기 안내는 A입니다.', [e['quote'] for e in result])
        self.assertIn('조건 로드는 폭 유지\n탭 변경은 폭 유지', [e['quote'] for e in result])

    def test_history_does_not_drop_corrections_without_event_keywords(self):
        from backend.services.grounded_explanation import timeline_evidence
        excerpts = [{'field': f'note:{i}', 'note_id': i, 'text': text} for i, text in enumerate(
            ['접수되었습니다.', 'Output이 수신되지 않습니다. 재확인 부탁드립니다.',
             '초기 안내와 다른 2037이 맞습니다.', '회신 A', '회신 B', '회신 C', '회신 D'], 1)]
        result = timeline_evidence({'issues': [{'id': 9, 'answer_evidence': {'excerpts': excerpts}}]}, [9])
        self.assertEqual(len(result), 7)

    def test_registration_context_and_missing_platform_are_distinct_from_fix_history(self):
        from backend.services.grounded_explanation import registration_evidence, platform_limits
        grounding = {'issues': [{'id': 9, 'answer_evidence': {'excerpts': [
            {'field': 'description', 'text': '강세약세 조회 시 흰 바탕 뒤에 영역이 표시됩니다.'},
            {'field': 'note:1', 'note_id': 1, 'text': 'Android의 일부 단말에서 발생합니다.'}]}}]}
        self.assertEqual(registration_evidence(grounding, [9])[0]['field'], 'description')
        limits = platform_limits('아이폰과 안드로이드 모두 문제야?', grounding, [9])
        self.assertIn('아이폰/iOS', limits)
        self.assertNotIn('Android', limits)

    def test_rejects_free_prose_instead_of_falling_back_to_unverified_completion(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        request = ChatRequest(modelId='test', messages=[{'role': 'user', 'content': 'Mantis #9 원인과 마지막 조치를 설명해줘'}])
        context = ChatPipelineContext(request.messages[-1].content, {}, '', [], 'Mantis #9', {'mantis': True}, [], {}, 5,
                                      intent_plan={'operation': 'explain'}, retrieval_result={'stages': []})
        generate = Mock(return_value={'modelId': 'test', 'content': '운영 반영 완료했습니다', 'raw': {}})
        answer = generate_grounded_explanation(request, context, generate)
        self.assertNotIn('반영 완료했습니다', answer['content'])
        self.assertEqual(answer['raw']['mode'], 'explanation_evidence_incomplete')
        self.assertLessEqual(generate.call_count, 2)

    def test_required_topics_preserve_the_question_parts(self):
        from backend.services.grounded_explanation import required_topics
        topics = required_topics('원인과 조치, 마지막 QC 진행 이력, 발신자와 노트 작성자를 구분해줘')
        self.assertTrue({'cause', 'action', 'timeline', 'actors'}.issubset(topics))

    def test_passage_ids_resolve_to_their_original_issue_and_text(self):
        from backend.services.grounded_explanation import evidence_catalog, resolve_selections
        grounding = {'issues': [{'id': 9, 'answer_evidence': {'excerpts': [
            {'field': 'note:1', 'text': '"수정" 요청입니다.\n\n완료한 것이 아닙니다.', 'reporter': '기록자'}]}}]}
        shown, catalog = evidence_catalog(grounding)
        refs = shown['issues'][0]['answer_evidence']['excerpts']
        payload = {'selected_issue_ids': [9], 'sections': [{'topic': 'action', 'evidence_ids': [refs[0]['passage_id']], 'unanswered': False}]}
        resolved = resolve_selections(payload, catalog)
        self.assertEqual(resolved['sections'][0]['evidence'][0]['issue_id'], 9)
        self.assertEqual(resolved['sections'][0]['evidence'][0]['quote'], '"수정" 요청입니다.')
        payload['sections'][0]['evidence_ids'] = ['invented']
        self.assertEqual(resolve_selections(payload, catalog), {})

    def test_supplemented_context_keeps_existing_passage_ids(self):
        from backend.services.grounded_explanation import evidence_catalog
        grounding = {'issues': [{'id': 9, 'answer_evidence': {'excerpts': [
            {'field': 'note:1', 'text': '최초 요청입니다.'}]}},
            {'id': 10, 'answer_evidence': {'excerpts': [{'field': 'note:8', 'text': '다른 후보입니다.'}]}}]}
        _shown, original = evidence_catalog(grounding)
        grounding['issues'][0]['answer_evidence']['excerpts'].append(
            {'field': 'note:2', 'text': 'QC NG로 재요청했습니다.'})
        shown, catalog = evidence_catalog(grounding, original)
        for key, value in original.items():
            self.assertEqual(catalog[key], value)
        added = shown['issues'][0]['answer_evidence']['excerpts'][-1]
        self.assertEqual(catalog[added['passage_id']]['quote'], 'QC NG로 재요청했습니다.')
        self.assertNotIn(added['passage_id'], original)

    def test_missing_note_is_read_before_focused_review_and_keeps_two_model_calls(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        from backend.sources.mantis import normalize_issue
        query = '수정 내용을 설명해줘'
        raw = {'id': 9, 'summary': '화면 수정', 'description': '화면 수정 요청입니다.', 'notes': [
            {'id': 1, 'text': '화면 수정 요청입니다.', 'created_at': '2026-01-01'},
            {'id': 2, 'text': 'QC NG로 재수정 요청했습니다.', 'created_at': '2026-01-02'}]}
        issue = normalize_issue(raw, evidence_query=query)
        issue['answer_evidence']['excerpts'] = [e for e in issue['answer_evidence']['excerpts'] if e['field'] != 'note:2']
        issue['answer_evidence']['complete'] = False
        request = ChatRequest(modelId='test', messages=[{'role': 'user', 'content': query}])
        context = ChatPipelineContext(query, {}, '', [], query, {'mantis': True}, [], {}, 5,
                                      intent_plan={'operation': 'explain'}, retrieval_result={
                                          'stages': [{'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': [issue]}]})
        seen = []

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            seen.append(shown)
            passages = shown['issues'][0]['answer_evidence']['excerpts']
            refs = [e['passage_id'] for e in passages if e['field'].startswith('note:')]
            return {'content': json.dumps({'selected_issue_ids': [9], 'sections': [
                {'topic': 'action', 'evidence_ids': refs, 'unanswered': len(seen) == 1}]}),
                'raw': {}, 'sources': [{'issue_id': 9}], 'retrieval': context.retrieval_result}

        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id', return_value=raw):
            result = generate_grounded_explanation(request, context, generate)
        self.assertEqual(len(seen), 2)
        self.assertNotIn('QC NG', json.dumps(seen[0], ensure_ascii=False))
        self.assertIn('QC NG', json.dumps(seen[1], ensure_ascii=False))
        self.assertIn('QC NG로 재수정 요청했습니다.', result['content'])
        self.assertNotIn('이 항목은 선택한 근거로 답변을 확정하지 못했습니다', result['content'])
        self.assertEqual(result['groundingReview']['noteSupplement']['supplemented_ids'], [9])
        stored = context.retrieval_result['stages'][0]['issues_list'][0]['answer_evidence']['excerpts']
        self.assertTrue(any(e['field'] == 'note:2' for e in stored))

    def test_source_changed_during_supplement_does_not_publish_old_quote(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        request, context = self.focused_generation_context()
        context.retrieval_result['stages'][0]['issues_list'][0]['answer_evidence']['complete'] = False

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            target = next(i for i in shown['issues'] if i['id'] == 9)
            passage = next(e for e in target['answer_evidence']['excerpts'] if e['field'] == 'description')
            return {'content': json.dumps({'selected_issue_ids': [9], 'sections': [
                {'topic': 'action', 'evidence_ids': [passage['passage_id']], 'unanswered': False}]}), 'raw': {}}

        with patch('backend.sources.mantis_note_supplement.fetch_mantis_issue_by_id',
                   return_value={'id': 9, 'description': '수정된 원문'}):
            result = generate_grounded_explanation(request, context, generate)
        self.assertEqual(result['raw']['mode'], 'explanation_evidence_incomplete')
        self.assertNotIn('입력값을 그대로 표시합니다.', result['content'])
        self.assertFalse(result['groundingReview']['completed'])
        self.assertIn('source_refresh_invalid', result['groundingReview']['errors'])
        self.assertEqual(result['groundingReview']['noteSupplement']['failures'][0]['reason'], 'stale_evidence')


    def test_focused_review_cannot_switch_issue_and_preserves_previous_evidence(self):
        from backend.services.grounded_explanation import merge_reviewed_selection
        first = {'valid': True, 'errors': [], 'selected_issue_ids': [9], 'sections': [
            {'topic': 'action', 'unanswered': False, 'evidence': [{'text': '수정 요청'}]}]}
        other = {'valid': True, 'errors': [], 'selected_issue_ids': [10], 'sections': []}
        self.assertFalse(merge_reviewed_selection(first, other)['valid'])
        followup = {'valid': True, 'errors': [], 'selected_issue_ids': [9], 'sections': [
            {'topic': 'action', 'unanswered': True, 'evidence': [{'text': '후속 재요청'}]}]}
        merged = merge_reviewed_selection(first, followup)
        self.assertEqual([e['text'] for e in merged['sections'][0]['evidence']], ['수정 요청', '후속 재요청'])
        self.assertTrue(merged['sections'][0]['unanswered'])

    def focused_generation_context(self):
        from backend.sources.mantis import normalize_issue
        query = '수정 내용을 설명해줘'
        request = ChatRequest(modelId='test', messages=[{'role': 'user', 'content': query}])
        rows = [normalize_issue({'id': 9, 'summary': '대상 이슈',
                                 'description': '입력값을 그대로 표시합니다.\n\n기본값 복원 시 폭은 초기화합니다.'}, evidence_query=query),
                normalize_issue({'id': 10, 'summary': '무관한 검색 후보',
                                 'description': '다른 이슈만의 구현 내용입니다.'}, evidence_query=query)]
        context = ChatPipelineContext(query, {}, '', [], query, {'mantis': True}, [], {}, 5,
                                      intent_plan={'operation': 'explain'}, retrieval_result={
                                          'stages': [{'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': rows}]})
        return request, context

    def test_two_model_calls_merge_missing_selection_and_focus_only_selected_issue(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        request, context = self.focused_generation_context()
        seen_contexts = []

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            seen_contexts.append(shown)
            target = next(issue for issue in shown['issues'] if issue['id'] == 9)
            wanted = '입력값을 그대로 표시합니다.' if len(seen_contexts) == 1 else '기본값 복원 시 폭은 초기화합니다.'
            passage = next(e for e in target['answer_evidence']['excerpts'] if e['text'] == wanted)
            payload = {'selected_issue_ids': [9], 'sections': [
                {'topic': 'action', 'evidence_ids': [passage['passage_id']], 'unanswered': False}]}
            return {'content': json.dumps(payload, ensure_ascii=False), 'raw': {},
                    'sources': [{'issue_id': 9}, {'issue_id': 10}]}

        result = generate_grounded_explanation(request, context, generate)
        self.assertEqual(len(seen_contexts), 2)
        self.assertEqual({row['id'] for row in seen_contexts[0]['issues']}, {9, 10})
        self.assertEqual([row['id'] for row in seen_contexts[1]['issues']], [9])
        self.assertNotIn('다른 이슈만의 구현 내용', json.dumps(seen_contexts[1], ensure_ascii=False))
        self.assertIn('입력값을 그대로 표시합니다.', result['content'])
        self.assertIn('기본값 복원 시 폭은 초기화합니다.', result['content'])
        self.assertNotIn('다른 이슈만의 구현 내용', result['content'])
        self.assertEqual(result['sources'], [{'issue_id': 9}])
        self.assertTrue(result['groundingReview']['focusedCoverageReview'])

    def test_focused_model_cannot_change_issue_even_with_a_valid_other_issue_passage(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        request, context = self.focused_generation_context()
        calls = []
        catalog = {}

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            calls.append(shown)
            for issue in shown['issues']:
                catalog[issue['id']] = next(e['passage_id'] for e in issue['answer_evidence']['excerpts']
                                             if e['field'] == 'description')
            selected = 9 if len(calls) % 2 else 10
            payload = {'selected_issue_ids': [selected], 'sections': [
                {'topic': 'action', 'evidence_ids': [catalog[selected]], 'unanswered': False}]}
            return {'content': json.dumps(payload), 'raw': {}, 'sources': [{'issue_id': 9}, {'issue_id': 10}]}

        result = generate_grounded_explanation(request, context, generate)
        self.assertEqual(len(calls), 4)
        self.assertEqual(result['raw']['mode'], 'explanation_evidence_incomplete')
        self.assertFalse(result['groundingReview']['completed'])
        self.assertIn('focused_review_invalid_or_changed_issue', result['groundingReview']['errors'])
        self.assertEqual(result['sources'], [])
        self.assertNotIn('다른 이슈만의 구현 내용', result['content'])

    def test_noncomparison_requires_source_relationship_for_multiple_issues(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        for related in [False, True]:
            with self.subTest(related=related):
                request, context = self.focused_generation_context()
                rows = context.retrieval_result['stages'][0]['issues_list']
                for row in rows:
                    row['project'] = '동일 프로젝트'
                    row['related_issue_ids'] = [10] if row['id'] == 9 and related else []

                def generate(_request, _model, messages, *_args):
                    shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
                    refs = [i['answer_evidence']['excerpts'][0]['passage_id'] for i in shown['issues']]
                    payload = {'selected_issue_ids': [9, 10], 'sections': [
                        {'topic': 'action', 'evidence_ids': refs, 'unanswered': False}]}
                    return {'content': json.dumps(payload), 'raw': {}, 'sources': [{'issue_id': 9}, {'issue_id': 10}]}

                result = generate_grounded_explanation(request, context, generate)
                self.assertEqual(result['groundingReview']['completed'], related)
                self.assertEqual(len(result['sources']), 2 if related else 0)

    def test_oversized_history_paragraph_does_not_discard_valid_latest_pending_note(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        from backend.sources.mantis import normalize_issue
        request, context = self.focused_generation_context()
        row = normalize_issue({'id': 9, 'summary': '대상 이슈', 'description': '수정 방식은 입력값 유지입니다.',
                               'notes': [
                                   {'id': 1, 'created_at': '2025-01-01', 'text': '분석자료 ' * 2700},
                                   {'id': 2, 'created_at': '2025-01-02', 'text': '운영 반영은 보류되었습니다.'},
                               ]}, evidence_query=context.latest_user_text)
        self.assertTrue(row['answer_evidence']['complete'])
        context.retrieval_result = {'stages': [{'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': [row]}]}

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            passage = next(e for e in shown['issues'][0]['answer_evidence']['excerpts'] if e['field'] == 'description')
            payload = {'selected_issue_ids': [9], 'sections': [
                {'topic': 'action', 'evidence_ids': [passage['passage_id']], 'unanswered': False}]}
            return {'content': json.dumps(payload), 'raw': {}, 'sources': [{'issue_id': 9}]}

        result = generate_grounded_explanation(request, context, generate)
        self.assertIn('수정 방식은 입력값 유지입니다.', result['content'])
        self.assertIn('운영 반영은 보류되었습니다.', result['content'])
        self.assertNotIn('분석자료 ' * 100, result['content'])
        self.assertFalse(result['groundingReview']['historyComplete'])


    def test_explicit_two_issue_comparison_allows_unrelated_projects(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        request, context = self.focused_generation_context()
        context.latest_user_text = context.retrieval_text = 'Mantis #9와 #10의 원인과 조치를 비교해줘'
        context.query_constraints = {'issue_ids': [9, 10]}
        for row in context.retrieval_result['stages'][0]['issues_list']:
            row['project'] = f"프로젝트 {row['id']}"
            row['related_issue_ids'] = []

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            refs = [i['answer_evidence']['excerpts'][0]['passage_id'] for i in shown['issues']]
            payload = {'selected_issue_ids': [9, 10], 'sections': [
                {'topic': topic, 'evidence_ids': refs, 'unanswered': False}
                for topic in ['cause', 'action']]}
            return {'content': json.dumps(payload), 'raw': {}, 'sources': [{'issue_id': 9}, {'issue_id': 10}]}

        result = generate_grounded_explanation(request, context, generate)
        self.assertTrue(result['groundingReview']['completed'])
        self.assertEqual(result['groundingReview']['selectedIssueIds'], [9, 10])
        self.assertIn('**#9**', result['content'])
        self.assertIn('**#10**', result['content'])

    def test_same_error_in_one_issue_does_not_authorize_unrelated_comparison(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        request, context = self.focused_generation_context()
        context.latest_user_text = context.retrieval_text = 'Mantis #9에서 동일 오류가 나는 원인을 설명해줘'
        context.query_constraints = {'issue_ids': [9]}
        for row in context.retrieval_result['stages'][0]['issues_list']:
            row['project'] = f"프로젝트 {row['id']}"
            row['related_issue_ids'] = []

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            refs = [i['answer_evidence']['excerpts'][0]['passage_id'] for i in shown['issues']]
            payload = {'selected_issue_ids': [9, 10], 'sections': [
                {'topic': 'cause', 'evidence_ids': refs, 'unanswered': False}]}
            return {'content': json.dumps(payload), 'raw': {}, 'sources': [{'issue_id': 9}, {'issue_id': 10}]}

        result = generate_grounded_explanation(request, context, generate)
        self.assertFalse(result['groundingReview']['completed'])
        self.assertEqual(result['sources'], [])
        self.assertNotIn('다른 이슈만의 구현 내용', result['content'])

    def test_credential_duplicate_fields_are_not_sent_to_either_selection_model(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        from backend.sources.mantis import normalize_issue
        request, context = self.focused_generation_context()
        credential = 'PW: fixture-only-placeholder'
        row = normalize_issue({'id': 9, 'summary': '대상 이슈',
                               'description': credential, 'additional_information': credential,
                               'notes': [{'id': 1, 'text': '입력값을 그대로 유지하도록 수정했습니다.'}]},
                              evidence_query=context.latest_user_text)
        row['matching_evidence'] = [{'field': 'description', 'text': credential}]
        row['resolution_fields'] = {'조치 및 개선': credential}
        context.retrieval_result = {'stages': [
            {'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': [row]}]}
        model_messages = []

        def generate(_request, _model, messages, *_args):
            model_messages.append(json.dumps(messages, ensure_ascii=False))
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            ref = next(e['passage_id'] for e in shown['issues'][0]['answer_evidence']['excerpts']
                       if e['field'] == 'note:1')
            payload = {'selected_issue_ids': [9], 'sections': [
                {'topic': 'action', 'evidence_ids': [ref], 'unanswered': False}]}
            return {'content': json.dumps(payload), 'raw': {}, 'sources': [{'issue_id': 9}]}

        result = generate_grounded_explanation(request, context, generate)
        self.assertTrue(result['groundingReview']['completed'])
        self.assertEqual(len(model_messages), 2)
        for messages in model_messages:
            self.assertNotIn('fixture-only-placeholder', messages)
        self.assertNotIn('fixture-only-placeholder', result['content'])
        self.assertIn('입력값을 그대로 유지하도록 수정했습니다.', result['content'])


    def test_aos_is_android_evidence(self):
        from backend.services.grounded_explanation import platform_limits
        grounding = {'issues': [{'id': 9, 'answer_evidence': {'excerpts': [
            {'field': 'description', 'text': '(iOS, AOS 모두 동일) 재현됩니다.'}]}}]}
        self.assertEqual(platform_limits('안드로이드와 아이폰 조건은?', grounding, [9]), '')

    def test_focused_review_can_add_verified_related_issue_without_dropping_primary(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        request, context = self.focused_generation_context()
        rows = context.retrieval_result['stages'][0]['issues_list']
        for row in rows:
            row['project'] = '동일 프로젝트'
            row['related_issue_ids'] = [10] if row['id'] == 9 else [9]
        calls = []

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            calls.append(shown)
            selected = [9] if len(calls) == 1 else [9, 10]
            refs = [i['answer_evidence']['excerpts'][0]['passage_id']
                    for i in shown['issues'] if i['id'] in selected]
            payload = {'selected_issue_ids': selected, 'sections': [
                {'topic': 'action', 'evidence_ids': refs, 'unanswered': False}]}
            return {'content': json.dumps(payload), 'raw': {}, 'sources': [{'issue_id': 9}, {'issue_id': 10}]}

        result = generate_grounded_explanation(request, context, generate)
        self.assertEqual(len(calls), 2)
        self.assertEqual({i['id'] for i in calls[1]['issues']}, {9, 10})
        self.assertTrue(result['groundingReview']['completed'])
        self.assertEqual(result['groundingReview']['selectedIssueIds'], [9, 10])


    def test_before_after_comparison_does_not_authorize_unrelated_issues(self):
        from backend.services.grounded_explanation import generate_grounded_explanation
        request, context = self.focused_generation_context()
        context.latest_user_text = context.retrieval_text = '고객 HTS 조회 기능의 수정 전후 동작을 비교해서 설명해줘'
        context.query_constraints = {'platform_key': 'HTS'}
        for row in context.retrieval_result['stages'][0]['issues_list']:
            row['project'] = '동일 고객 프로젝트'
            row['related_issue_ids'] = []

        def generate(_request, _model, messages, *_args):
            shown = json.loads(messages[0]['content'].rsplit('[검색 파이프라인 결과]\n', 1)[1])
            refs = [next(e['passage_id'] for e in i['answer_evidence']['excerpts']
                         if e['field'] == 'description') for i in shown['issues']]
            payload = {'selected_issue_ids': [9, 10], 'sections': [
                {'topic': 'action', 'evidence_ids': refs, 'unanswered': False}]}
            return {'content': json.dumps(payload), 'raw': {}, 'sources': [{'issue_id': 9}, {'issue_id': 10}]}

        result = generate_grounded_explanation(request, context, generate)
        self.assertFalse(result['groundingReview']['completed'])
        self.assertEqual(result['sources'], [])
        self.assertNotIn('다른 이슈만의 구현 내용', result['content'])

    def test_comparison_scope_keeps_explicit_ids_and_requests_for_other_cases(self):
        from backend.services.grounded_explanation import allows_issue_comparison
        cases = [
            ('#9와 #10의 변경 내용을 설명해줘', {'issue_ids': [9, 10]}),
            ('고객의 여러 이슈 간 원인을 비교해줘', {}),
            ('두 이슈의 조치를 비교해줘', {}),
            ('서로 다른 이슈 사이의 처리 경과를 비교해줘', {}),
            ('이 현상과 유사한 이슈를 설명해줘', {}),
            ('비슷한 사례의 조치를 설명해줘', {}),
            ('다른 이슈의 처리 이력도 설명해줘', {}),
        ]
        for query, constraints in cases:
            with self.subTest(query=query):
                self.assertTrue(allows_issue_comparison(query, constraints))

    def test_multiple_inquiries_are_distinct_from_two_symptoms_inside_one_issue(self):
        from backend.services.grounded_explanation import allows_issue_comparison
        self.assertTrue(allows_issue_comparison('SK 갭 문의 두 건을 구분해줘. 각각 왜 그런 거야?', {}))
        self.assertTrue(allows_issue_comparison('두 건의 문의에서 원인과 마지막 요청을 구분해줘', {}))
        self.assertFalse(allows_issue_comparison('Mantis #9 두 문제의 결과와 배포 상태를 나눠줘', {'issue_ids': [9]}))


if __name__ == '__main__':
    unittest.main()
