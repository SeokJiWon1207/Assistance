"""대량 문서 분할·고객용 보정도 기존 근거·검토·호출 한도 안에서 처리한다."""
import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from agent_harness_cases import FakeSources, agent_environment, chat_request, final, tool_call, turn
from test_agent_document_matrix import MatrixModel, QUESTIONS, decisions, plan, rows, stage
from test_agent_documents import DocumentModel, QUESTION, RAW, context, generate_supported, ledger, snapshot


def many_issues(count=20):
    from backend.sources.mantis_fields import normalize_issue
    from backend.tools.ledger import EvidenceLedger
    state = EvidenceLedger()
    raw = rows(count)
    listing = stage(raw)
    listing['meta']['query_result'] = {'operation': 'list', 'total_count': count, 'observed_count': count,
                                      'search_complete': True, 'ordered_ids': [r['id'] for r in raw]}
    state.add('mantis', listing, grade='scoped', unapplied=(), call_id='list')
    for item in raw:
        state.add('mantis', {'source': 'mantis_explanation_focus', 'status': 'ok',
                            'issues_list': [normalize_issue(item, evidence_query=QUESTIONS['postmortem'])],
                            'meta': {'detail_read_id': item['id']}},
                  grade='scoped', unapplied=(), call_id='detail')
    return state


class AgentDocumentBatchTests(unittest.TestCase):
    def test_writer_gets_completion_evidence_from_the_unchanged_validator(self):
        from backend.services.agent.documents import build_document
        state = ledger()
        state.stages[1].stage['issues_list'][0]['answer_evidence']['excerpts'].extend([
            {'field': 'note:8', 'text': '수정할 예정입니다.'},
            {'field': 'note:9', 'text': '수정했습니다.'},
            {'field': 'note:10', 'text': '2026-11-05 메일 회신 To. 고객\n확인 내용을 안내드립니다.'}])
        writes = []

        def generate(system, data):
            if 'issues' in data: writes.append(data)
            return generate_supported(system, data)

        build_document(state, 'notice', '증상과 조치를 안내해줘', generate)
        support = writes[0]['issues'][0]['completion_evidence']
        self.assertNotIn('M10101:p3', support['implementation'])
        self.assertIn('M10101:p4', support['implementation'])
        self.assertIn('M10101:p5', support['reply'])
        self.assertEqual(support['production'], [])

    def test_incomplete_body_does_not_spend_the_second_repair_batch_on_repeated_reads(self):
        from backend.services.chat_service import ChatService
        raw = rows(17)

        class TwoRepairs(DocumentModel):
            def grounded(self, payload):
                if payload['messages'][0]['content'].startswith('사용자 질문 전체를 사내 조회 실행 계획 JSON'):
                    return super().grounded(payload)
                data = json.loads(payload['messages'][-1]['content'])
                value = generate_supported('', data)
                if 'claims' in data:
                    value['supported'] = {c['id']: 'r' in c['id'] or c['issue_id'] > 41008 for c in data['claims']}
                return self.respond(final(json.dumps(value, ensure_ascii=False)))

        fake = TwoRepairs([*decisions(raw)[:-1],
                           *[turn(tool_call('get_mantis_issue_detail', {'issue_id': 41001})) for _ in range(20)]],
                          planner=plan('notice'))
        source = FakeSources(lookups={r['id']: r for r in raw})
        lookup = source.lookup

        def incomplete(ids, constraints=None):
            result = lookup(ids, constraints)
            for issue in result['issues_list']:
                if issue['id'] == 41001: issue['answer_evidence']['complete'] = False
            return result

        source.lookup = incomplete
        with (agent_environment(model=fake, sources=source, extra_env={'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage(raw))):
            response = ChatService().build_response(chat_request(QUESTIONS['notice']), user={'uid': 'repair-budget'})
        self.assertEqual(response['documentReview']['summarizedIssueIds'], list(range(41001, 41018)))
        self.assertEqual(response['documentReview']['unresolvedIssueIds'], [])
        self.assertEqual(response['documentReview']['incompleteBodyIds'], [41001])
        self.assertFalse(response['documentReview']['complete'])
        self.assertLessEqual(response['agentReview']['modelRequests'], 16)

    def test_repair_changes_only_rejected_slots_and_keeps_the_verified_symptom(self):
        from backend.services.agent.documents import build_document
        state = ledger()
        for entry in state.stages[1:]:
            entry.stage['issues_list'][0]['answer_evidence']['excerpts'].append(
                {'field': 'note:7', 'text': '요청을 접수했습니다.'})
        writes, reviewed = [], []

        def generate(system, data):
            if 'claims' in data:
                reviewed.extend(data['claims'])
                return generate_supported(system, data)
            writes.append(data)
            value = generate_supported(system, data)
            issue = value['issues'][0]
            if data.get('correction'):
                issue['sections'] = [
                    {'slot': 'symptoms', 'claims': [{'text': '응답 시간이 999초입니다.', 'evidence_ids': ['M10101:p2']}]},
                    {'slot': 'actions', 'claims': [{'text': '요청을 접수했습니다.', 'evidence_ids': ['M10101:p3']}]}]
            else:
                issue['sections'].append({'slot': 'actions', 'claims': [
                    {'text': 'API를 수정했습니다.', 'evidence_ids': ['M10101:p2']}]})
            return value

        content, meta, _ = build_document(state, 'notice', '증상과 조치를 안내해줘', generate, can_repair=lambda: True)
        self.assertTrue(meta['complete'])
        self.assertEqual(meta['unresolvedIssueIds'], [])
        self.assertEqual(writes[-1]['correction']['required_slots'], [{'issue_id': 10101, 'slots': ['actions']}])
        self.assertIn('종목을 바꾸면 차트가 멈춥니다.', content)
        self.assertNotIn('999', content)
        self.assertFalse(any('999' in c['text'] for c in reviewed))
        self.assertEqual(meta['ignoredRepairSlots'], [{'issue_id': 10101, 'slot': 'symptoms'}])

    def test_draft_schema_limits_customer_slots_and_adapts_to_the_actual_fallback(self):
        from types import SimpleNamespace
        from backend.services.agent.document_format import draft_format
        from backend.services.agent import orchestrator as agent
        from backend.services.agent.policy import AgentSettings
        schema = draft_format('notice')
        section = schema['json_schema']['schema']['properties']['issues']['items']['properties']['sections']['items']
        self.assertNotIn('reproduction', section['properties']['slot']['enum'])
        self.assertEqual(section['properties']['claims']['maxItems'], 1)
        self.assertTrue(schema['json_schema']['strict'])
        payload = {'response_format': schema, 'messages': []}
        primary = {'id': 'luna', 'provider': 'openai', 'endpoint': 'https://api.openai.com/v1/chat/completions'}
        backup = {'id': 'company', 'provider': 'openai-compatible', 'endpoint': 'https://company.test/v1/chat/completions'}
        state = SimpleNamespace(budget=agent.AgentBudget(8, 8, 2, 45), settings=AgentSettings(),
                                text_models=[primary, backup], failed_models={'decision': set(), 'text': set()}, trace=[], usage=[])
        with (patch.object(agent, '_post', side_effect=[(400, {}), (200, {'choices': [final('{"issues":[]}')]})]) as post,
              patch.object(agent, '_record_response')):
            result = agent._request_turn(state, payload, state.budget.final_seconds, 'document_draft', retries=0)
        self.assertEqual(result.kind, 'final')
        self.assertEqual(post.call_args_list[0].args[1]['response_format'], schema)
        self.assertEqual(post.call_args_list[1].args[1]['response_format'], {'type': 'json_object'})
        self.assertEqual(payload['response_format'], schema)

    def test_customer_correction_schema_only_allows_the_rejected_slots(self):
        from backend.services.agent.document_format import draft_format
        value = draft_format('notice', slots={'actions'})['json_schema']['schema']
        section = value['properties']['issues']['items']['properties']['sections']['items']
        self.assertEqual(section['properties']['slot']['enum'], ['actions'])
        self.assertEqual(section['properties']['claims']['maxItems'], 1)

    def test_twenty_issues_are_written_and_reviewed_in_bounded_batches(self):
        from backend.services.agent.documents import build_document
        writes, reviews = [], []

        def generate(system, data):
            if 'claims' in data:
                reviews.append(deepcopy(data))
            else:
                writes.append(deepcopy(data))
                if len(data['issues']) > 6:
                    raise ValueError('simulated output length limit')
            return generate_supported(system, data)

        _, meta, refs = build_document(many_issues(), 'postmortem', QUESTIONS['postmortem'], generate)
        self.assertTrue(meta['complete'])
        self.assertEqual(meta['summarizedIssueIds'], list(range(41001, 41021)))
        self.assertEqual(len(refs), 20)
        self.assertEqual([len(w['issues']) for w in writes], [6, 6, 6, 2])
        self.assertEqual(len(reviews), 4)
        identifiers = [c['id'] for review in reviews for c in review['claims']]
        self.assertEqual(len(identifiers), len(set(identifiers)))
        for write, review in zip(writes, reviews, strict=True):
            self.assertEqual({i['issue_id'] for i in write['issues']}, {c['issue_id'] for c in review['claims']})

    def test_failed_middle_batch_keeps_verified_earlier_and_later_claims(self):
        from backend.services.agent.documents import build_document

        def generate(system, data):
            if 'issues' in data and any(41007 <= i['issue_id'] <= 41012 for i in data['issues']):
                raise ValueError('broken middle batch')
            return generate_supported(system, data)

        _, meta, _ = build_document(many_issues(13), 'postmortem', QUESTIONS['postmortem'], generate)
        self.assertEqual(meta['summarizedIssueIds'], [*range(41001, 41007), 41013])
        self.assertEqual(len(meta['readIssueIds']), 13)
        self.assertFalse(meta['complete'])
        self.assertEqual(meta['status'], 'partial')
        self.assertEqual(meta['failedBatches'][0]['issueIds'], list(range(41007, 41013)))

    def test_remaining_budget_is_checked_before_drafting_an_unreviewable_batch(self):
        from backend.services.agent.documents import build_document
        remaining = [6]
        generated = []

        def generate(system, data):
            remaining[0] -= 1
            generated.append('review' if 'claims' in data else 'draft')
            return generate_supported(system, data)

        _, meta, _ = build_document(many_issues(), 'postmortem', QUESTIONS['postmortem'], generate,
                                     can_generate=lambda: remaining[0] >= 2)
        self.assertEqual(generated, ['draft', 'review'] * 3)
        self.assertEqual(meta['summarizedIssueIds'], list(range(41001, 41019)))
        self.assertFalse(meta['complete'])
        self.assertEqual(len(meta['readIssueIds']), 20)

    def test_a_different_batch_cannot_supply_a_claims_evidence(self):
        from backend.services.agent.documents import build_document

        def generate(system, data):
            value = generate_supported(system, data)
            if 'issues' in value:
                for item in value['issues']:
                    if item['issue_id'] == 41007:
                        item['sections'][0]['claims'][0]['evidence_ids'] = ['M41001:p2']
            return value

        _, meta, _ = build_document(many_issues(7), 'postmortem', QUESTIONS['postmortem'], generate)
        self.assertEqual(meta['summarizedIssueIds'], list(range(41001, 41007)))
        self.assertFalse(meta['complete'])
        self.assertEqual(meta['rejectedIssueIds'], [41007])

    def test_notice_writer_and_repair_receive_the_existing_customer_rules(self):
        from backend.services.agent.documents import build_document
        from backend.reports.document_narrative import customer_safe
        state = ledger()
        for entry in state.stages[1:]:
            excerpts = entry.stage['issues_list'][0]['answer_evidence']['excerpts']
            next(p for p in excerpts if p['field'] == 'description')['text'] = 'Windows 11 26H2 호환 여부 테스트를 요청했습니다.'
        writes = []

        def generate(system, data):
            value = generate_supported(system, data)
            if 'issues' in value:
                writes.append(deepcopy(data))
                if data.get('correction'):
                    for issue in value['issues']:
                        issue['sections'][0]['claims'][0]['text'] = '해당 운영체제 환경에서 호환 여부 테스트를 요청했습니다.'
            return value

        content, meta, _ = build_document(state, 'notice', '고객 안내문으로 작성해줘', generate,
                                         can_repair=lambda: True)
        self.assertTrue(meta['complete'])
        self.assertEqual(writes[0]['options']['audience'], '고객')
        self.assertEqual(set(writes[0]['notice_rules']['allowed_latin_tokens']),
                         {'hts', 'mts', 'ios', 'android', 'aos', 'pc', 'app', 'ui', 'os', 'web'})
        self.assertIn('Windows', writes[1]['correction']['rejected_claims'][0]['blocked_tokens'])
        self.assertNotIn('Windows', content)
        self.assertFalse(customer_safe('Windows 11 26H2 호환 여부 테스트를 요청했습니다.', {}))
        self.assertEqual(meta['unresolvedIssueIds'], [])

    def test_malformed_draft_is_rewritten_once_with_a_review_call_reserved(self):
        from backend.services.agent.orchestrator import run_agent_investigation
        from backend.services.agent.policy import request_transfer_scope

        class InvalidDraft(DocumentModel):
            broken = False

            def grounded(self, payload):
                if payload['messages'][0]['content'].startswith('실제로 읽은 Mantis') and not self.broken:
                    self.broken = True
                    return self.respond(final('{"issues":['))
                return super().grounded(payload)

        for limit, expected in ((4, False), (6, True)):
            with self.subTest(limit=limit):
                fake = InvalidDraft([turn(tool_call('list_mantis_issues', {'limit': 20})),
                                     turn(tool_call('get_mantis_issue_details', {'issue_ids': [10101, 20202]}))])
                with (agent_environment(model=fake, sources=FakeSources(lookups={r['id']: r for r in RAW}),
                                        extra_env={'AGENT_MAX_MODEL_REQUESTS': str(limit)}),
                      request_transfer_scope('company-test') as policy,
                      patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=snapshot())):
                    response = run_agent_investigation(chat_request(QUESTION), context(), policy)
                self.assertEqual(response['documentReview']['complete'], expected)
                self.assertLessEqual(response['agentReview']['modelRequests'], limit)
                drafts = [c for c in fake.calls if not c['tools']]
                self.assertTrue(all(c['payload'].get('response_format') == {'type': 'json_object'} for c in drafts))
                if expected:
                    self.assertEqual(len(drafts), 3)
                    self.assertIn('document_draft_retry', [s['step'] for s in response['agentReview']['trace']])

    def test_incomplete_body_still_leaves_budget_for_every_batch_review(self):
        from backend.services.chat_service import ChatService
        raw = rows(20)
        fake = MatrixModel([*decisions(raw)[:-1],
                            *[turn(tool_call('get_mantis_issue_detail', {'issue_id': 41001})) for _ in range(20)]],
                           planner=plan('postmortem'))
        source = FakeSources(lookups={r['id']: r for r in raw})
        lookup = source.lookup

        def incomplete(ids, constraints=None):
            result = lookup(ids, constraints)
            for issue in result['issues_list']:
                if issue['id'] == 41001:
                    issue['answer_evidence']['complete'] = False
            return result

        source.lookup = incomplete
        with (agent_environment(model=fake, sources=source, extra_env={'UNIFIED_QUERY_PLANNER_ENABLED': 'true'}),
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=stage(raw))):
            response = ChatService().build_response(chat_request(QUESTIONS['postmortem']), user={'uid': 'batch-budget'})
        meta, agent = response['documentReview'], response['agentReview']
        self.assertEqual(meta['summarizedIssueIds'], list(range(41001, 41021)))
        self.assertEqual(meta['incompleteBodyIds'], [41001])
        self.assertFalse(meta['complete'])
        self.assertLessEqual(agent['modelRequests'], 16)
        self.assertGreaterEqual(sum(t['step'] == 'document_review' for t in agent['trace']), 4)

    def test_malformed_correction_is_rewritten_and_independently_reviewed(self):
        from backend.services.agent.orchestrator import run_agent_investigation
        from backend.services.agent.policy import request_transfer_scope

        class BrokenCorrection(DocumentModel):
            correction_broken = False

            def grounded(self, payload):
                data = json.loads(payload['messages'][-1]['content'])
                if 'claims' in data:
                    return super().grounded(payload)
                value = generate_supported('', data)
                if not data.get('correction'):
                    value['issues'][0]['sections'].append({'slot': 'reproduction', 'claims': [
                        {'text': 'API를 실행합니다.', 'evidence_ids': ['M10101:p2']}]})
                elif not self.correction_broken:
                    self.correction_broken = True
                    return self.respond(final('{"issues":['))
                else:
                    value['issues'][0]['sections'][0]['slot'] = 'reproduction'
                return self.respond(final(json.dumps(value, ensure_ascii=False)))

        fake = BrokenCorrection([turn(tool_call('list_mantis_issues', {'limit': 20})),
                                 turn(tool_call('get_mantis_issue_details', {'issue_ids': [10101, 20202]}))])
        with (agent_environment(model=fake, sources=FakeSources(lookups={r['id']: r for r in RAW})),
              request_transfer_scope('company-test') as policy,
              patch('backend.tools.mantis_queries.execute_mantis_snapshot_search', return_value=snapshot())):
            response = run_agent_investigation(chat_request(QUESTION), context(), policy)
        meta = response['documentReview']
        self.assertTrue(meta['complete'])
        self.assertEqual(meta['unresolvedIssueIds'], [])
        self.assertNotIn('API를 실행', response['content'])
        self.assertEqual({c['slot'] for c in meta['claims'] if c['issue_id'] == 10101}, {'symptoms', 'reproduction'})
        self.assertEqual(sum(t['step'] == 'document_review' for t in response['agentReview']['trace']), 2)
