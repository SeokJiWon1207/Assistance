"""추천에 표시한 식별자가 기존 채팅의 현재 원문 조회까지 이어져야 한다."""
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from backend.schemas import ChatRequest, ChatMessage
from backend.services.chat import prepare, planning
from backend.services.retrieval_pipeline import run_retrieval_pipeline
from backend.suggestions.contracts import EvidenceBatch
from backend.suggestions.ranking import build_suggestions
from test_suggestion_ranking import candidate


class SuggestionFlowTests(unittest.TestCase):
    def prompt(self, combined):
        m = candidate(identifier='31105'); m.facts['notionIds'] = ['a'*32]
        n = candidate('notion', 'a'*32)
        n.sources[0].url = 'https://www.notion.so/' + 'a'*32
        item = build_suggestions(EvidenceBatch(candidates=[m, n] if combined else [n]), now=datetime(2026, 11, 9, tzinfo=timezone.utc))[0]
        return item.promptPreview

    def context(self, question):
        request = ChatRequest(modelId='test', messages=[ChatMessage(role='user', content=question)])
        with patch.object(prepare, 'get_private_model', return_value={'id': 'test', 'status': 'active'}), \
             patch.object(prepare.llm, 'get_model_api_key', return_value='key'), \
             patch('backend.intent.unified.plan_question', side_effect=AssertionError('이미 명시한 URL을 모델이 다시 선택함')):
            context = prepare.prepare_chat_pipeline(request, question, {'uid': 'u', 'mantisUsername': '김담당'})
            planning.plan_chat_query(request, context, {'uid': 'u'})
        return context

    def test_generated_question_preserves_explicit_sources_in_both_planners(self):
        for unified in ('true', 'false'):
            for combined in (False, True):
                with self.subTest(unified=unified, combined=combined), patch.dict('os.environ', {'UNIFIED_QUERY_PLANNER_ENABLED': unified}):
                    context = self.context(self.prompt(combined))
                    self.assertEqual(context.intent_plan['source'], 'both' if combined else 'notion')
                    self.assertEqual(context.intent_plan['operation'], 'explain')
                    self.assertEqual(context.query_constraints['notion_page_ids'], ['a'*32])
                    self.assertEqual(context.query_constraints['issue_ids'], [31105] if combined else [])
                    self.assertEqual(context.query_constraints['statuses'], [])
                    self.assertEqual(context.query_constraints['excluded_statuses'], [])
                    self.assertIsNone(context.query_constraints['created_at'])

    def test_actual_send_reads_current_page_and_respects_disabled_sources(self):
        from backend.notion import direct_lookup
        question = self.prompt(True)
        context = self.context(question)
        page = {'id': 'a'*32, 'title': '현재 문서', 'content': 'prepare 이후 갱신된 본문', '_verified_body': True, 'content_incomplete': False}
        with patch.object(direct_lookup, 'resolve_notion_settings', return_value={'token': 'test', 'database_ids': ['db']}), \
             patch.object(direct_lookup, 'resolve_notion_read_scope', return_value=({'db': 'db'}, True)), \
             patch.object(direct_lookup, 'read_candidate_page', return_value={**page, 'database_id': 'db'}), \
             patch.object(direct_lookup, 'read_current_page', return_value=page) as read, \
             patch('backend.services.retrieval_pipeline.execute_mantis_id_lookup', return_value={'source': 'mantis_id_lookup', 'status': 'ok', 'issues_list': []}) as mantis:
            result = run_retrieval_pipeline(question, 5, {'mantis': True, 'notion': True}, context.intent_plan)
            self.assertEqual(result['stages'][0]['notion_pages'][0]['content'], page['content'])
            self.assertEqual(read.call_args.args[0]['page_id'], 'a'*32)
            self.assertEqual(mantis.call_args.args[0], [31105])
            read.reset_mock(); mantis.reset_mock()
            result = run_retrieval_pipeline(question, 5, {'mantis': False, 'notion': False}, context.intent_plan)
            self.assertFalse(result['stages']); read.assert_not_called(); mantis.assert_not_called()

    def test_direct_url_outside_read_scope_never_reads_body(self):
        from backend.notion import direct_lookup
        with patch.object(direct_lookup, 'resolve_notion_settings', return_value={'token': 'test', 'database_ids': ['db']}), \
             patch.object(direct_lookup, 'resolve_notion_read_scope', return_value=({'db': 'db'}, True)), \
             patch.object(direct_lookup, 'read_candidate_page', return_value=None), \
             patch.object(direct_lookup, 'read_current_page') as body:
            result = direct_lookup.read_explicit_pages(['a'*32], {})
        body.assert_not_called(); self.assertFalse(result['notion_pages'])

    def test_write_request_is_not_converted_to_direct_read(self):
        from backend.intent.direct_references import explicit_reference_plan
        self.assertIsNone(explicit_reference_plan('https://www.notion.so/' + 'a'*32 + ' 문서를 삭제해줘'))

    def test_mantis_only_question_keeps_short_explicit_issue_id(self):
        from backend.intent.direct_references import explicit_reference_plan
        item = build_suggestions(EvidenceBatch(candidates=[candidate(identifier='12')]),
                                 now=datetime(2026, 11, 9, tzinfo=timezone.utc))[0]
        plan = explicit_reference_plan(item.promptPreview)
        self.assertEqual(plan['source'], 'mantis')
        self.assertEqual(plan['resolved_constraints']['issue_ids'], [12])
        with patch('backend.services.retrieval_pipeline.execute_mantis_id_lookup', return_value={'source': 'mantis_id_lookup', 'issues_list': []}) as lookup, \
             patch('backend.notion.direct_lookup.read_explicit_pages') as notion:
            run_retrieval_pipeline(item.promptPreview, 3, {'mantis': True, 'notion': True}, plan)
        lookup.assert_called_once(); notion.assert_not_called()

    def test_six_business_fixtures_keep_work_identity_and_partial_sources(self):
        import json
        from pathlib import Path
        from backend.suggestions.sources import _issue_candidate, page_candidate, current_scope_key
        from backend.sources.mantis_fields import normalize_issue
        cases = json.loads((Path(__file__).parent / 'fixtures/home_suggestions.json').read_text())
        now = datetime(2026, 11, 9, tzinfo=timezone.utc)
        for case in cases:
            with self.subTest(case=case['name']):
                if case.get('before'):
                    self.assertNotEqual(current_scope_key(case['before']), current_scope_key(case['after']))
                    continue
                candidates = []
                if case.get('mantis'):
                    with patch('backend.suggestions.sources.get_mantis_settings', return_value={'url':'https://mantis.test/api/rest/issues'}):
                        candidates.append(_issue_candidate(normalize_issue(case['mantis']), now, {}))
                if case.get('linked'):
                    candidates[0].facts['notionIds'] = [case['notion']['id']]
                if case.get('notion'):
                    candidates.append(page_candidate(case['notion'], now, relation='owner'))
                result = build_suggestions(EvidenceBatch(candidates=candidates), now=now)
                self.assertEqual(len(result), case['count'])
                self.assertEqual({s.type for item in result for s in item.sources}, set(case['sources']))
                if 'reason' in case:
                    self.assertEqual(result[0].reasonCode, case['reason'])
                if 'kind' in case:
                    self.assertEqual(result[0].kind, case['kind'])
                for item in result:
                    self.assertNotIn('검증 완료', item.title)
                    self.assertNotIn('모순', item.title)

    def test_reference_plan_does_not_steal_similarity_or_report_requests(self):
        from backend.intent.direct_references import explicit_reference_plan
        for question in ('Mantis #31105 와 비슷한 과거 이슈를 비교해서 정리해줘',
                         'Mantis #31105 의 공수를 정리해줘'):
            self.assertIsNone(explicit_reference_plan(question))
