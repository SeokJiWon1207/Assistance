"""비교·집계의 명시 조건을 오경고하거나 실패한 앞 조건을 버리지 않는다."""
from unittest import TestCase

from backend.intent.unified_validation import validate_plan


def plan(**updates):
    return {'source': 'mantis', 'operation': 'count', 'scope': 'all', 'followup': False,
            'filters': [], 'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
            'clear_fields': [], 'terms': [], 'group_by': None, 'limit': None, **updates}


class ComparisonPlanRecoveryTests(TestCase):
    def test_explicit_status_all_word_order_is_supported(self):
        result = validate_plan('전사 MTS 이슈 상태 전체로 세어줘', plan(
            status={'mode': 'all', 'values': [], 'evidence': '상태 전체'}), {}, {})
        self.assertEqual(result['resolved_constraints']['statuses'], [])
        self.assertEqual(result['resolved_constraints']['mantis_team_scope'], 'all')

    def test_followup_without_a_confirmed_parent_cannot_broaden_the_count(self):
        with self.assertRaisesRegex(ValueError, '이전|후속'):
            validate_plan('그 조건에서 해결된 이슈와 종료된 이슈를 모두 제외한 건수는?', plan(
                status={'mode': 'exclude', 'values': ['resolved', 'closed'],
                        'evidence': '해결된 이슈와 종료된 이슈를 모두 제외'}), {}, {})

    def test_followup_cannot_discard_a_confirmed_parent_by_claiming_independence(self):
        previous = validate_plan('전사 NH 이슈 몇 건?', plan(filters=[
            {'field': 'customer', 'operator': 'equals', 'value': 'NH', 'evidence': 'NH'}]), {}, {})
        with self.assertRaisesRegex(ValueError, '이전|후속'):
            validate_plan('그중 종료된 이슈를 제외해서 세어줘', plan(
                status={'mode': 'exclude', 'values': ['closed'], 'evidence': '종료된 이슈를 제외'}), {}, previous)

    def test_unsupported_filter_repair_identifies_the_field_and_operator(self):
        with self.assertRaisesRegex(ValueError, 'issue_id.*lte'):
            validate_plan('#9000보다 오래된 유사 사례 원인과 조치 비교', plan(
                operation='report', report_kind='similar', scope='target', filters=[
                    {'field': 'issue_id', 'operator': 'lte', 'value': 9000, 'evidence': '#9000보다 오래된'}]), {}, {})

    def test_explicit_include_every_state_with_a_particle_is_supported(self):
        result=validate_plan('전사 MTS 이슈 상태를 모두 포함해서 세어줘',plan(
            status={'mode':'all','values':[],'evidence':'상태를 모두 포함'}),{}, {})
        self.assertEqual(result['resolved_constraints']['statuses'],[])
        for q in ['남아 있는 이슈 모두 세어줘','미종료 이슈 전부 몇 개야?']:
            with self.assertRaises(ValueError):
                validate_plan(q,plan(status={'mode':'all','values':[],'evidence':'모두' if '모두' in q else '전부'}),{}, {})

    def test_all_status_validation_uses_the_whole_unquoted_question(self):
        result=validate_plan('전사 MTS 이슈는 상태는 전부 포함해서 세어줘',plan(
            status={'mode':'all','values':[],'evidence':'전부 포함'}),{}, {})
        self.assertEqual(result['resolved_constraints']['statuses'],[])
        with self.assertRaises(ValueError):
            validate_plan('제목에 "상태는 전부 포함"이 있는 이슈 몇 건?',plan(
                status={'mode':'all','values':[],'evidence':'상태는 전부 포함'}),{}, {})
