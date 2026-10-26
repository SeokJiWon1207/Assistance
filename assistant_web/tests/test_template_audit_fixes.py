"""10월 26일 실제 템플릿 점검에서 재현한 실행·표현·검증 회귀."""
import copy
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from backend.intent.unified_validation import validate_plan, validate_execution_contract
from backend.reports import weekly_customer
from backend.services.unified_planning import execute_plan
from backend.sources.mantis import issue_matches_constraints
from backend.sources.mantis_collection import IssueBatch
from test_unified_planner import proposal


class AuditDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 10, 26)


class TemplateAuditFixTests(unittest.TestCase):
    def aging(self, days=30):
        q = f'KB증권 {days}일 넘게 미종료된 이슈를 오래된 순으로 보여줘'
        p = proposal(operation='search', scope='team', status={'mode': 'open', 'values': [], 'evidence': '미종료'},
                     filters=[{'field': 'customer', 'operator': 'equals', 'value': 'kb', 'evidence': 'KB증권'}])
        return q, p

    def test_aging_condition_cannot_disappear_or_change_cutoff(self):
        q, p = self.aging()
        with patch('backend.intent.unified_validation.date', AuditDate):
            with self.assertRaisesRegex(ValueError, '30일'):
                validate_plan(q, p, {}, {})
            p['filters'].append({'field': 'created_at', 'operator': 'lte', 'value': '2026-09-27', 'evidence': '30일 넘게'})
            with self.assertRaisesRegex(ValueError, '2026-09-26'):
                validate_plan(q, p, {}, {})

    def test_aging_cutoff_includes_boundary_and_excludes_recent_and_other_team(self):
        q, p = self.aging()
        p['filters'].append({'field': 'created_at', 'operator': 'lte', 'value': '2026-09-26', 'evidence': '30일 넘게'})
        with patch('backend.intent.unified_validation.date', AuditDate):
            plan = validate_plan(q, p, {}, {})
            self.assertTrue(validate_execution_contract(q, plan)['valid'])
        raw = {'project': {'name': '112KBStar(KB증권)'}, 'status': {'name': 'assigned'},
               'handler': {'real_name': '석지원'}, 'created_at': '2026-09-26'}
        self.assertTrue(issue_matches_constraints(raw, plan['resolved_constraints']))
        self.assertFalse(issue_matches_constraints({**raw, 'created_at': '2026-09-27'}, plan['resolved_constraints']))
        self.assertFalse(issue_matches_constraints({**raw, 'handler': {'real_name': '다른팀'}}, plan['resolved_constraints']))

    def test_verifier_rejects_old_validated_plan_missing_aging_condition(self):
        q, p = self.aging()
        plan = validate_plan('KB증권 미종료 이슈를 보여줘', p, {}, {})
        plan['grounding_text'] = q
        with patch('backend.intent.unified_validation.date', AuditDate):
            self.assertFalse(validate_execution_contract(q, plan)['valid'])

    def test_weekly_period_cannot_disappear(self):
        q = '지난주 주간 보고서를 신규 이슈, 미종료 이슈, 장기 적체, 공수, 평균 처리시간 중심으로 작성해줘'
        p = proposal(operation='report', report_kind='weekly', scope='team',
                     status={'mode': 'unspecified', 'values': [], 'evidence': ''})
        with self.assertRaisesRegex(ValueError, '기간'):
            validate_plan(q, p, {}, {})

    def test_report_execution_accepts_personal_work_report_using_confirmed_scope(self):
        q = '내 담당 Mantis 이슈를 기준으로 이번 주 업무보고서를 작성해줘'
        constraints = {'created_at': {'from': '2026-10-26', 'to': '2026-10-26'},
                       'people': {'names': ['석지원'], 'role': 'handler'}, 'statuses': []}
        raw = [{'id': 31631, 'project': {'name': '140Kyobo(교보증권)'}, 'summary': '확인 요청',
                'status': {'name': 'confirmed'}, 'handler': {'real_name': '석지원'},
                'created_at': '2026-10-19', 'updated_at': '2026-10-26'}]
        context = SimpleNamespace(intent_plan={'source': 'mantis', 'operation': 'report', 'report_kind': 'weekly'},
                                  enabled_tools={'mantis': True}, latest_user_text=q, query_constraints=constraints)
        with patch.object(weekly_customer, 'load_report_issues', return_value=IssueBatch(raw, {'search_complete': True})), \
                patch.object(weekly_customer, 'date', AuditDate):
            result = execute_plan(SimpleNamespace(modelId='test'), context, {'mantisUsername': '석지원'})
        self.assertEqual(result['raw']['mode'], 'deterministic_weekly_report')
        self.assertIn('석지원', result['content'].splitlines()[0])
        self.assertIn('현재 미종료: **1건**', result['content'])
        self.assertNotIn('교보증권', result['content'].splitlines()[0])

    def test_all_customer_heading_does_not_pick_the_most_common_customer(self):
        self.assertEqual(weekly_customer._customer_label({}, [{'project': '120NH'}]), '전체 고객사')

    def test_quoted_aging_phrase_is_not_a_date_filter(self):
        q = '제목에 "30일 넘게"가 포함된 미종료 이슈를 보여줘'
        _, p = self.aging()
        p['filters'] = [{'field': 'title', 'operator': 'contains', 'value': '30일 넘게', 'evidence': '"30일 넘게"'}]
        self.assertEqual(validate_plan(q, p, {}, {})['resolved_constraints']['created_at'], None)

    def test_aging_followup_retains_cutoff_until_explicitly_changed(self):
        q, p = self.aging()
        p['filters'].append({'field': 'created_at', 'operator': 'lte', 'value': '2026-09-26', 'evidence': '30일 넘게'})
        with patch('backend.intent.unified_validation.date', AuditDate):
            previous = validate_plan(q, p, {}, {})
            followup = proposal(operation='search', scope='inherit', followup=True,
                                status={'mode': 'inherit', 'values': [], 'evidence': ''},
                                filters=[{'field': 'platform', 'operator': 'equals', 'value': 'mts', 'evidence': 'MTS'}])
            result = validate_plan('그중 MTS만 보여줘', followup, {}, previous)
            self.assertIn({'field': 'created_at', 'operator': 'lte', 'value': '2026-09-26'}, result['filter_clauses'])
            changed = copy.deepcopy(followup); changed['filters'] = []
            with self.assertRaisesRegex(ValueError, '90일'):
                validate_plan('90일 넘게 미종료된 것으로 바꿔줘', changed, {}, previous)

    def test_customer_notice_cannot_fall_through_to_internal_issue_explanation(self):
        q = '#31632 이슈를 바탕으로 내부 식별자를 제외하고 간결한 말투의 메신저 답변 형식으로 작성해줘'
        p = proposal(operation='explain', scope='target', status={'mode':'unspecified','values':[],'evidence':''},
                     filters=[{'field':'issue_id','operator':'equals','value':'31632','evidence':'#31632'}])
        with self.assertRaisesRegex(ValueError, 'notice'):
            validate_plan(q, p, {}, {})
        p.update(operation='report', report_kind='notice')
        self.assertEqual(validate_plan(q, p, {}, {})['report_kind'], 'notice')

    def test_aging_cutoff_is_visible_in_search_criteria(self):
        from backend.reports.aggregate import format_issue_list_criteria
        answer = format_issue_list_criteria({'filter_clauses': [
            {'field':'created_at','operator':'lte','value':'2026-09-26'}]}, [])
        self.assertIn('2026-09-26', answer)
        self.assertIn('등록일', answer)
