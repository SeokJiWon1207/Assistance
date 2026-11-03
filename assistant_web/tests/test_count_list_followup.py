"""건수 답변 뒤 전체 목록 요청은 확정된 조건으로 원문 목록을 반환한다."""
from contextlib import nullcontext
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from backend.intent import unified
from backend.intent.literal_plans import literal_plan
from backend.intent.unified_validation import validate_execution_contract, validate_plan
from backend.schemas import ChatMessage, ChatRequest
from backend.services.chat.retrieve import retrieval_limit, retrieve_chat_evidence
from backend.services.unified_planning import execute_plan, prepare_context
from backend.sources.mantis_collection import IssueBatch
from test_unified_planner import proposal


USER = {'uid': 'count-list-followup', 'mantisUsername': '석지원'}
QUESTION = 'MTS 안끝난 이슈 몇개야'


def previous_plan():
    return validate_plan(QUESTION, proposal(scope='team',
        filters=[{'field': 'platform', 'operator': 'equals', 'value': 'mts', 'evidence': 'MTS'}],
        status={'mode': 'open', 'values': [], 'evidence': '안끝난'}), USER, {})


class CountListFollowupTests(unittest.TestCase):
    def plan(self, question, previous):
        interpreted = proposal(operation='search', scope='inherit', followup=True, limit=200,
            status={'mode': 'inherit', 'values': [], 'evidence': ''})
        with patch.object(unified, '_propose', return_value=interpreted) as model:
            result = unified.plan_question(question, {'id': 'fixture'}, 'key', USER, previous)
        self.assertEqual(model.call_count, 1)
        context = json.loads(model.call_args.args[2]['messages'][1]['content'])
        self.assertEqual(context['question'], question)
        self.assertEqual(context['previous_confirmed_plan']['grounding_text'], previous['grounding_text'])
        self.assertEqual(context['previous_confirmed_plan']['resolved_constraints'], previous['resolved_constraints'])
        return result

    def test_count_to_full_list_preserves_open_platform_and_team(self):
        for question in ('리스트 전부 보여줘', '목록을 모두 보여주세요', '전체 목록 보여줘',
                         '전부 보여줘', '그럼 이슈를 전부 보여줘', '같은 조건으로 목록 전체 보여줘',
                         '목록 다 보여줘', '그 이슈들 빠짐없이 나열해줘', '그 16건을 하나씩 보여줄래?'):
            with self.subTest(question=question):
                result = self.plan(question, previous_plan())
                self.assertEqual(result.get('operation'), 'search')
                self.assertEqual(result['limit'], 200)
                self.assertEqual(result['resolved_constraints']['platform_key'], 'mts')
                self.assertEqual(result['resolved_constraints']['statuses'], ['open'])
                self.assertEqual(result['resolved_constraints']['mantis_team_scope'], 'team')
                self.assertTrue(validate_execution_contract(question, result)['valid'])

    def test_full_list_preserves_existing_filters_terms_and_sort(self):
        question = '2026년 10월 NH MTS 담당자 석지원 미종료 이슈 제목에 차트 포함, 점검 제외, 오류 관련 몇 건이야?'
        previous = validate_plan(question, proposal(scope='target', terms=['오류'], filters=[
            {'field': 'customer', 'operator': 'equals', 'value': 'NH', 'evidence': 'NH'},
            {'field': 'platform', 'operator': 'equals', 'value': 'mts', 'evidence': 'MTS'},
            {'field': 'owner', 'operator': 'equals', 'value': '석지원', 'evidence': '담당자 석지원'},
            {'field': 'created_at', 'operator': 'between', 'value': ['2026-10-01', '2026-10-31'], 'evidence': '2026년 10월'},
            {'field': 'title', 'operator': 'contains', 'value': '차트', 'evidence': '제목에 차트 포함'},
            {'field': 'title', 'operator': 'not_contains', 'value': '점검', 'evidence': '점검 제외'},
        ], status={'mode': 'open', 'values': [], 'evidence': '미종료'}), USER, {})
        previous['sort'] = {'field': 'created_at', 'direction': 'asc'}
        original = deepcopy(previous)
        result = self.plan('리스트 전부 보여줘', previous)
        self.assertEqual(result.get('operation'), 'search')
        self.assertCountEqual(result['filter_clauses'], original['filter_clauses'])
        self.assertEqual(result['terms'], ['오류'])
        self.assertEqual(result['sort'], {'field': 'created_at', 'direction': 'asc'})
        self.assertEqual(result['resolved_constraints']['people']['names'], ['석지원'])
        self.assertEqual(previous, original)

    def test_existing_search_also_expands_the_whole_list(self):
        previous = previous_plan()
        previous.update(operation='search', limit=2)
        result = self.plan('리스트 전부 보여줘', previous)
        self.assertEqual(result.get('operation'), 'search')
        self.assertEqual(result['limit'], 200)
        self.assertEqual(result['resolved_constraints']['statuses'], ['open'])

    def test_additional_instructions_and_unconfirmed_context_do_not_use_display_rule(self):
        previous = previous_plan()
        for question in ('리스트 전부 보여줘', '목록 다 보여줘',
                         '리스트 전부 보여줘. 종료도 포함해', '리스트 전부 보여줘 NH만',
                         '전부 몇 건이야?', '리스트 전부 보여줘 담당자 조건은 해제해',
                         '제목에 "리스트 전부 보여줘" 포함된 이슈 찾아줘'):
            with self.subTest(question=question):
                self.assertIsNone(literal_plan(question, previous))
        for invalid in ({}, {**previous, 'source': 'notion'}, {**previous, 'operation': 'report'},
                        {**previous, 'resolved_constraints': {}}):
            with self.subTest(previous=invalid):
                self.assertIsNone(literal_plan('리스트 전부 보여줘', invalid))

    def test_invalid_count_with_display_limit_is_reinterpreted_by_model(self):
        wrong = proposal(scope='inherit', followup=True, limit=200,
            status={'mode': 'inherit', 'values': [], 'evidence': ''})
        corrected = {**wrong, 'operation': 'search'}
        previous = previous_plan()
        with patch.object(unified, '_propose', side_effect=[wrong, corrected]) as model:
            result = unified.plan_question('리스트 전부 보여줘', {'id': 'fixture'}, 'key', USER, previous)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(result.get('operation'), 'search')
        self.assertEqual(result['limit'], 200)
        self.assertEqual(result['resolved_constraints']['platform_key'], 'mts')
        self.assertEqual(result['resolved_constraints']['statuses'], ['open'])

    def test_repeated_invalid_model_plan_does_not_run_a_broader_query(self):
        wrong = proposal(scope='inherit', followup=True, limit=200,
            status={'mode': 'inherit', 'values': [], 'evidence': ''})
        with patch.object(unified, '_propose', return_value=wrong):
            result = unified.plan_question('리스트 전부 보여줘', {'id': 'fixture'}, 'key', USER, previous_plan())
        self.assertEqual(result.get('operation'), 'clarify')
        self.assertNotIn('resolved_constraints', result)

    def test_model_can_keep_count_or_explicitly_change_status(self):
        for question, interpreted, operation, statuses in (
            ('그 조건으로 전부 몇 건이야?', proposal(scope='inherit', followup=True,
                status={'mode': 'inherit', 'values': [], 'evidence': ''}), 'count', ['open']),
            ('목록 다 보여줘. 종료도 포함해', proposal(operation='search', scope='inherit', followup=True,
                limit=200, status={'mode': 'all', 'values': [], 'evidence': '종료도 포함'}), 'search', []),
        ):
            with self.subTest(question=question), patch.object(unified, '_propose', return_value=interpreted):
                result = unified.plan_question(question, {'id': 'fixture'}, 'key', USER, previous_plan())
            self.assertEqual(result['operation'], operation)
            self.assertEqual(result['resolved_constraints']['statuses'], statuses)
            self.assertEqual(result['resolved_constraints']['platform_key'], 'mts')

    def test_model_followup_without_authenticated_prior_plan_requires_clarification(self):
        interpreted = proposal(operation='search', scope='inherit', followup=True, limit=200,
            status={'mode': 'inherit', 'values': [], 'evidence': ''})
        with patch.object(unified, '_propose', return_value=interpreted):
            result = unified.plan_question('목록 다 보여줘', {'id': 'fixture'}, 'key', USER, {})
        self.assertEqual(result.get('operation'), 'clarify')
        self.assertNotIn('resolved_constraints', result)

    def test_authenticated_count_followup_returns_all_sixteen_matching_issues(self):
        question = '리스트 전부 보여줘'
        request = ChatRequest(modelId='fixture', contextAuditId='a' * 32, searchLimit=5,
            tools={'mantis': True, 'vectorDb': False, 'notion': False}, messages=[
                ChatMessage(role='user', content=QUESTION), ChatMessage(role='assistant', content='총 16건입니다.'),
                ChatMessage(role='user', content=question)])
        interpreted = proposal(operation='search', scope='inherit', followup=True, limit=200,
            status={'mode': 'inherit', 'values': [], 'evidence': ''})
        with patch('backend.services.unified_planning.load_audit_query_plan', return_value=previous_plan()), \
                patch.object(unified, '_propose', return_value=interpreted) as model:
            context = prepare_context(request, question, USER, {'id': 'fixture'}, 'key', [], [])
        self.assertEqual(model.call_count, 1)
        self.assertEqual(context.intent_plan.get('operation'), 'search')
        self.assertIsNone(execute_plan(request, context, USER))
        self.assertEqual(retrieval_limit(request, context), (200, None))
        rows = [{'id': 31000 + i, 'summary': f'MTS 원문 이슈 {i}', 'project': {'name': '120NH(NH증권)'},
                 'handler': {'real_name': '석지원'}, 'status': {'name': 'assigned'},
                 'created_at': f'2026-10-{i + 1:02d}T09:00:00+09:00',
                 'custom_fields': [{'field': {'name': '매체구분'}, 'value': 'MTS'}]} for i in range(16)]
        for i, changes in enumerate(({'status': {'name': 'closed'}},
                                      {'custom_fields': [{'field': {'name': '매체구분'}, 'value': 'HTS'}]},
                                      {'handler': {'real_name': '다른팀담당자'}}), start=16):
            rows.append({**rows[0], 'id': 31000 + i, **changes})
        batch = IssueBatch(rows, {'search_complete': True, 'snapshot_complete': True, 'status': 'snapshot'})
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda **kw: nullcontext(batch)):
            result = retrieve_chat_evidence(request, context).response
        self.assertEqual(result['raw']['mode'], 'deterministic_issue_list')
        self.assertEqual([s['issue_id'] for s in result['sourcesForFollowUps']], list(range(31015, 30999, -1)))
        self.assertIn('MTS 원문 이슈 0', result['content'])
        self.assertIn('MTS 원문 이슈 15', result['content'])
        self.assertEqual(context.query_constraints['inherited_from_audit_id'], 'a' * 32)
