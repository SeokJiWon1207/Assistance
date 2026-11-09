"""검증된 항목을 보존하는 공통 보정 거래와 경로별 회귀."""
from copy import deepcopy
from threading import Event
from time import monotonic
import unittest

from backend.execution import ExecutionBudget, RequestStopped, execution_scope


class RepairTransactionTests(unittest.TestCase):
    def test_repairs_only_failed_ids_and_rechecks_the_combination(self):
        from backend.evidence.repair import Failure, repair_items
        original = {'good': {'text': '확인된 증상', 'count': 5}}
        audit = {}
        seen = []
        def validate(items):
            seen.append(deepcopy(items))
            return {k: v for k, v in items.items() if v['count'] == 3}
        result = repair_items(original, {'bad': Failure('numeric', 'wrong_count')},
            propose=lambda targets: {'good': {'text': '덮어쓰기', 'count': 999},
                                     'bad': {'text': '정보 부족', 'count': 3}, 'extra': {'count': 9}},
            validate=validate, revalidate=lambda items: items['good'] == original['good'] and items['bad']['count'] == 3,
            can_attempt=lambda: True, audit=audit)
        self.assertEqual(result, {**original, 'bad': {'text': '정보 부족', 'count': 3}})
        self.assertEqual(list(seen[0]), ['bad'])
        self.assertEqual(audit['ignored'], ['good', 'extra'])
        self.assertEqual(audit['repaired'], ['bad'])
        result['good']['count'] = 100
        self.assertEqual(original['good']['count'], 5)

    def test_missing_evidence_and_policy_are_not_rewritten(self):
        from backend.evidence.repair import Failure, repair_items
        for kind in ('missing_evidence', 'permission', 'cancelled', 'condition'):
            with self.subTest(kind=kind):
                audit = {}
                result = repair_items({'good': '보존'}, {'bad': Failure(kind, kind)},
                    propose=lambda _: self.fail('보정하면 안 됨'), validate=lambda x: x,
                    revalidate=lambda x: True, can_attempt=lambda: True, audit=audit)
                self.assertEqual(result, {'good': '보존'})
                self.assertFalse(audit['attempted'])
                self.assertEqual(audit['unresolved'], ['bad'])

    def test_failed_revalidation_keeps_verified_items_and_does_not_retry(self):
        from backend.evidence.repair import Failure, repair_items
        calls = []
        def propose(targets):
            calls.append(targets)
            return {'bad': '후보'}
        audit = {}
        result = repair_items({'good': '보존'}, {'bad': Failure('expression', 'meaning')},
            propose=propose, validate=lambda x: x, revalidate=lambda _: False,
            can_attempt=lambda: True, audit=audit)
        self.assertEqual(result, {'good': '보존'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(audit['status'], 'combination_rejected')

    def test_cancelled_after_review_does_not_publish_repair(self):
        from backend.evidence.repair import Failure, repair_items
        budget = ExecutionBudget(monotonic() + 10, Event())
        audit = {}
        def validate(items):
            budget.cancel()
            return items
        with self.assertRaises(RequestStopped), execution_scope(budget):
            repair_items({'good': '보존'}, {'bad': Failure('citation', 'unknown_ref')},
                propose=lambda _: {'bad': '후보'}, validate=validate, revalidate=lambda _: True,
                can_attempt=lambda: True, audit=audit)
        self.assertEqual(audit['repaired'], [])

    def test_no_remaining_budget_or_policy_stop_cannot_start_repair(self):
        from backend.evidence.repair import Failure, repair_items
        for available, reason in ((False, ''), (True, 'policy_blocked'), (True, 'cancelled'), (True, 'serving_changed')):
            audit = {}
            self.assertEqual(repair_items({'good': '보존'}, {'bad': Failure('citation', 'unknown_ref')},
                propose=lambda _: self.fail('추가 호출 금지'), validate=lambda x: x, revalidate=lambda _: True,
                can_attempt=lambda available=available: available, stop_reason=reason, audit=audit), {'good': '보존'})
            self.assertFalse(audit['attempted'])


class CommonRepairIntegrationTests(unittest.TestCase):
    def test_real_harness_charges_repair_and_review_and_delivers_only_validated_patch(self):
        import json
        from agent_harness_cases import FakeModel, FakeSources, agent_environment, explain_context, final, notion_page, tool_call, turn
        from test_agent_harness import run
        from backend.evaluation.verification import build_verification_report
        from backend.evaluation.delivery import decide_delivery
        question = '배포 가이드의 롤백 절차를 설명해줘'
        page = notion_page('page-roll', '배포 가이드', '설정을 확인합니다. 이전 버전으로 되돌립니다.')
        good = '설정을 확인합니다.'
        model = FakeModel([turn(tool_call('search_notion_pages', {'query': '롤백'})),
                           final(good + ' [N1]\n이전 버전으로 되돌립니다. [N8]')],
            finals=[final(json.dumps({'repairs': {'c0': '확인한 사실을 변경합니다. [N1]',
                                                 'c1': '이전 버전으로 되돌립니다. [N1]'}})),
                    final(json.dumps({'supported': {'c1': True}}))])
        with agent_environment(model=model, sources=FakeSources(notion={'롤백': [page]})):
            response = run(explain_context(question, source='notion'))
        self.assertIn(good, response['content'])
        self.assertNotIn('변경합니다', response['content'])
        self.assertIn('이전 버전으로 되돌립니다', response['content'])
        self.assertEqual(response['agentReview']['modelRequests'], 4)
        self.assertEqual(response['agentReview']['usage']['reportedCalls'], 4)
        self.assertEqual(response['agentReview']['repairs'][0]['ignored'], ['c0'])
        report = build_verification_report(question, response['content'], response['sourcesForFollowUps'],
                                            response['retrieval'], response['raw']['mode'])
        self.assertTrue(decide_delivery(report).allowed)

    def test_policy_denial_in_document_does_not_try_other_batches_or_repair(self):
        from backend.llm import ModelTransferBlocked
        from backend.services.agent.documents import build_document
        from test_agent_document_batches import many_issues
        calls = []
        def generate(*args):
            calls.append('call')
            raise ModelTransferBlocked('denied')
        with self.assertRaises(ModelTransferBlocked):
            build_document(many_issues(8), 'handoff', '증상 요약', generate, can_repair=lambda: True)
        self.assertEqual(calls, ['call'])

    def test_grounded_repair_respects_reserved_review_budget(self):
        from test_explanation_narrative import NarrativeRepairTests
        from backend.services.explanation_narrative import repair_summary
        fixture = NarrativeRepairTests(); fixture.setUp()
        items, meta = repair_summary(fixture.request, fixture.context, fixture.validated, fixture.grounding,
            lambda *_: self.fail('검토 예산 없는 초안 금지'), can_repair=lambda: False)
        self.assertEqual(items, [])
        self.assertFalse(meta['repair']['attempted'])

    def test_grounded_summary_repairs_only_bad_item_and_preserves_good_evidence(self):
        import json
        from test_explanation_narrative import NarrativeRepairTests, reply
        from backend.services.explanation_narrative import repair_summary
        fixture = NarrativeRepairTests(); fixture.setUp()
        good = deepcopy(fixture.validated['answer_items'][0])
        good['draft_answer'] = '캐시 덮어쓰기 로직을 수정했습니다.'
        fixture.validated['answer_items'].insert(0, good)
        original = deepcopy(fixture.validated)
        seen = []
        def generate(request, model, messages, *args):
            seen.append(messages[-1]['content'])
            return reply({'answers': ['캐시 로직을 수정했습니다.']} if len(seen) == 1 else {'supported': [True]})
        items, meta = repair_summary(fixture.request, fixture.context, fixture.validated, fixture.grounding, generate)
        self.assertEqual([i['answer'] for i in items], [good['draft_answer'], '캐시 로직을 수정했습니다.'])
        self.assertEqual(items[0]['evidence'], good['evidence'])
        self.assertEqual(len(json.loads(seen[0])['items']), 1)
        self.assertEqual(fixture.validated, original)
        self.assertEqual(meta['repair']['preserved'], ['s0'])

    def test_grounded_summary_preserves_good_item_when_other_body_is_missing(self):
        from test_explanation_narrative import NarrativeRepairTests
        from backend.services.explanation_narrative import repair_summary
        fixture = NarrativeRepairTests(); fixture.setUp()
        good = fixture.validated['answer_items'][0]
        good['draft_answer'] = '캐시 덮어쓰기 로직을 수정했습니다.'
        fixture.validated['answer_items'].append({'question_part': '재현', 'unanswered': False,
                                                  'evidence': [], 'draft_answer': '재현됐습니다.'})
        items, meta = repair_summary(fixture.request, fixture.context, fixture.validated, fixture.grounding,
                                     lambda *_: self.fail('근거 부족은 재작성 금지'))
        self.assertEqual(items[0]['answer'], good['draft_answer'])
        self.assertIn('확인하지 못했습니다', items[1]['answer'])
        self.assertFalse(meta['repair']['attempted'])
        self.assertEqual(meta['status'], 'partial')

    def test_aggregation_repairs_only_failed_fact_and_keeps_verified_sentence(self):
        from backend.reports.aggregation_narrative import build_facts, summarize_aggregation
        from test_aggregation_narrative import AggregationNarrativeTests
        result = AggregationNarrativeTests().daily_result()
        facts = build_facts(result)
        a = next(f for f in facts if f['group'] == ['고객사A'])
        b = next(f for f in facts if f['group'] == ['고객사B'])
        good = {'fact_ids': [a['id']], 'text': '2026-11-02 고객사A 등록 건수는 1건입니다.'}
        bad = {'fact_ids': [b['id']], 'text': '2026-11-02 고객사B 등록 건수는 999건입니다.'}
        fixed = {**bad, 'text': '2026-11-02 고객사B 등록 건수는 2건입니다.'}
        calls = []
        def chat(model, key, system, data, **kwargs):
            calls.append(data)
            if data.get('repair'):
                return {'repairs': {'c0': {**good, 'text': '바꾼 정상 문장'}, 'c1': fixed}}
            return {'supported': [True] * len(data['claims'])} if 'claims' in data else {'claims': [good, bad]}
        text, meta = summarize_aggregation('고객사별 집계 설명', result, {'id': 'test'}, 'key',
                                           chat=chat, can_repair=lambda: True)
        self.assertEqual(meta['claims'], [good, fixed])
        self.assertIn(good['text'], text)
        self.assertNotIn('999', text)
        self.assertEqual(meta['repair']['ignored'], ['c0'])
        self.assertEqual(meta['calls'], 4)
        self.assertTrue(all(len(c['claims']) == 1 for c in calls if 'claims' in c))

    def test_aggregation_repair_cannot_change_the_fact_binding_or_hide_wrong_number(self):
        from backend.reports.aggregation_narrative import build_facts, summarize_aggregation
        from test_aggregation_narrative import AggregationNarrativeTests
        result = AggregationNarrativeTests().daily_result()
        a, b = [f for f in build_facts(result) if f['group']]
        good = {'fact_ids': [a['id']], 'text': a['text']}
        bad = {'fact_ids': [b['id']], 'text': b['text'] + ' 999건'}
        for patch_value in (good, bad):
            def chat(model, key, system, data, patch_value=patch_value, **kwargs):
                if data.get('repair'): return {'repairs': {'c1': patch_value}}
                return {'supported': [True]} if 'claims' in data else {'claims': [good, bad]}
            text, meta = summarize_aggregation('집계 설명', result, {'id': 'test'}, 'key',
                                               chat=chat, can_repair=lambda: True)
            self.assertEqual(meta['claims'], [good])
            self.assertNotIn('999', text)
            self.assertEqual(meta['repair']['unresolved'], ['c1'])

    def test_citation_repair_keeps_good_text_and_supplies_body_to_independent_review(self):
        from backend.services.agent.answer_repair import repair_answer
        good = '접속 설정을 확인합니다. [N1]'
        bad = '재접속을 안내합니다. [N8]'
        catalog = {'N1': {'type': 'notion', 'id': 'one', 'grade': 'scoped', 'title': '가이드'}}
        evidence = {'N1': '접속 설정을 확인합니다. 재접속을 안내합니다.'}
        seen = []
        def chat(system, data, kind):
            seen.append((kind, data))
            if kind == 'answer_repair':
                return {'repairs': {'c0': '모든 설정을 삭제합니다. [N1]', 'c1': '재접속을 안내합니다. [N1]'}}
            return {'supported': {'c1': True}}
        text, report, meta = repair_answer(good + '\n' + bad, catalog, set(), evidence,
                                          chat=chat, can_repair=lambda: True)
        self.assertEqual(text, good + '\n재접속을 안내합니다. [N1]')
        self.assertTrue(report.ok)
        self.assertEqual(meta['repair']['ignored'], ['c0'])
        self.assertEqual([k for k, _ in seen], ['answer_repair', 'answer_repair_review'])
        self.assertEqual(seen[0][1]['evidence']['N1'], evidence['N1'])

    def test_repair_does_not_accept_fabricated_numbers_or_failed_semantic_review(self):
        from backend.services.agent.answer_repair import repair_answer
        catalog = {'N1': {'type': 'notion', 'id': 'one', 'grade': 'scoped'}}
        for fixed, verdict in (('999초를 기다립니다. [N1]', True), ('10초 뒤 해결됩니다. [N1]', False)):
            seen = []
            def chat(system, data, kind, seen=seen, fixed=fixed, verdict=verdict):
                seen.append(kind)
                return {'repairs': {'c1': fixed}} if kind == 'answer_repair' else {'supported': {'c1': verdict}}
            text, report, meta = repair_answer('접속합니다. [N1]\n999초 뒤 해결됩니다. [N1]', catalog, set(),
                {'N1': '접속합니다. 10초 뒤 확인합니다.'}, chat=chat, can_repair=lambda: True)
            self.assertEqual(text, '접속합니다. [N1]')
            self.assertTrue(report.ok)
            self.assertEqual(meta['removed'], 1)
            self.assertEqual(meta['repair']['failures'][0]['kind'], 'numeric')
            self.assertEqual(len(seen), 1 if '999' in fixed else 2)

    def test_missing_current_body_cannot_be_repaired_from_title(self):
        from backend.services.agent.answer_repair import repair_answer
        text, _, meta = repair_answer('서버를 다시 시작합니다. [N1]', {}, set(), {},
            chat=lambda *_: self.fail('본문 없음'), can_repair=lambda: True)
        self.assertEqual(text, '')
        self.assertFalse(meta['repair']['attempted'])
        self.assertEqual(meta['repair']['failures'][0]['kind'], 'missing_evidence')

    def test_document_keeps_accepted_claim_in_the_same_repaired_slot(self):
        from backend.services.agent.documents import build_document
        from test_agent_document_batches import ledger, generate_supported
        state = ledger()
        good = '종목을 바꾸면 차트가 멈춥니다.'
        fixed = '차트가 멈추는 증상이 보고됐습니다.'
        def generate(system, data):
            if 'claims' in data:
                return generate_supported(system, data)
            value = generate_supported(system, data)
            section = value['issues'][0]['sections'][0]
            if data.get('correction'):
                section['claims'] = [{'text': fixed, 'evidence_ids': section['claims'][0]['evidence_ids']}]
            else:
                section['claims'].append({'text': '999초간 멈춥니다.', 'evidence_ids': section['claims'][0]['evidence_ids']})
            return value
        content, meta, _ = build_document(state, 'handoff', '증상을 요약해줘', generate, can_repair=lambda: True)
        self.assertIn(good, content)
        self.assertIn(fixed, content)
        self.assertNotIn('999', content)
        self.assertTrue(meta['repair']['attempted'])


if __name__ == '__main__':
    unittest.main()
