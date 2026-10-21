"""자연어 정확도 점검에서 발견한 조건 누락과 실제 출처 참조 회귀."""
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from backend import audit
from backend.intent import unified
from backend.intent.unified_validation import validate_plan
from backend.schemas import ChatMessage, ChatRequest
from backend.services.unified_planning import prepare_context
from backend.sources.mantis import issue_matches_constraints
from test_unified_planner import proposal


USER = {'uid': 'accuracy-regression', 'mantisUsername': '석지원'}
NO_STATUS = {'mode': 'unspecified', 'values': [], 'evidence': ''}


def response(parsed):
    return Mock(json=lambda: {'choices': [{'message': {'content': json.dumps(parsed, ensure_ascii=False)}}]})


class ExplicitConditionRegressionTests(unittest.TestCase):
    def test_ordinal_grading_requires_selected_id_not_an_implicit_new_team_search(self):
        from scripts.accuracy.dynamic_accuracy_check import (
            AccuracyCase, FollowUpScenario, expected_follow_up_constraints, grade,
        )
        parent = AccuracyCase('parent', 'mantis', '고객사 최신', '유안타증권 최근 이슈 세 개만 보여줘',
                              'ordered_issue_ids', [31368, 31208, 31202], oracle_filters={'scope': 'team'})
        question = '그중 두 번째 이슈를 자세히 알려줘'
        scenario = FollowUpScenario('followup-project-second', 'mantis', '순번', 'parent',
                                    question, 'exact_issue_ids', [31208])
        expected = expected_follow_up_constraints(scenario, parent)
        self.assertEqual(expected, {'issue_ids': [31208]})
        case = AccuracyCase('followup', 'mantis', '순번', question, 'exact_issue_ids', [31208],
                            expected_constraints=expected)
        plan = validate_plan('0031208 ' + question, proposal(operation='get', scope='target',
            status=NO_STATUS, filters=[{'field': 'issue_id', 'operator': 'equals',
                                       'value': '0031208', 'evidence': '0031208'}]), USER, {})
        result = {'content': '#31208 상세 내용', 'sources': [{'type': 'mantis', 'issue_id': 31208}],
                  'retrieval': {'query': plan['grounding_text'], 'query_plan': plan,
                                'constraints': plan['resolved_constraints']}}
        self.assertTrue(grade(case, result)['passed'])
        result['retrieval']['constraints'] = {'issue_ids': []}
        self.assertFalse(grade(case, result)['passed'])

    def test_named_handler_cannot_be_replaced_by_owner_grouping(self):
        question = '담당자 권한별의 최근 이슈 2개 보여줘'
        bad = proposal(operation='search', scope='team', status=NO_STATUS, group_by='owner', limit=2)
        with self.assertRaisesRegex(ValueError, '담당자'):
            validate_plan(question, bad, USER, {})
        correct = {**bad, 'scope': 'target', 'group_by': None, 'filters': [
            {'field': 'owner', 'operator': 'equals', 'value': '권한별', 'evidence': '담당자 권한별'},
        ]}
        constraints = validate_plan(question, correct, USER, {})['resolved_constraints']
        self.assertEqual(constraints['people']['names'], ['권한별'])
        self.assertTrue(issue_matches_constraints({'id': 31651, 'handler': {'real_name': '권한별'}}, constraints))
        self.assertFalse(issue_matches_constraints({'id': 31659, 'handler': {'real_name': '이영희'}}, constraints))

    def test_person_in_an_issue_explanation_is_not_forced_into_a_search_filter(self):
        question = '#31208 이슈에서 담당자 권한별이 처리한 경위를 설명해줘'
        plan = validate_plan(question, proposal(operation='explain', scope='target', status=NO_STATUS,
            filters=[{'field': 'issue_id', 'operator': 'equals', 'value': '31208',
                      'evidence': '#31208'}]), USER, {})
        self.assertEqual(plan['resolved_constraints']['issue_ids'], [31208])
        self.assertEqual(plan['resolved_constraints']['people']['names'], [])

    def test_literal_notion_title_is_not_accepted_as_a_body_keyword(self):
        question = "노션에서 '@하영훈' 문서 좀 찾아줘"
        bad = proposal(source='notion', operation='search', scope='all', status=NO_STATUS, filters=[
            {'field': 'keyword', 'operator': 'contains', 'value': '@하영훈', 'evidence': "'@하영훈'"},
        ])
        with self.assertRaisesRegex(ValueError, '제목'):
            validate_plan(question, bad, USER, {})
        correct = copy.deepcopy(bad)
        correct['filters'][0].update(field='title', operator='equals')
        result = validate_plan(question, correct, USER, {})
        self.assertEqual(result['filter_clauses'], [{'field': 'title', 'operator': 'equals', 'value': '@하영훈'}])

    def test_explicit_issue_id_cannot_be_omitted_from_detail_lookup(self):
        with self.assertRaisesRegex(ValueError, '번호'):
            validate_plan('0031208 그중 두 번째 이슈를 자세히 알려줘',
                          proposal(operation='get', scope='target', status=NO_STATUS), USER, {})

    def test_title_content_request_does_not_turn_body_literal_into_exact_title(self):
        question = '노션에서 본문에 "차트"가 포함된 문서를 찾아줘'
        result = validate_plan(question, proposal(source='notion', operation='search', scope='all',
            status=NO_STATUS, filters=[{'field': 'keyword', 'operator': 'contains', 'value': '차트',
                                      'evidence': '본문에 "차트"가 포함'}]), USER, {})
        self.assertEqual(result['filter_clauses'][0]['field'], 'keyword')

    def test_temporary_planning_timeout_retries_without_dropping_notion_exclusion(self):
        question = "노션에서 '교보증권 2026.09 정기점검(HTS)' 항목을 찾아줘. 시작전 상태는 빼고"
        parsed = proposal(source='notion', operation='search', scope='all', status=NO_STATUS, filters=[
            {'field': 'title', 'operator': 'equals', 'value': '교보증권 2026.09 정기점검(HTS)',
             'evidence': "'교보증권 2026.09 정기점검(HTS)'"},
            {'field': 'status', 'operator': 'not_equals', 'value': '시작전', 'evidence': '시작전 상태는 빼고'},
        ])
        with patch.object(unified, 'post_model_chat', side_effect=[requests.ReadTimeout(), response(parsed)]):
            result = unified.plan_question(question, {'id': 'fixture-model'}, 'key', USER, {})
        self.assertEqual(result.get('source'), 'notion')
        self.assertEqual(result['filter_clauses'], [
            {'field': 'title', 'operator': 'equals', 'value': '교보증권 2026.09 정기점검(HTS)'},
            {'field': 'status', 'operator': 'not_equals', 'value': '시작전'},
        ])

    def test_permanent_planning_auth_error_is_not_retried(self):
        error = requests.HTTPError(response=Mock(status_code=401))
        with patch.object(unified, 'post_model_chat', side_effect=error) as model:
            result = unified.plan_question('이슈 찾아줘', {'id': 'fixture-model'}, 'key', USER, {})
        self.assertEqual(model.call_count, 1)
        self.assertEqual(result['operation'], 'clarify')


class SourceBoundReferenceRegressionTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.database = Path(temporary) / 'audit.sqlite3'
        self.enterContext(patch.object(audit, 'AUDIT_DB_PATH', self.database))
        audit.init_audit_db()

    def context(self, first_question, question, sources, parsed, *, owner=USER['uid']):
        source = 'notion' if sources and sources[0].get('type') == 'notion' else 'mantis'
        previous = {'source': source, 'operation': 'search', 'grounding_text': first_question,
                    'resolved_constraints': {'filter_clauses': [], 'mantis_team_scope': 'team'}}
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('INSERT INTO validation_logs '
                       '(id,created_at,question,model_id,mode,score,status,payload,user_uid) '
                       'VALUES (?,?,?,?,?,?,?,?,?)',
                       ('a' * 32, '2026-10-21', first_question, 'fixture-model', 'search', 100, 'pass',
                        json.dumps({'queryPlan': previous, 'sources': sources}), owner))
        request = ChatRequest(modelId='fixture-model', contextAuditId='a' * 32, messages=[
            ChatMessage(role='user', content=first_question),
            ChatMessage(role='assistant', content='가짜 문서 제목 또는 이슈 번호 #99999'),
            ChatMessage(role='user', content=question),
        ])
        with patch.object(unified, 'post_model_chat', return_value=response(parsed)):
            return prepare_context(request, question, USER, {'id': 'fixture-model'}, 'key', [], [])

    def test_ordinal_detail_uses_second_displayed_source_not_a_new_customer_search(self):
        question = '그중 두 번째 이슈를 자세히 알려줘'
        parsed = proposal(operation='get', scope='target', status=NO_STATUS, filters=[
            {'field': 'issue_id', 'operator': 'equals', 'value': '0031208', 'evidence': '0031208'},
        ])
        context = self.context('유안타증권 최근 이슈 세 개만 보여줘', question,
            [{'issue_id': 31368}, {'issue_id': 31208}, {'issue_id': 31202}], parsed)
        self.assertEqual(context.query_constraints['issue_ids'], [31208])
        self.assertEqual(context.latest_user_text, question)
        self.assertIn('0031208', context.retrieval_text)
        self.assertNotIn('99999', context.retrieval_text)

    def test_single_notion_reference_uses_actual_title_from_user_bound_source(self):
        title = '@하영훈'
        question = '그 항목의 제목을 정확히 다시 알려줘'
        parsed = proposal(source='notion', operation='search', scope='all', status=NO_STATUS, filters=[
            {'field': 'title', 'operator': 'equals', 'value': title, 'evidence': '"@하영훈"'},
        ])
        context = self.context("노션에서 '@하영훈' 문서 좀 찾아줘", question,
                               [{'type': 'notion', 'page_id': 'page-1', 'title': title}], parsed)
        self.assertEqual(context.intent_plan.get('source'), 'notion')
        self.assertEqual(context.query_constraints.get('filter_clauses'),
                         [{'field': 'title', 'operator': 'equals', 'value': title}])
        self.assertIn('"@하영훈"', context.retrieval_text)

    def test_other_users_audit_cannot_supply_reference_target(self):
        parsed = proposal(operation='get', scope='target', status=NO_STATUS, filters=[
            {'field': 'issue_id', 'operator': 'equals', 'value': '0031208', 'evidence': '0031208'},
        ])
        context = self.context('최근 이슈 보여줘', '그중 두 번째 이슈를 자세히 알려줘',
                               [{'issue_id': 31368}, {'issue_id': 31208}], parsed, owner='someone-else')
        self.assertEqual(context.intent_plan.get('operation'), 'clarify')
        self.assertEqual(context.query_constraints.get('issue_ids'), [])

    def test_title_reference_accepts_multiple_notion_pages_with_one_common_title(self):
        title = '@하영훈'
        question = '그 항목의 제목을 정확히 다시 알려줘'
        parsed = proposal(source='notion', operation='search', scope='all', status=NO_STATUS, filters=[
            {'field': 'title', 'operator': 'equals', 'value': title, 'evidence': '"@하영훈"'},
        ])
        context = self.context("노션에서 '@하영훈' 문서 좀 찾아줘", question, [
            {'type': 'notion', 'page_id': 'page-1', 'title': title},
            {'type': 'notion', 'page_id': 'page-2', 'title': title},
        ], parsed)
        self.assertEqual(context.query_constraints.get('filter_clauses'),
                         [{'field': 'title', 'operator': 'equals', 'value': title}])

    def test_different_notion_titles_are_not_guessed_as_a_single_target(self):
        from backend.intent.context import resolve_reference_from_context
        with patch('backend.intent.context.load_audit_sources', return_value=[
            {'type': 'notion', 'page_id': 'page-1', 'title': '주간 업무'},
            {'type': 'notion', 'page_id': 'page-2', 'title': '월간 업무'},
        ]):
            self.assertIsNone(resolve_reference_from_context('그 항목의 제목을 알려줘', 'a' * 32, USER['uid']))

    def test_out_of_range_ordinal_never_falls_back_to_a_broad_search(self):
        parsed = proposal(operation='get', scope='inherit', followup=True,
                          status={'mode': 'inherit', 'values': [], 'evidence': ''})
        context = self.context('최근 이슈 두 개 보여줘', '그중 세 번째 이슈 자세히 알려줘',
                               [{'issue_id': 31368}, {'issue_id': 31208}], parsed)
        self.assertEqual(context.intent_plan.get('operation'), 'clarify')


if __name__ == '__main__':
    unittest.main()
