"""세 업무 문서의 빈 결과·불완전 조회·대량 결과·실제 감사 후속 계약."""
import json
import unittest
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import patch

from agent_harness_cases import FakeSources, agent_environment, chat_request, final, tool_call, turn
from test_agent_documents import DocumentModel, FILTER
from test_unified_planner import proposal


PURPOSES = {
    'handoff': '개발팀 인수인계용으로 증상과 재현 조건을 요약해줘',
    'postmortem': '사후 분석 문서로 증상과 원인·조치를 요약해줘',
    'notice': '고객 안내문 초안으로 증상과 조치를 정중하게 요약해줘',
}
QUESTIONS = {kind: '화면번호 0000 최근 8주 이슈를 ' + purpose for kind, purpose in PURPOSES.items()}


def rows(count):
    return [{'id': 41001 + i, 'summary': '[0000] 합성 검증용 조회 멈춤',
             'project': {'name': '신한투자증권'}, 'handler': {'real_name': '석지원'},
             'created_at': date.today().isoformat(), 'status': {'name': 'new'},
             'custom_fields': [{'field': {'name': '화면번호'}, 'value': '0000'}],
             'description': '종목을 바꾸면 화면이 멈춥니다.',
             'steps_to_reproduce': '종목을 바꾼 뒤 조회 버튼을 누르면 멈추는 현상을 확인했습니다.',
             'notes': [{'id': 1, 'text': '원인은 중복 요청입니다. 요청을 한 번만 처리하도록 수정했습니다.',
                        'reporter': {'name': '석지원'}}]} for i in range(count)]


def stage(raw, *, total=None, complete=True):
    from backend.sources.mantis_fields import normalize_issue
    return {'source': 'mantis_snapshot', 'status': 'ok',
            'issues_list': [normalize_issue(r) for r in raw[:20]],
            'meta': {'found_count': len(raw) if total is None else total, 'search_complete': complete,
                     'matched_issue_ids': [r['id'] for r in raw], 'ordered_matched_ids': [r['id'] for r in raw]}}


def plan(kind, *, followup=False):
    start, end = date.today() - timedelta(days=55), date.today()
    return proposal(operation='report', report_kind=kind, scope='inherit' if followup else 'team',
                    followup=followup, status={'mode': 'inherit' if followup else 'unspecified',
                                             'values': [], 'evidence': ''},
                    filters=[] if followup else [{**FILTER, 'evidence': '화면번호 0000'},
                        {'field': 'created_at', 'operator': 'between', 'value': [start.isoformat(), end.isoformat()],
                         'evidence': '최근 8주'}])


def decisions(raw):
    ids = [r['id'] for r in raw[:20]]
    filters = [{k: v for k, v in f.items() if k != 'evidence'} for f in plan('handoff')['filters']]
    return [turn(tool_call('list_mantis_issues', {'limit': 20, 'filters': filters, 'scope': 'team'})),
            *[turn(tool_call('get_mantis_issue_details', {'issue_ids': ids[i:i + 5]}))
              for i in range(0, len(ids), 5)], final('조회한 원문 범위로 작성합니다.')]


class MatrixModel(DocumentModel):
    def grounded(self, payload):
        if payload['messages'][0]['content'].startswith('사용자 질문 전체를 사내 조회 실행 계획 JSON'):
            return super().grounded(payload)
        data = json.loads(payload['messages'][-1]['content'])
        if 'claims' in data:
            return super().grounded(payload)
        issues = []
        for item in data['issues']:
            sections = []
            for ref, passage in item['passages'].items():
                slot = ('symptoms' if passage['field'] == 'description' else
                        'reproduction' if passage['field'] == 'steps_to_reproduce' else
                        'actions' if passage['field'].startswith('note:') else None)
                if slot and (slot != 'reproduction' or data['kind'] == 'handoff'):
                    sections.append({'slot': slot, 'claims': [{'text': passage['quote'], 'evidence_ids': [ref]}]})
            issues.append({'issue_id': item['issue_id'], 'sections': sections})
        return self.respond(final(json.dumps({'issues': issues}, ensure_ascii=False)))


class AgentDocumentMatrixTests(unittest.TestCase):
    def assert_document(self, response, kind):
        self.assertEqual(response.get('raw', {}).get('mode'), 'agent_issue_document', response['content'])
        self.assertNotIn('단일 이슈 번호', response['content'])
        review = response['documentReview']
        for claim in review['claims']:
            self.assertTrue(all(ref.startswith(f"M{claim['issue_id']}:") for ref in claim['evidence_ids']))
        self.assertLessEqual(response['agentReview']['modelRequests'], 16)
        self.assertLessEqual(response['agentReview']['toolCalls'], 16)
        if kind == 'notice':
            for internal in ('[0000]', '#410', '[M#', '석지원', '신한투자증권'):
                self.assertNotIn(internal, response['content'])
        return review

    def test_three_kinds_repeat_zero_partial_and_large_results(self):
        from backend.services.chat_service import ChatService
        scenarios = {
            'zero': ([], True, 0, None),
            'unfinished_zero': ([], False, 0, None),
            'partial_search': (rows(3), False, 3, None),
            'partial_body': (rows(3), True, 3, 41002),
            'large': (rows(250), True, 250, None),
        }
        for repeat in range(3):
            for kind in PURPOSES:
                for name, (raw, complete, total, incomplete_id) in scenarios.items():
                    with self.subTest(kind=kind, scenario=name, repeat=repeat):
                        source = FakeSources(lookups={r['id']: r for r in raw})
                        lookup = source.lookup

                        def read(ids, constraints=None, *, lookup=lookup, incomplete_id=incomplete_id):
                            result = lookup(ids, constraints)
                            for item in result['issues_list']:
                                if item['id'] == incomplete_id:
                                    item['answer_evidence']['complete'] = False
                            return result

                        source.lookup = read
                        fake = MatrixModel(decisions(raw), planner=plan(kind))
                        with (agent_environment(model=fake, sources=source,
                                               extra_env={'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
                              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search',
                                    return_value=stage(raw, total=total, complete=complete))):
                            response = ChatService().build_response(chat_request(QUESTIONS[kind],
                                tools={'mantis': True, 'notion': False, 'vectorDb': False}),
                                user={'uid': f'matrix-{repeat}-{kind}-{name}'})
                        review = self.assert_document(response, kind)
                        expected_ids = [r['id'] for r in raw[:20]]
                        self.assertEqual(review['searchComplete'], complete)
                        self.assertEqual(review['totalCount'], total if complete else None)
                        self.assertEqual(review['readIssueIds'], expected_ids)
                        self.assertEqual(review['summarizedIssueIds'], expected_ids)
                        self.assertEqual(review['complete'], name == 'zero')
                        self.assertEqual(review['incompleteBodyIds'], [incomplete_id] if incomplete_id else [])
                        self.assertEqual('일치하는 이슈는 0건' in response['content'], name == 'zero')
                        if name == 'large':
                            self.assertIn('전체 조회 250건', response['content'])
                            self.assertIn('원문 확보 20건', response['content'])
                            self.assertEqual({i for call in source.lookup_calls for i in call['issue_ids']}, set(expected_ids))
                        if not complete:
                            self.assertIn('총건수를 확정하지 못', response['content'])
                        if raw:
                            self.assertEqual(review['status'], 'partial')

    def test_followup_changes_document_kind_and_rereads_current_bodies(self):
        from backend.services.chat_service import ChatService
        for kind in PURPOSES:
            with self.subTest(kind=kind):
                raw = rows(2)
                fake = MatrixModel(decisions(raw), planner=plan('handoff'))
                source = FakeSources(lookups={r['id']: deepcopy(r) for r in raw})
                user = {'uid': 'matrix-followup-' + kind}
                with (agent_environment(model=fake, sources=source,
                                       extra_env={'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
                      patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage(raw)) as search):
                    initial = ChatService().build_response(chat_request(QUESTIONS['handoff']), user=user)
                    self.assertTrue(initial['documentReview']['complete'])
                    for body in source.lookups.values():
                        body['description'] = '현재는 재접속 시 목록이 비어 보입니다.'
                    fake.planner = plan(kind, followup=True)
                    fake.decisions = decisions(raw)
                    question = '같은 조건으로 ' + PURPOSES[kind]
                    request = chat_request(question, contextAuditId=initial['auditId'])
                    request.messages = [*chat_request(QUESTIONS['handoff']).messages,
                                        *chat_request(question).messages]
                    response = ChatService().build_response(request, user=user)
                review = self.assert_document(response, kind)
                self.assertTrue(review['complete'], response['content'])
                self.assertIn('현재는 재접속 시 목록이 비어 보입니다.', response['content'])
                self.assertNotIn('종목을 바꾸면 화면이 멈춥니다.', response['content'])
                self.assertEqual(len(source.lookup_calls), 4)
                self.assertEqual(search.call_args_list[0].args[1]['filter_clauses'],
                                 search.call_args_list[1].args[1]['filter_clauses'])
                self.assertEqual(search.call_args.args[1]['mantis_team_scope'], 'team')

    def test_followup_after_partial_scan_cannot_reuse_old_complete_claim(self):
        from backend.services.chat_service import ChatService
        for kind in PURPOSES:
            with self.subTest(kind=kind):
                raw = rows(2)
                fake = MatrixModel(decisions(raw), planner=plan(kind))
                source = FakeSources(lookups={r['id']: r for r in raw})
                with (agent_environment(model=fake, sources=source,
                                       extra_env={'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
                      patch('backend.tools.mantis_queries.execute_mantis_snapshot_search',
                            side_effect=[stage(raw), stage([], complete=False)])):
                    user = {'uid': 'matrix-empty-followup-' + kind}
                    first = ChatService().build_response(chat_request(QUESTIONS[kind]), user=user)
                    fake.planner = plan(kind, followup=True)
                    fake.decisions = decisions([])
                    question = '같은 조건으로 ' + PURPOSES[kind]
                    request = chat_request(question, contextAuditId=first['auditId'])
                    request.messages = [*chat_request(QUESTIONS[kind]).messages, *chat_request(question).messages]
                    response = ChatService().build_response(request, user=user)
                review = self.assert_document(response, kind)
                self.assertFalse(review['complete'])
                self.assertIsNone(review['totalCount'])
                self.assertEqual(review['summarizedIssueIds'], [])
                self.assertNotIn('화면이 멈춥니다', response['content'])
                self.assertNotIn('일치하는 이슈는 0건', response['content'])

    def test_ordinal_followup_uses_only_the_displayed_second_issue(self):
        from backend.services.chat_service import ChatService
        for kind in PURPOSES:
            with self.subTest(kind=kind):
                raw = rows(2)
                fake = MatrixModel(decisions(raw), planner=plan('handoff'))
                source = FakeSources(lookups={r['id']: r for r in raw})
                with (agent_environment(model=fake, sources=source,
                                       extra_env={'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
                      patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage(raw))):
                    user = {'uid': 'matrix-ordinal-' + kind}
                    first = ChatService().build_response(chat_request(QUESTIONS['handoff']), user=user)
                    fake.planner = proposal(operation='report', report_kind=kind, scope='target',
                        status={'mode': 'unspecified', 'values': [], 'evidence': ''},
                        filters=[{'field': 'issue_id', 'operator': 'equals', 'value': 41002, 'evidence': '0041002'}])
                    fake.decisions = [turn(tool_call('get_mantis_issue_detail', {'issue_id': 41002})), final('확인했습니다.')]
                    question = '그중 두 번째 이슈를 ' + PURPOSES[kind]
                    request = chat_request(question, contextAuditId=first['auditId'])
                    request.messages = [*chat_request(QUESTIONS['handoff']).messages, *chat_request(question).messages]
                    response = ChatService().build_response(request, user=user)
                review = self.assert_document(response, kind)
                self.assertTrue(review['complete'], response['content'])
                self.assertEqual(review['targetIssueIds'], [41002])
                self.assertEqual(review['summarizedIssueIds'], [41002])
                self.assertNotIn('#41001', response['content'])

    def test_all_kinds_keep_literal_completion_and_semantic_guards(self):
        from backend.services.agent.documents import build_document
        from test_agent_documents import generate_supported, ledger
        for kind in PURPOSES:
            for failure in ('literal', 'completion', 'semantic'):
                with self.subTest(kind=kind, failure=failure):
                    def generate(system, data, *, failure=failure):
                        if 'claims' in data:
                            return {'supported': {c['id']: failure != 'semantic' for c in data['claims']}}
                        draft = generate_supported(system, data)
                        if failure != 'semantic':
                            for issue in draft['issues']:
                                issue['sections'][0]['claims'][0]['text'] = (
                                    '총 987654건 발생했습니다.' if failure == 'literal' else '고객 검증까지 완료했습니다.')
                        return draft
                    content, review, refs = build_document(ledger(), kind, QUESTIONS[kind], generate)
                    self.assertFalse(review['complete'])
                    self.assertEqual(review['summarizedIssueIds'], [])
                    self.assertEqual(refs, [])
                    self.assertEqual(review['rejectedClaims'], 2)
                    self.assertNotIn('987654', content)
                    self.assertNotIn('고객 검증까지 완료', content)

    def test_malformed_or_truncated_output_never_bypasses_validation(self):
        from backend.services.chat_service import ChatService

        class MalformedModel(MatrixModel):
            broken_step = ''

            def grounded(self, payload):
                system = payload['messages'][0]['content']
                if system.startswith('사용자 질문 전체를 사내 조회 실행 계획 JSON'):
                    return super().grounded(payload)
                data = json.loads(payload['messages'][-1]['content'])
                step = 'review' if 'claims' in data else 'draft'
                if step == 'draft' and self.broken_step == 'draft_length':
                    return self.respond(final('{"issues":[', finish='length'))
                if step == self.broken_step:
                    return self.respond(final('{"supported":' if step == 'review' else '{"issues":['))
                return super().grounded(payload)

        for kind in PURPOSES:
            for broken in ('draft', 'review', 'draft_length'):
                with self.subTest(kind=kind, broken=broken):
                    raw = rows(20 if broken == 'draft_length' else 2)
                    fake = MalformedModel(decisions(raw), planner=plan(kind))
                    fake.broken_step = broken
                    with (agent_environment(model=fake, sources=FakeSources(lookups={r['id']: r for r in raw}),
                                           extra_env={'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
                          patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage(raw))):
                        response = ChatService().build_response(chat_request(QUESTIONS[kind]),
                                                               user={'uid': f'matrix-json-{kind}-{broken}'})
                    review = self.assert_document(response, kind)
                    self.assertFalse(review['complete'])
                    self.assertEqual(review['status'], 'unavailable')
                    self.assertEqual(review['summarizedIssueIds'], [])
                    self.assertTrue(review['failureDetail'])
                    self.assertNotIn('종목을 바꾸면 화면이 멈춥니다.', response['content'])
                    if broken == 'draft_length':
                        from collections import Counter
                        self.assertEqual(len(review['readIssueIds']), 20)
                        attempts = Counter(tuple(i['issue_id'] for i in json.loads(c['payload']['messages'][-1]['content'])['issues'])
                                           for c in fake.calls
                                           if c['payload']['messages'][0]['content'].startswith('실제로 읽은 Mantis'))
                        self.assertEqual(len(attempts), 4)
                        self.assertEqual(set(attempts.values()), {2})
