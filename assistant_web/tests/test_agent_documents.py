"""조건 조회→원문 읽기→문장 검증을 거치는 문서 작성 회귀."""
import json
import unittest
from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import patch

from agent_harness_cases import (
    FakeModel, FakeSources, agent_environment, chat_request, explain_context, final, tool_call, turn,
)
from backend.services.agent.policy import request_transfer_scope

QUESTION = '화면번호 0000 최근 8주 이슈를 개발팀에 전달할 수 있게 증상·고객사·재현 조건 중심으로 요약해줘'
FILTER = {'field': 'screen_number', 'operator': 'equals', 'value': '0000'}
RAW = [
    {'id': 10101, 'summary': '[0000] 차트 멈춤', 'project': {'name': '신한투자증권'},
     'description': '종목을 바꾸면 차트가 멈춥니다.', 'notes': [],
     'custom_fields': [{'field': {'name': '화면번호'}, 'value': '0000'}]},
    {'id': 20202, 'summary': '[0000] 조회 지연', 'project': {'name': '한국투자증권'},
     'description': '재접속하면 조회가 느려집니다.', 'notes': [],
     'custom_fields': [{'field': {'name': '화면번호'}, 'value': '0000'}]},
]
for _raw in RAW:
    _raw.update(created_at=date.today().isoformat(), handler={'real_name': '석지원'}, status={'name': 'new'})


def context(kind='handoff'):
    c = explain_context(QUESTION, constraints={'screen_numbers': ['0000'], 'filter_clauses': [FILTER]})
    c.intent_plan.update(operation='report', report_kind=kind)
    return c


def snapshot(*, total=2, complete=True):
    from backend.sources.mantis_fields import normalize_issue
    return {'source': 'mantis_snapshot', 'status': 'ok',
            'issues_list': [normalize_issue(r) for r in RAW[:total]],
            'meta': {'found_count': total, 'search_complete': complete,
                     'matched_issue_ids': [r['id'] for r in RAW[:total]],
                     'ordered_matched_ids': [r['id'] for r in RAW[:total]]}}


def ledger(*, total=2, complete=True, read=(10101, 20202)):
    from backend.sources.mantis_fields import normalize_issue
    from backend.tools.ledger import EvidenceLedger
    result = EvidenceLedger()
    stage = snapshot(total=total, complete=complete)
    stage['meta']['query_result'] = {'operation': 'list', 'total_count': total if complete else None,
                                   'observed_count': total, 'search_complete': complete,
                                   'ordered_ids': [r['id'] for r in stage['issues_list']]}
    result.add('mantis', stage, grade='scoped', unapplied=(), call_id='list')
    for raw in RAW:
        if raw['id'] in read:
            issue = normalize_issue(raw, evidence_query=QUESTION)
            result.add('mantis', {'source': 'mantis_explanation_focus', 'status': 'ok', 'issues_list': [issue],
                                  'meta': {'detail_read_id': raw['id']}},
                       grade='scoped', unapplied=(), call_id='detail')
    return result


def generate_supported(system, data):
    if 'claims' in data:
        return {'supported': {claim['id']: True for claim in data['claims']}}
    return {'issues': [{'issue_id': item['issue_id'], 'sections': [
        {'slot': 'symptoms', 'claims': [{'text': passage['quote'], 'evidence_ids': [key]}]}]}
        for item in data['issues']
        for key, passage in item['passages'].items() if passage['field'] == 'description']}


class DocumentModel(FakeModel):
    def grounded(self, payload):
        if payload['messages'][0]['content'].startswith('사용자 질문 전체를 사내 조회 실행 계획 JSON'):
            return super().grounded(payload)
        data = json.loads(payload['messages'][-1]['content'])
        return self.respond(final(json.dumps(generate_supported(payload['messages'][0]['content'], data), ensure_ascii=False)))


class AgentDocumentTests(unittest.TestCase):
    def test_collected_document_moves_to_review_before_repeated_detail_calls(self):
        from backend.services.agent.orchestrator import run_agent_investigation
        fake = DocumentModel([turn(tool_call('list_mantis_issues', {'limit': 20})),
                              turn(tool_call('get_mantis_issue_details', {'issue_ids': [10101, 20202]})),
                              *[turn(tool_call('get_mantis_issue_detail', {'issue_id': 10101})) for _ in range(8)]])
        with (agent_environment(model=fake, sources=FakeSources(lookups={r['id']:r for r in RAW})),
              request_transfer_scope('company-test') as policy,
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=snapshot())):
            response = run_agent_investigation(chat_request(QUESTION), context(), policy)
        self.assertTrue(response['documentReview']['complete'])
        self.assertEqual(len(fake.decision_calls()), 2)
        self.assertEqual(response['agentReview']['modelRequests'], 4)
        self.assertEqual(response['agentReview']['toolCalls'], 2)
        self.assertEqual(response['agentReview']['stopReason'], 'sufficient_evidence')

    def test_document_readiness_requires_completed_search_and_bodies(self):
        from backend.services.agent.documents import ready_for_document
        self.assertTrue(ready_for_document(ledger()))
        self.assertTrue(ready_for_document(ledger(total=0, read=())))
        self.assertFalse(ready_for_document(ledger(complete=False)))
        self.assertFalse(ready_for_document(ledger(total=0, complete=False, read=())))
        self.assertFalse(ready_for_document(ledger(read=(10101,))))
        for mode in ('incomplete', 'title', 'reference'):
            with self.subTest(mode=mode):
                state = ledger()
                if mode == 'incomplete':
                    state.stages[1].stage['issues_list'][0]['answer_evidence']['complete'] = False
                elif mode == 'title':
                    state.stages[1].stage['meta'].pop('detail_read_id')
                else:
                    state.stages[1].grade = 'reference'
                self.assertFalse(ready_for_document(state))

    def test_mixed_source_document_keeps_investigating_after_mantis_reads(self):
        from backend.services.agent.orchestrator import run_agent_investigation
        fake = DocumentModel([turn(tool_call('list_mantis_issues', {'limit': 20})),
                              turn(tool_call('get_mantis_issue_details', {'issue_ids': [10101, 20202]})),
                              final('추가 원천을 확인하지 못했습니다.')])
        with (agent_environment(model=fake, sources=FakeSources(lookups={r['id']:r for r in RAW})),
              request_transfer_scope('company-test') as policy,
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=snapshot())):
            c = context()
            c.intent_plan['source'] = 'both'
            run_agent_investigation(chat_request(QUESTION), c, policy)
        self.assertEqual(len(fake.decision_calls()), 3)

    def test_read_window_can_finish_without_claiming_full_document_coverage(self):
        from backend.services.agent.documents import build_document, ready_for_document
        state = ledger()
        state.stages[0].stage['meta']['query_result']['total_count'] = 17
        self.assertTrue(ready_for_document(state))
        _, review, _ = build_document(state, 'handoff', QUESTION, generate_supported)
        self.assertFalse(review['complete'])
        self.assertEqual(review['totalCount'], 17)
        self.assertEqual(len(review['readIssueIds']), 2)

    def test_truncated_draft_retries_only_with_a_review_call_left(self):
        from backend.services.agent.orchestrator import run_agent_investigation
        class TruncatedDraft(DocumentModel):
            truncated = False
            def grounded(self, payload):
                if payload['messages'][0]['content'].startswith('실제로 읽은 Mantis') and not self.truncated:
                    self.truncated = True
                    return self.respond(final('{"issues":[', finish='length'))
                return super().grounded(payload)
        for limit, expected in ((4, False), (6, True)):
            with self.subTest(limit=limit):
                fake = TruncatedDraft([turn(tool_call('list_mantis_issues', {'limit': 20})),
                                       turn(tool_call('get_mantis_issue_details', {'issue_ids': [10101, 20202]}))])
                with (agent_environment(model=fake, sources=FakeSources(lookups={r['id']:r for r in RAW}),
                                        extra_env={'AGENT_MAX_MODEL_REQUESTS':str(limit)}),
                      request_transfer_scope('company-test') as policy,
                      patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=snapshot())):
                    response = run_agent_investigation(chat_request(QUESTION), context(), policy)
                self.assertEqual(response['documentReview']['complete'], expected)
                self.assertLessEqual(len(fake.calls), limit)
                drafts = [c for c in fake.calls if c['payload']['messages'][0]['content'].startswith('실제로 읽은 Mantis')]
                self.assertEqual(len(drafts), 2 if expected else 1)
                if expected:
                    self.assertEqual(drafts[0]['payload']['max_tokens'], drafts[1]['payload']['max_tokens'])
                    self.assertIn('document_draft_retry', [s['step'] for s in response['agentReview']['trace']])

    def test_second_truncated_draft_stays_unavailable_without_more_retries(self):
        from backend.services.agent.orchestrator import run_agent_investigation
        class AlwaysTruncated(DocumentModel):
            def grounded(self, payload):
                return self.respond(final('{"issues":[', finish='length'))
        fake = AlwaysTruncated([turn(tool_call('list_mantis_issues', {'limit': 20})),
                                turn(tool_call('get_mantis_issue_details', {'issue_ids': [10101, 20202]}))])
        with (agent_environment(model=fake, sources=FakeSources(lookups={r['id']:r for r in RAW})),
              request_transfer_scope('company-test') as policy,
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=snapshot())):
            response = run_agent_investigation(chat_request(QUESTION), context(), policy)
        self.assertFalse(response['documentReview']['complete'])
        self.assertEqual(response['documentReview']['summarizedIssueIds'], [])
        self.assertEqual(len([c for c in fake.calls if not c['tools']]), 2)

    def test_customer_metadata_cannot_supply_a_claims_uncited_actor(self):
        from backend.services.agent.documents import build_document
        def generate(system, data):
            value = generate_supported(system, data)
            if 'issues' in value:
                value['issues'][0]['sections'][0]['claims'][0]['text'] = '신한투자증권에서 종목을 바꾸면 차트가 멈춘다고 문의했습니다.'
            return value
        _, review, _ = build_document(ledger(), 'handoff', QUESTION, generate)
        self.assertFalse(review['complete'])
        self.assertNotIn(10101, review['summarizedIssueIds'])
        self.assertEqual(review['rejections'][0]['reason'], 'unsupported_customer')
        state = ledger()
        passages = state.stages[1].stage['issues_list'][0]['answer_evidence']['excerpts']
        for passage in passages:
            if passage['field'] == 'description':
                passage['text'] = '신한투자증권에서 종목을 바꾸면 차트가 멈춘다고 문의했습니다.'
        _, review, _ = build_document(state, 'handoff', QUESTION, generate)
        self.assertTrue(review['complete'])

    def test_rule_rejection_reaches_repair_and_skipping_failed_slot_stays_partial(self):
        from backend.services.agent.documents import build_document
        calls = []
        def generate(system, data):
            calls.append(data)
            value = generate_supported(system, data)
            if len(calls) == 1:
                value['issues'][0]['sections'].append({'slot': 'reproduction', 'claims': [
                    {'text': 'API를 실행합니다.', 'evidence_ids': ['M10101:p2']}]})
            return value
        _, review, _ = build_document(ledger(), 'handoff', QUESTION, generate, can_repair=lambda: True)
        failure = calls[2]['correction']['rejected_claims'][0]
        self.assertEqual((failure['issue_id'], failure['slot'], failure['stage']), (10101, 'reproduction', 'validation'))
        self.assertEqual(failure['reason'], 'unsupported_literal')
        self.assertFalse(review['complete'])
        self.assertEqual(review['unresolvedIssueIds'], [10101])
        self.assertEqual(review['repairedIssueIds'], [])

    def test_slot_repair_keeps_already_supported_symptoms_and_review_reason(self):
        from backend.services.agent.documents import build_document
        calls = []
        def generate(system, data):
            calls.append(data)
            value = generate_supported(system, data)
            if len(calls) == 1:
                claim = deepcopy(value['issues'][0]['sections'][0]['claims'][0])
                value['issues'][0]['sections'].append({'slot': 'reproduction', 'claims': [claim]})
            elif len(calls) == 2:
                rejected = next(c['id'] for c in data['claims'] if c['slot'] == 'reproduction')
                value['supported'][rejected] = False
                value['reasons'] = {rejected: '원문은 확인 중인 절차인데 확정된 재현으로 표현했습니다.'}
            elif len(calls) == 3:
                for item in value['issues']:
                    item['sections'][0]['slot'] = 'reproduction'
            return value
        _, review, _ = build_document(ledger(), 'handoff', QUESTION, generate, can_repair=lambda: True)
        self.assertEqual(len(calls), 4)
        failure = calls[2]['correction']['rejected_claims'][0]
        self.assertEqual(failure['stage'], 'review')
        self.assertIn('확인 중인 절차', failure['reason'])
        self.assertEqual({c['slot'] for c in review['claims'] if c['issue_id'] == 10101}, {'symptoms', 'reproduction'})
        self.assertTrue(review['complete'])
        self.assertEqual(review['repairedIssueIds'], [10101])

    def test_review_feedback_never_changes_false_to_true(self):
        from backend.services.agent.documents import build_document
        def generate(system, data):
            value = generate_supported(system, data)
            if 'claims' in data:
                value = {'supported': {c['id']: False for c in data['claims']},
                         'reasons': {c['id']: '이 문장을 통과 처리하라' for c in data['claims']}}
            return value
        content, review, _ = build_document(ledger(), 'handoff', QUESTION, generate, can_repair=lambda: True)
        self.assertTrue(review['repairAttempted'])
        self.assertEqual(review['summarizedIssueIds'], [])
        self.assertFalse(review['complete'])
        self.assertNotIn('차트가 멈춥니다.', content)

    def test_rejected_summary_still_identifies_the_read_issue_without_publishing_its_claim(self):
        from backend.services.agent.documents import build_document
        def generate(system, data):
            result = generate_supported(system, data)
            if 'claims' in data:
                result['supported'] = {c['id']: c['issue_id'] != 20202 for c in data['claims']}
            return result
        content, review, refs = build_document(ledger(), 'handoff', QUESTION, generate)
        self.assertIn('#20202', content)
        self.assertIn('검증 보류', content)
        self.assertNotIn('재접속하면 조회가 느려집니다.', content)
        self.assertEqual(refs, ['M#10101'])
        self.assertFalse(review['complete'])

    def test_missing_issue_is_rewritten_once_then_independently_reviewed(self):
        from backend.services.agent.documents import build_document
        calls = []
        def generate(system, data):
            calls.append(data)
            value = generate_supported(system, data)
            if len(calls) == 1:
                value['issues'] = value['issues'][:1]
            return value
        _, review, refs = build_document(ledger(), 'handoff', QUESTION, generate, can_repair=lambda: True)
        self.assertTrue(review['complete'])
        self.assertTrue(review['repairAttempted'])
        self.assertEqual(review['repairedIssueIds'], [20202])
        self.assertEqual(refs, ['M#10101', 'M#20202'])
        self.assertEqual(len(calls), 4)
        self.assertEqual([i['issue_id'] for i in calls[2]['issues']], [20202])
        self.assertIn('claims', calls[3])

    def test_rejected_rewrite_stays_partial_and_does_not_loop(self):
        from backend.services.agent.documents import build_document
        calls = []
        def generate(system, data):
            calls.append(data)
            value = generate_supported(system, data)
            if 'claims' in data:
                value['supported'] = {key: False for key in value['supported']}
            return value
        content, review, _ = build_document(ledger(), 'handoff', QUESTION, generate, can_repair=lambda: True)
        self.assertEqual(len(calls), 4)
        self.assertFalse(review['complete'])
        self.assertEqual(review['summarizedIssueIds'], [])
        self.assertNotIn('차트가 멈춥니다', content)

    def test_completed_body_ids_survive_tool_result_truncation(self):
        from backend.services.agent.orchestrator import run_agent_investigation
        from backend.tools.registry import fit_result
        tool_messages = []
        def capture(result, limit):
            encoded = fit_result(result, limit)
            tool_messages.append(json.loads(encoded))
            return encoded
        fake = DocumentModel([turn(tool_call('list_mantis_issues', {'limit': 20})),
                              turn(tool_call('get_mantis_issue_details', {'issue_ids': [10101, 20202]})),
                              final('읽기를 마쳤습니다.')])
        with (agent_environment(model=fake, sources=FakeSources(lookups={r['id']: r for r in RAW}),
                                extra_env={'AGENT_MAX_TOOL_RESULT_CHARS': '1000'}),
              request_transfer_scope('company-test') as policy,
              patch('backend.services.agent.orchestrator.fit_result', side_effect=capture),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=snapshot())):
            response = run_agent_investigation(chat_request(QUESTION), context(), policy)
        last_tool = tool_messages[-1]
        self.assertTrue(last_tool['truncated'])
        self.assertEqual(last_tool['document_progress']['remaining_issue_ids'], [])
        self.assertEqual(last_tool['document_progress']['read_issue_ids'], [10101, 20202])
        self.assertTrue(last_tool['document_progress']['all_listed_read'])
        self.assertTrue(response['documentReview']['complete'])

    def test_shorter_issues_leave_room_for_a_longer_body(self):
        from backend.services.agent.documents import build_document
        state = ledger()
        state.stages[1].stage['issues_list'][0]['answer_evidence']['excerpts'].append(
            {'field': 'note:99', 'text': '추가 확인 기록입니다. ' * 60})
        _, review, _ = build_document(state, 'handoff', QUESTION, generate_supported, max_context_chars=2400)
        self.assertEqual(review['incompleteBodyIds'], [])
        self.assertTrue(review['complete'])

    def test_batch_detail_reads_only_provenance_checked_ids_and_keeps_each_issue(self):
        from backend.tools.catalog import agent_registry
        from backend.tools.conditions import project_conditions
        from backend.tools.registry import ToolContext
        source = FakeSources(lookups={r['id']: r for r in RAW})
        with agent_environment(sources=source):
            c = context()
            tool_context = ToolContext(QUESTION, QUESTION, c.enabled_tools,
                {s: project_conditions(c.intent_plan, c.query_constraints, s) for s in ('mantis', 'notion')},
                frozenset(), ledger(read=()), lambda: None, lambda n: n, 20, 20000)
            tools = agent_registry(query_tools=True)
            bad = tools.execute('get_mantis_issue_details', {'issue_ids': [10101, 99999]}, '', tool_context)
            self.assertFalse(bad['ok'])
            self.assertEqual(bad['error']['code'], 'not_in_scope')
            self.assertEqual(source.lookup_calls, [])
            good = tools.execute('get_mantis_issue_details', {'issue_ids': [10101, 20202]}, '', tool_context)
        self.assertTrue(good['ok'], good)
        self.assertEqual([r['id'] for r in good['results']], [10101, 20202])
        from backend.services.agent.documents import build_document
        _, review, _ = build_document(tool_context.ledger, 'handoff', QUESTION, generate_supported)
        self.assertTrue(review['complete'])
        self.assertEqual(len(source.lookup_calls), 2)

    def test_full_chat_keeps_verified_filters_document_and_audit(self):
        from backend.services.chat_service import ChatService
        from backend.audit import AUDIT_DB_PATH
        from test_unified_planner import proposal
        import sqlite3
        start, end = (date.today() - timedelta(days=55)).isoformat(), date.today().isoformat()
        clauses = [FILTER, {'field': 'created_at', 'operator': 'between', 'value': [start, end]}]
        proposed = proposal(operation='report', report_kind='handoff', scope='team', terms=[],
            status={'mode': 'unspecified', 'values': [], 'evidence': ''},
            filters=[{**clauses[0], 'evidence': '화면번호 0000'}, {**clauses[1], 'evidence': '최근 8주'}])
        fake = DocumentModel([turn(tool_call('list_mantis_issues', {'filters': clauses, 'scope': 'team', 'limit': 20})),
                              turn(*(tool_call('get_mantis_issue_detail', {'issue_id': r['id']}) for r in RAW)),
                              final('본문을 확인했습니다.')], planner=proposed)
        sources = FakeSources(lookups={r['id']: r for r in RAW})
        with (agent_environment(model=fake, sources=sources, extra_env={'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=snapshot()) as search):
            response = ChatService().build_response(chat_request(QUESTION, tools={'mantis': True, 'notion': False, 'vectorDb': False}),
                                                    user={'uid': 'document-integration'})
        self.assertEqual(response['raw']['mode'], 'agent_issue_document', response['content'])
        self.assertEqual(response['verification']['delivery'], 'allowed')
        self.assertTrue(response['documentReview']['complete'])
        # 적용 조건 줄은 연산자 이름·JSON 따옴표 없이 사람이 읽는 문구로 보인다.
        condition_line = response['content'].splitlines()[0]
        self.assertIn('화면번호 0000', condition_line)
        self.assertIn(f'등록 기간 {start} ~ {end}', condition_line)
        self.assertNotRegex(condition_line, r'equals|between|&quot;|"')
        self.assertEqual(search.call_args.args[1]['screen_numbers'], ['0000'])
        self.assertEqual(search.call_args.args[1]['created_at'], {'from': start, 'to': end})
        with sqlite3.connect(AUDIT_DB_PATH) as db:
            audit = json.loads(db.execute('select payload from validation_logs where id=?', (response['auditId'],)).fetchone()[0])
        self.assertEqual(audit['queryPlan']['report_kind'], 'handoff')
        self.assertEqual(audit['agent']['document']['summarizedIssueIds'], [10101, 20202])

    def test_condition_line_reads_as_plain_korean(self):
        from backend.services.agent.orchestrator import _condition_text
        text = _condition_text({'filter_clauses': [
            FILTER,
            {'field': 'created_at', 'operator': 'between', 'value': ['2026-10-01', '2026-11-06']},
            {'field': 'customer', 'operator': 'not_in', 'value': ['신한투자증권', 'KB증권']},
            {'field': 'due_at', 'operator': 'lte', 'value': '2026-11-30'},
            {'field': 'title', 'operator': 'contains', 'value': '<b>차트</b>'},
        ], 'statuses': ['new']})
        self.assertEqual(text, '화면번호 0000 · 등록 기간 2026-10-01 ~ 2026-11-06 · 고객사 신한투자증권, KB증권 제외 · '
                               '목표일 2026-11-30 이전 · 제목에 <b>차트</b> 포함 · 상태 new')
        self.assertEqual(_condition_text({}), '없음')

    def test_document_generation_and_review_share_reserved_model_budget(self):
        from backend.services.agent.orchestrator import run_agent_investigation
        fake = DocumentModel([turn(tool_call('list_mantis_issues', {'limit': 20})),
                              turn(*(tool_call('get_mantis_issue_detail', {'issue_id': r['id']}) for r in RAW))])
        sources = FakeSources(lookups={r['id']: r for r in RAW})
        with (agent_environment(model=fake, sources=sources, extra_env={'AGENT_MAX_MODEL_REQUESTS': '4'}),
              request_transfer_scope('company-test') as policy,
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=snapshot())):
            result = run_agent_investigation(chat_request(QUESTION), context(), policy)
        self.assertEqual(len(fake.calls), 4)
        self.assertEqual(result['agentReview']['modelRequests'], 4)
        self.assertEqual(result['agentReview']['limits']['reserveCalls'], 2)
        self.assertEqual(result['agentReview']['limits']['reserveSeconds'], 45)
        self.assertTrue(result['documentReview']['complete'])
        self.assertEqual({t['step'] for t in result['agentReview']['trace'] if t['step'].startswith('document')},
                         {'document_draft', 'document_review'})

    def test_title_only_read_does_not_authorize_document_facts(self):
        from backend.services.agent.documents import build_document
        state = ledger()
        for entry in state.stages:
            meta = entry.stage['meta']
            if 'detail_read_id' in meta:
                meta['title_lookup_id'] = meta.pop('detail_read_id')
        with patch(__name__ + '.generate_supported', side_effect=AssertionError('title only')) as gen:
            content, review, _ = build_document(state, 'handoff', QUESTION, gen)
        self.assertEqual(review['readIssueIds'], [])
        self.assertFalse(review['complete'])
        self.assertNotIn('차트가 멈춥니다', content)

    def test_reading_one_different_issue_cannot_complete_the_listed_set(self):
        from backend.services.agent.documents import build_document
        state = ledger(total=1, read=(20202,))
        _, review, _ = build_document(state, 'handoff', QUESTION, generate_supported)
        self.assertEqual(review['totalCount'], 1)
        self.assertEqual(review['summarizedIssueIds'], [20202])
        self.assertFalse(review['complete'])

    def test_truncated_body_or_context_cannot_be_labelled_complete(self):
        from backend.services.agent.documents import build_document
        state = ledger()
        state.stages[-1].stage['issues_list'][0]['answer_evidence']['complete'] = False
        _, review, _ = build_document(state, 'handoff', QUESTION, generate_supported)
        self.assertFalse(review['complete'])
        self.assertEqual(review['incompleteBodyIds'], [20202])
        state = ledger()
        for entry in state.stages[1:]:
            entry.stage['issues_list'][0]['answer_evidence']['excerpts'].append(
                {'field': 'note:99', 'text': '긴 기록입니다. ' * 300})
        _, review, _ = build_document(state, 'handoff', QUESTION, generate_supported, max_context_chars=800)
        self.assertFalse(review['complete'])

    def test_document_agent_is_off_without_policy_and_never_handles_writes(self):
        from backend.services.agent.route import run_agent_if_eligible
        with agent_environment(enabled=False), request_transfer_scope('company-test'):
            self.assertIsNone(run_agent_if_eligible(chat_request(QUESTION), context()))
        with agent_environment(), request_transfer_scope('company-test'):
            c = context()
            c.intent_plan['operation'] = 'write'
            self.assertIsNone(run_agent_if_eligible(chat_request(QUESTION), c))

    def test_screenshot_plan_preserves_screen_and_exact_eight_week_period(self):
        from backend.intent.unified_validation import validate_plan
        from backend.intent.execution_contract import validate_execution_contract
        from test_unified_planner import proposal
        end = date.today()
        start = end - timedelta(days=55)
        proposal_value = proposal(operation='report', report_kind='handoff', scope='team', terms=[],
            status={'mode': 'unspecified', 'values': [], 'evidence': ''}, filters=[
                {**FILTER, 'evidence': '화면번호 0000'},
                {'field': 'created_at', 'operator': 'between', 'value': [start.isoformat(), end.isoformat()],
                 'evidence': '최근 8주'}])
        plan = validate_plan(QUESTION, proposal_value, {}, {})
        self.assertEqual(plan['resolved_constraints']['screen_numbers'], ['0000'])
        self.assertEqual(plan['resolved_constraints']['issue_ids'], [])
        self.assertTrue(validate_execution_contract(QUESTION, plan)['valid'])
        for bad in ('wrong_period', 'missing_period', 'presentation_terms'):
            value = deepcopy(proposal_value)
            if bad == 'wrong_period':
                value['filters'][1]['value'][0] = (start - timedelta(days=10)).isoformat()
            elif bad == 'missing_period':
                value['filters'].pop()
            else:
                value['terms'] = ['증상', '고객사', '재현 조건']
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_plan(QUESTION, value, {}, {})

    def test_document_enters_agent_before_legacy_single_issue_builder(self):
        from backend.services.chat_service import ChatService
        fake = FakeModel([turn(tool_call('list_mantis_issues', {'limit': 20})),
                          turn(*(tool_call('get_mantis_issue_detail', {'issue_id': r['id']}) for r in RAW)),
                          final('확인한 원문으로 작성합니다.')])
        sources = FakeSources(lookups={r['id']: r for r in RAW})
        with (agent_environment(model=fake, sources=sources), request_transfer_scope('company-test'),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=snapshot()),
              patch('backend.services.agent.orchestrator._generate_document', side_effect=lambda r, s, d: generate_supported(s, d)),
              patch('backend.services.chat_service.plan_chat_query', side_effect=AssertionError('legacy intercepted'))):
            result = ChatService()._complete_response(chat_request(QUESTION), context(), None, lambda r: r)
        self.assertEqual(result['raw']['mode'], 'agent_issue_document')
        self.assertEqual(result['documentReview']['readIssueIds'], [10101, 20202])
        self.assertIn('종목을 바꾸면', result['content'])
        self.assertIn('재접속하면', result['content'])
        self.assertNotIn('단일 이슈 번호', result['content'])

    def test_documents_require_enabled_sources_and_keep_other_report_routes(self):
        from backend.services.agent.route import agent_route_reason
        with agent_environment(), request_transfer_scope('company-test'):
            for kind in ('handoff', 'postmortem', 'notice'):
                self.assertEqual(agent_route_reason(context(kind)), '')
            self.assertEqual(agent_route_reason(context('weekly')), '')
            c = context('profile')
            self.assertEqual(agent_route_reason(c), '')
            c = context()
            c.enabled_tools['mantis'] = False
            self.assertEqual(agent_route_reason(c), 'source_disabled')

    def test_document_only_uses_read_bodies_and_reports_partial_coverage(self):
        from backend.services.agent.documents import build_document
        content, review, refs = build_document(ledger(read=(10101,)), 'handoff', QUESTION, generate_supported)
        self.assertEqual(review['totalCount'], 2)
        self.assertEqual(review['readIssueIds'], [10101])
        self.assertFalse(review['complete'])
        self.assertIn('일부', content)
        self.assertIn('차트가 멈춥니다', content)
        self.assertNotIn('재접속하면', content)
        self.assertEqual(refs, ['M#10101'])

    def test_complete_zero_and_unfinished_zero_are_distinct(self):
        from backend.services.agent.documents import build_document
        for complete in (True, False):
            with self.subTest(complete=complete):
                with patch(__name__ + '.generate_supported', side_effect=AssertionError('no evidence generation')) as gen:
                    content, review, _ = build_document(ledger(total=0, complete=complete, read=()),
                                                        'handoff', QUESTION, gen)
                self.assertEqual(review['searchComplete'], complete)
                self.assertEqual(review['totalCount'], 0 if complete else None)
                self.assertEqual('일치하는 이슈는 0건' in content, complete)
                self.assertIn('전체 조회' if complete else '확정하지 못', content)

    def test_cross_issue_evidence_and_failed_independent_review_cannot_be_published(self):
        from backend.services.agent.documents import build_document
        for bad_reference in (True, False):
            def generate(system, data, bad_reference=bad_reference):
                if 'claims' in data:
                    return {'supported': {c['id']: False for c in data['claims']}}
                draft = generate_supported(system, data)
                if bad_reference:
                    first, second = draft['issues']
                    first['sections'][0]['claims'][0]['evidence_ids'] = second['sections'][0]['claims'][0]['evidence_ids']
                return draft
            content, review, _ = build_document(ledger(), 'handoff', QUESTION, generate)
            self.assertEqual(review['summarizedIssueIds'], [])
            self.assertNotIn('차트가 멈춥니다', content)
            self.assertFalse(review['complete'])

    def test_customer_notice_keeps_internal_identifiers_out_of_text(self):
        from backend.services.agent.documents import build_document
        content, _, refs = build_document(ledger(), 'notice', '고객 안내문으로 작성해줘', generate_supported)
        for internal in ('10101', '20202', '0000', '[M#', '[0000]'):
            self.assertNotIn(internal, content)
        self.assertEqual(refs, ['M#10101', 'M#20202'])

