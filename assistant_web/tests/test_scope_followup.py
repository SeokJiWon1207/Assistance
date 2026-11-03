"""후속 질문이 명시한 조회 범위 변경을 모델이 inherit로 빠뜨리면 재해석한다."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from backend.intent import unified
from backend.intent.followup_changes import requested_scope
from backend.intent.unified_validation import validate_plan
from test_count_list_followup import USER, previous_plan
from test_unified_planner import proposal


def followup(scope='inherit'):
    return proposal(operation='search', scope=scope, followup=True, limit=200,
                    status={'mode': 'inherit', 'values': [], 'evidence': ''})


def company_wide_plan():
    question = '전사 MTS 미종료 이슈 목록 보여줘'
    return validate_plan(question, proposal(scope='all',
        filters=[{'field': 'platform', 'operator': 'equals', 'value': 'mts', 'evidence': 'MTS'}],
        status={'mode': 'open', 'values': [], 'evidence': '미종료'}), USER, {})


class ScopeFollowupTests(unittest.TestCase):
    def test_requested_scope_reads_only_one_explicit_scope(self):
        cases = {
            '내 담당만 목록 다 보여줘': 'self',
            '다시 우리 팀만 목록 다 보여줘': 'team',
            '팀 범위 풀고 전사 기준으로 목록 다 보여줘': 'all',
            '그중 NH증권 것만 목록 다 보여줘': None,
            '우리 팀 중 내 담당만 보여줘': None,
            '제목에 "내 담당" 들어간 것만 보여줘': None,
        }
        for question, expected in cases.items():
            with self.subTest(question=question):
                self.assertEqual(requested_scope(question), expected)

    def test_omitted_personal_scope_is_reinterpreted(self):
        with patch.object(unified, '_propose', side_effect=[followup(), followup('self')]) as model:
            result = unified.plan_question('내 담당만 목록 다 보여줘', {'id': 'fixture'}, 'key', USER, previous_plan())
        c = result['resolved_constraints']
        self.assertEqual(model.call_count, 2)
        self.assertIn('본인 담당', model.call_args.args[2]['messages'][-1]['content'])
        self.assertEqual(c['people']['names'], ['석지원'])
        self.assertEqual(c['platform_key'], 'mts')

    def test_omitted_team_scope_is_reinterpreted_after_company_wide_list(self):
        with patch.object(unified, '_propose', side_effect=[followup(), followup('team')]):
            result = unified.plan_question('다시 우리 팀만 목록 다 보여줘', {'id': 'fixture'}, 'key', USER,
                                           company_wide_plan())
        c = result['resolved_constraints']
        self.assertEqual(c['mantis_team_scope'], 'team')
        self.assertTrue(c['mantis_team_members'])
        self.assertEqual(c['platform_key'], 'mts')

    def test_repeated_omission_asks_instead_of_running_the_old_scope(self):
        with patch.object(unified, '_propose', side_effect=[followup(), followup()]):
            result = unified.plan_question('내 담당만 목록 다 보여줘', {'id': 'fixture'}, 'key', USER, previous_plan())
        self.assertEqual(result['operation'], 'clarify')
        self.assertIn('본인 담당', result['reason'])

    def test_same_scope_or_no_scope_phrase_keeps_inherit(self):
        previous = previous_plan()
        for question in ('우리 팀 것 중 할당된 것만 보여줘', '그중 NH증권 것만 목록 다 보여줘'):
            with self.subTest(question=question):
                result = validate_plan(question, deepcopy(followup()), USER, previous)
                self.assertEqual(result['resolved_constraints']['mantis_team_scope'], 'team')
        mine = validate_plan('내 담당만 목록 다 보여줘', followup('self'), USER, previous)
        again = validate_plan('내 담당 중 HTS도 보여줘', followup(), USER, mine)
        self.assertEqual(again['resolved_constraints']['people']['names'], ['석지원'])

    def test_restoring_team_is_not_a_team_clear(self):
        bad = followup('team')
        bad.update(clear_fields=['team'], clear_evidence={'team': '우리 팀만'})
        with patch.object(unified, '_propose', side_effect=[bad, followup('team')]) as model:
            result = unified.plan_question('다시 우리 팀만 목록 다 보여줘', {'id': 'fixture'}, 'key', USER,
                                           company_wide_plan())
        self.assertIn('팀 범위로 되돌리는 요청은 해제가 아니므로', model.call_args.args[2]['messages'][-1]['content'])
        self.assertEqual(result['resolved_constraints']['mantis_team_scope'], 'team')
