"""이슈 문서의 모델 서술은 원문 문단에 연결되고 검토를 통과한 경우에만 결정형 추출값을 대체한다."""
from unittest import TestCase
from unittest.mock import patch

import requests

MODEL = {'id': 'test-model', 'model': 'test-model'}
RAW = {'id': 31003, 'summary': '[1768] 화면 사용자 개체 증가', 'project': {'name': '138Meritz(메리츠)'},
       'status': {'name': 'assigned'}, 'handler': {'name': 'hong', 'real_name': '홍길동'},
       'notes': [{'id': 11, 'created_at': '2026-05-01', 'reporter': {'real_name': '홍길동'},
                  'text': '원인: 그리드 재조회 시 타이머 해제 누락으로 사용자 개체가 증가함.'},
                 {'id': 12, 'created_at': '2026-05-02', 'text': '그리드 초기화 시 KillTimer 호출을 추가하여 수정함.'},
                 {'id': 13, 'created_at': '2026-05-03', 'text': '[QC 요청]'},
                 {'id': 14, 'created_at': '2026-05-04', 'text': '고객사에서 장시간 사용 시 화면이 느려진다고 재확인 요청함.'}]}


def pid(quote):
    from backend.reports.document_narrative import passage_catalog
    _, catalog = passage_catalog(RAW)
    return next(key for key, row in catalog.items() if quote in row['quote'])


def claim(text, *quotes):
    return {'text': text, 'evidence_ids': [pid(quote) for quote in quotes]}


def ok(*values):
    return {'supported': {f'c{index}': value for index, value in enumerate(values, start=1)}}


def slots(**values):
    return {slot: values.get(slot, []) for slot in
            ('cause', 'actions', 'impact', 'open_items', 'next_steps', 'customer_summary')}


class DocumentNarrativeTests(TestCase):
    def build(self, kind, *responses, text='#31003 문서 초안', model=MODEL):
        from backend.reports.issue_documents import build_issue_document_report
        with patch('backend.reports.issue_documents.fetch_mantis_issue_by_id', return_value=RAW), \
             patch('backend.reports.document_narrative._chat', side_effect=list(responses)) as chat:
            result = build_issue_document_report(text, {'issue_ids': [31003]}, kind, model=model, api_key='key')
        return result, chat

    def fallback(self, kind):
        from backend.reports.issue_documents import build_issue_document_report
        with patch('backend.reports.issue_documents.fetch_mantis_issue_by_id', return_value=RAW):
            return build_issue_document_report('#31003 문서 초안', {'issue_ids': [31003]}, kind)['content']

    def test_accepted_handoff_replaces_extracted_text_with_cited_sentences(self):
        draft = slots(cause=[claim('그리드 재조회 시 타이머 해제가 누락되어 사용자 개체가 늘었습니다.', '타이머 해제 누락')],
                      actions=[claim('그리드 초기화 시 KillTimer 호출을 추가하는 수정 기록이 있습니다.', 'KillTimer')],
                      open_items=[claim('고객사가 장시간 사용 시 화면 지연을 재확인해 달라고 요청했습니다.', '재확인 요청')],
                      next_steps=[claim('장시간 사용 조건에서 화면 지연이 재현되는지 확인하세요.', '재확인 요청')])
        result, chat = self.build('handoff', draft, ok(True, True, True, True))
        content = result['content']
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(result['retrieval']['document_narrative']['status'], 'accepted')
        self.assertIn('그리드 재조회 시 타이머 해제가 누락되어 사용자 개체가 늘었습니다. [노트 11]', content)
        self.assertIn('KillTimer 호출을 추가하는 수정 기록이 있습니다. [노트 12]', content)
        self.assertIn('### 막힌 점\n고객사가 장시간 사용 시 화면 지연을 재확인해 달라고 요청했습니다. [노트 14]', content)
        self.assertIn('### 다음 행동\n제안: 장시간 사용 조건에서 화면 지연이 재현되는지 확인하세요. [노트 14]', content)
        self.assertIn('### 타임라인과 근거', content)

    def test_any_unsupported_sentence_keeps_the_whole_deterministic_draft(self):
        cases = {
            '근거 없는 수치': slots(cause=[claim('타이머 해제 누락으로 개체가 300개 늘었습니다.', '타이머 해제 누락')]),
            '요청을 완료로 확대': slots(actions=[claim('QC 완료 후 전달했습니다.', 'QC 요청')]),
            '수정을 해결로 확대': slots(actions=[claim('KillTimer 호출을 추가해 해결했습니다.', 'KillTimer')]),
            '없는 근거 ID': slots(cause=[{'text': '타이머 해제 누락입니다.', 'evidence_ids': ['p999']}]),
            '출처 표시 혼입': slots(cause=[claim('타이머 해제 누락입니다 [노트 11].', '타이머 해제 누락')]),
            '완료된 제안': slots(next_steps=[claim('QC를 완료했습니다.', 'QC 요청')]),
            '남은 일의 완료 단정': slots(open_items=[claim('고객사 확인을 마쳤습니다.', '재확인 요청')]),
        }
        expected = self.fallback('handoff')
        for name, draft in cases.items():
            with self.subTest(name=name):
                result, chat = self.build('handoff', draft, ok(True))
                self.assertEqual(result['content'], expected)
                self.assertEqual(chat.call_count, 1)
                self.assertEqual(result['retrieval']['document_narrative']['status'], 'rejected')

    def test_one_unsupported_section_falls_back_alone(self):
        draft = slots(cause=[claim('그리드 재조회 시 타이머 해제가 누락되었습니다.', '타이머 해제 누락')],
                      actions=[claim('KillTimer 호출을 추가해 해결했습니다.', 'KillTimer')],
                      open_items=[claim('고객사가 장시간 사용 시 화면 지연 재확인을 요청했습니다.', '재확인 요청')])
        result, chat = self.build('handoff', draft, ok(True, False))
        content, meta = result['content'], result['retrieval']['document_narrative']
        self.assertEqual(meta['status'], 'partial')
        self.assertEqual([(r['slot'], r['stage']) for r in meta['rejected_claims']],
                         [('actions', 'validation'), ('open_items', 'review')])
        self.assertIn('그리드 재조회 시 타이머 해제가 누락되었습니다. [노트 11]', content)
        fallback = self.fallback('handoff')
        for section in ('### 시도한 조치', '### 막힌 점'):
            self.assertEqual(content.split(section)[1].split('###')[0], fallback.split(section)[1].split('###')[0])
        self.assertNotIn('해결했습니다', content)

    def test_completed_records_are_read_from_action_fields_and_delivery_notes(self):
        notes = [{'id': 41, 'created_at': '2026-05-01',
                  'text': '수정 소스 전달드리오니, 반영 후 차트 색상이 유지되는지 확인 부탁드립니다.'}]
        fields = [{'field': {'name': '조치 및 개선'}, 'value': '정상 동작 하는 파일 고객사에 재전달'}]
        with patch.dict(RAW, notes=notes, custom_fields=fields):
            for text, quote in [('정상 동작하는 파일을 고객사에 재전달했습니다.', '재전달'),
                                ('수정 소스를 고객사에 전달했습니다.', '전달드리오니')]:
                with self.subTest(text=text):
                    result, _ = self.build('handoff', slots(actions=[claim(text, quote)]), ok(True))
                    self.assertEqual(result['retrieval']['document_narrative']['status'], 'accepted')
        planned = [{'field': {'name': '조치 및 개선'}, 'value': '수정 파일 고객사 전달 예정'}]
        with patch.dict(RAW, notes=[], custom_fields=planned):
            result, _ = self.build('handoff', slots(actions=[claim('수정 파일을 고객사에 전달했습니다.', '전달 예정')]),
                                   ok(True))
        self.assertEqual(result['retrieval']['document_narrative']['status'], 'rejected')

    def test_hedged_cause_must_stay_hedged(self):
        raw_note = '타이머 해제 누락으로 인한 현상으로 추정됨.'
        with patch.dict(RAW, notes=[{'id': 21, 'created_at': '2026-05-01', 'text': raw_note}]):
            rejected, _ = self.build('handoff', slots(cause=[claim('원인은 타이머 해제 누락입니다.', '추정됨')]),
                                     ok(True))
            accepted, _ = self.build('handoff', slots(cause=[claim('타이머 해제 누락이 원인으로 추정됩니다.', '추정됨')]),
                                     ok(True))
        self.assertEqual(rejected['retrieval']['document_narrative']['status'], 'rejected')
        self.assertEqual(accepted['retrieval']['document_narrative']['status'], 'accepted')

    def test_review_rejection_and_model_failure_fall_back(self):
        expected = self.fallback('postmortem')
        draft = slots(cause=[claim('그리드 재조회 시 타이머 해제가 누락되었습니다.', '타이머 해제 누락')])
        reviewed, _ = self.build('postmortem', draft, ok(False))
        failed, _ = self.build('postmortem', requests.ConnectionError('down'))
        no_model, chat = self.build('postmortem', model=None)
        for result, status in [(reviewed, 'rejected'), (failed, 'unavailable'), (no_model, 'not_requested')]:
            with self.subTest(status=status):
                self.assertEqual(result['content'], expected)
                self.assertEqual(result['retrieval']['document_narrative']['status'], status)
        self.assertEqual(chat.call_count, 0)

    def test_customer_notice_uses_safe_summary_without_internal_identifiers(self):
        safe = slots(customer_summary=[claim('장시간 사용 시 화면이 느려지는 현상을 확인하고 있습니다.', '재확인 요청')],
                     impact=[claim('장시간 사용하는 화면에서 지연이 보고되었습니다.', '재확인 요청')])
        result, _ = self.build('notice', safe, ok(True, True))
        content = result['content']
        self.assertIn('장시간 사용 시 화면이 느려지는 현상을 확인하고 있습니다.', content)
        self.assertIn('### 고객 영향\n장시간 사용하는 화면에서 지연이 보고되었습니다.', content)
        for forbidden in ['31003', '노트', '[', 'KillTimer', '홍길동']:
            self.assertNotIn(forbidden, content)

        expected = self.fallback('notice')
        unsafe = {
            '이슈 번호': '31003 이슈의 화면 지연을 확인하고 있습니다.',
            '담당자 이름': '홍길동 담당자가 화면 지연을 확인하고 있습니다.',
            '내부 함수명': 'KillTimer 호출을 추가했습니다.',
            '화면 코드': '1768 화면의 지연을 확인하고 있습니다.',
        }
        quotes = {'이슈 번호': '재확인 요청', '담당자 이름': '재확인 요청', '내부 함수명': 'KillTimer', '화면 코드': '재확인 요청'}
        for name, text in unsafe.items():
            with self.subTest(name=name):
                result, _ = self.build('notice', slots(customer_summary=[claim(text, quotes[name])]), ok(True))
                self.assertEqual(result['content'], expected)
                self.assertEqual(result['retrieval']['document_narrative']['status'], 'rejected')

    def test_notice_keeps_only_customer_sections_and_title_summary_when_none_is_written(self):
        impact = [claim('장시간 사용하는 화면에서 지연이 보고되었습니다.', '재확인 요청')]
        internal = [claim('그리드 초기화 시 KillTimer 호출을 추가하는 수정 기록이 있습니다.', 'KillTimer')]
        notice, chat = self.build('notice', slots(impact=impact, actions=internal), ok(True))
        self.assertEqual(notice['retrieval']['document_narrative']['status'], 'accepted')
        self.assertEqual(len(chat.call_args_list[1].args[3]['claims']), 1)
        self.assertIn('### 현재 확인된 사실\n화면 사용자 개체 증가', notice['content'])
        self.assertIn('### 고객 영향\n장시간 사용하는 화면에서 지연이 보고되었습니다.', notice['content'])
        self.assertNotIn('KillTimer', notice['content'])
        summary = [claim('장시간 사용 시 화면이 느려지는 현상을 확인하고 있습니다.', '재확인 요청')]
        handoff, _ = self.build('handoff', slots(customer_summary=summary), ok(True))
        self.assertEqual(handoff['retrieval']['document_narrative']['reason'], 'customer summary outside notice')

    def test_access_information_is_never_offered_to_the_model(self):
        from backend.reports.document_narrative import passage_catalog
        with patch.dict(RAW, notes=[*RAW['notes'], {'id': 30, 'created_at': '2026-05-05',
                                                     'text': '서버 접속 정보\nIP: 10.0.0.1\nPW: secret123'}]):
            passages, _ = passage_catalog(RAW)
        self.assertFalse(any('secret123' in passage['text'] for passage in passages))

    def test_plan_execution_passes_the_session_model_to_documents(self):
        from backend.schemas import ChatRequest
        from backend.services.contracts import ChatPipelineContext
        from backend.services.unified_planning import execute_plan
        q = '#31003 인수인계 문서 작성해줘'
        ctx = ChatPipelineContext(q, MODEL, 'key', [], q, {'mantis': True}, [], {'issue_ids': [31003]}, 5,
                                  intent_plan={'planner': 'unified_llm', 'source': 'mantis', 'operation': 'report',
                                               'report_kind': 'handoff'})
        with patch('backend.reports.issue_documents.build_issue_document_report',
                   return_value={'content': '문서', 'raw': {'mode': 'deterministic_issue_document'}}) as build:
            execute_plan(ChatRequest(modelId='test', messages=[{'role': 'user', 'content': q}]), ctx, {})
        self.assertEqual(build.call_args.kwargs, {'model': MODEL, 'api_key': 'key'})

    def test_review_must_judge_every_sentence_by_id(self):
        draft = slots(cause=[claim('그리드 재조회 시 타이머 해제가 누락되었습니다.', '타이머 해제 누락')],
                      open_items=[claim('고객사가 장시간 사용 시 화면 지연 재확인을 요청했습니다.', '재확인 요청')])
        for name, review in [('누락', {'supported': {'c1': True}}), ('순서 배열', {'supported': [True, True]}),
                             ('추가 키', {'supported': {'c1': True, 'c2': True, 'c3': True}})]:
            with self.subTest(name=name):
                result, _ = self.build('handoff', draft, review)
                self.assertEqual(result['retrieval']['document_narrative']['reason'], 'review rejected')
        result, chat = self.build('handoff', draft, {'supported': {'c2': True, 'c1': True}})
        self.assertEqual(result['retrieval']['document_narrative']['status'], 'accepted')
        self.assertEqual([c['id'] for c in chat.call_args_list[1].args[3]['claims']], ['c1', 'c2'])
