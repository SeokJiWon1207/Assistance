"""공수는 전달일과 기입자를 기준으로 집계하며 등록일/담당자로 대체하지 않는다."""
from contextlib import nullcontext
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from backend.intent.unified_validation import validate_plan
from backend.reports import effort
from backend.sources.mantis import mantis_issue_effort_date
from backend.sources.mantis_collection import IssueBatch


def issue(number, delivered, amount, *, created='2025-01-01', handler='다른사람'):
    return {'id': number, 'summary': f'검증 이슈 {number}',
            'project': {'name': '147MiraeAsset(미래에셋증권)'},
            'created_at': created, 'handler': {'real_name': handler},
            'status': {'name': 'closed'}, 'custom_fields': [
                {'field': {'name': '고객사전달일'}, 'value': delivered},
                {'field': {'name': '3PC공수'}, 'value': f'석지원({amount}), 다른사람(9)'},
                {'field': {'name': '매체구분'}, 'value': 'MTS'}]}


def proposal(**changes):
    return {'source': 'mantis', 'operation': 'report', 'report_kind': 'effort',
            'scope': 'self', 'followup': False, 'filters': [
                {'field': 'delivered_at', 'operator': 'between',
                 'value': ['2026-10-01', '2026-10-31'], 'evidence': '2026년 10월'}],
            'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
            'clear_fields': [], 'terms': [], 'group_by': None, 'limit': None,
            'effort': {'type': 'total'}, **changes}


class EffortDeliveryTests(TestCase):
    def test_date_uses_actual_delivery_and_never_registration_or_target(self):
        for raw, expected in [('2026-10-03', '2026-10-03'), ('2026-02-30', ''),
                              ('0', ''), ('', ''), ('미상', '')]:
            with self.subTest(raw=raw):
                row = issue(1, raw, 1, created='2026-10-08')
                row['custom_fields'].append({'field': {'name': '고객사전달 목표일'}, 'value': '2026-10-09'})
                self.assertEqual(mantis_issue_effort_date(row), expected)

    def test_person_total_includes_old_registration_other_owner_and_excludes_undelivered(self):
        rows = [issue(1, '2026-10-01', .75), issue(2, '2026-10-31', 1.25),
                issue(3, '2026-11-01', 20, created='2026-10-01'),
                issue(4, '', 30, created='2026-10-02')]
        batch = IssueBatch(rows, {'search_complete': True, 'status': 'snapshot'})
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(batch)), \
             patch.object(effort, 'fetch_mantis_issues', side_effect=lambda p: rows if p['page'] == 1 else []):
            result = effort.run_person_effort_report('2026년 10월 석지원 공수', '석지원', 2026, 10)
        self.assertEqual(result['effortChart']['totalEffort'], 2)
        self.assertEqual({s['issue_id'] for s in result['sources']}, {1, 2})
        self.assertEqual(result['retrieval']['stages'][0]['meta']['date_basis'], 'delivered_at')
        self.assertIn('고객사전달일', result['content'])

    def test_two_old_pages_do_not_hide_later_deliveries(self):
        pages = {1: [issue(1, '2025-01-01', 10)], 2: [issue(2, '2025-02-01', 10)],
                 3: [issue(3, '2026-10-15', .75)]}
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(None)), \
             patch.object(effort, 'fetch_mantis_issues', side_effect=lambda p: pages.get(p['page'], [])):
            result = effort.run_person_effort_report('2026년 10월 석지원 공수', '석지원', 2026, 10)
        self.assertEqual(result['effortChart']['totalEffort'], .75)
        self.assertEqual([s['issue_id'] for s in result['sources']], [3])
        self.assertTrue(result['retrieval']['stages'][0]['meta']['search_complete'])

    def test_llm_plan_binds_self_to_effort_contributor(self):
        plan = validate_plan('2026년 10월 내 공수', proposal(), {'uid': 'user1', 'mantisUsername': '석지원'}, {})
        clauses = plan['resolved_constraints']['filter_clauses']
        self.assertIn({'field': 'effort_person', 'operator': 'equals', 'value': '석지원'}, clauses)
        self.assertFalse(any(c['field'] == 'owner' for c in clauses))
        self.assertFalse(plan['resolved_constraints'].get('created_at'))

    def test_self_effort_can_keep_an_explicit_other_issue_owner(self):
        p = proposal(filters=[*proposal()['filters'],
            {'field': 'owner', 'operator': 'equals', 'value': '홍길동', 'evidence': '홍길동 담당'}])
        c = validate_plan('2026년 10월 홍길동 담당 이슈의 내 공수', p,
                          {'mantisUsername': '석지원'}, {})['resolved_constraints']
        self.assertIn({'field': 'owner', 'operator': 'equals', 'value': '홍길동'}, c['filter_clauses'])
        self.assertIn({'field': 'effort_person', 'operator': 'equals', 'value': '석지원'}, c['filter_clauses'])

    def test_empty_partial_ranking_keeps_collection_warning(self):
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(None)), \
             patch.object(effort, 'fetch_mantis_issues', side_effect=lambda p: [issue(p['page'], '', 1)]):
            result = effort.run_effort_ranking_report('10월 공수 순위', 2026, 10)
        self.assertFalse(result['retrieval']['stages'][0]['meta']['search_complete'])
        self.assertIn('누락', result['content'])

    def test_followup_executes_confirmed_period_without_reparsing_question(self):
        from backend.intent.confirmed_context import confirmed_constraints_scope
        from backend.schemas import ChatRequest
        from backend.services.contracts import ChatPipelineContext
        from backend.services.unified_planning import execute_plan
        first = validate_plan('2026년 10월 내 공수', proposal(), {'mantisUsername': '석지원'}, {})
        p = proposal(scope='inherit', followup=True, filters=[
            {'field': 'delivered_at', 'operator': 'between', 'value': ['2026-09-01', '2026-09-30'], 'evidence': '9월'}],
            status={'mode': 'inherit', 'values': [], 'evidence': ''})
        plan = validate_plan('9월로 바꿔줘', p, {'mantisUsername': '석지원'}, first)
        q = '9월로 바꿔줘'
        c = plan['resolved_constraints']
        ctx = ChatPipelineContext(q, 'test', 'key', [], q, {'mantis': True}, [], c, 5, intent_plan=plan)
        rows = IssueBatch([issue(1, '2026-09-30', .75), issue(2, '2026-10-01', 10)], {'search_complete': True})
        with confirmed_constraints_scope(q, c), \
             patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(rows)), \
             patch.object(effort, 'fetch_mantis_issues', side_effect=lambda p: list(rows) if p['page'] == 1 else []):
            result = execute_plan(ChatRequest(modelId='test', messages=[{'role': 'user', 'content': q}]), ctx, {'mantisUsername': '석지원'})
        self.assertEqual(result['effortChart']['totalEffort'], .75)
        self.assertEqual([s['issue_id'] for s in result['sources']], [1])
        self.assertEqual(c, plan['resolved_constraints'])

    def test_explicit_owner_remains_distinct_from_effort_contributor(self):
        from backend.sources.mantis import issue_matches_constraints
        p = proposal(scope='target', filters=[
            {'field': 'owner', 'operator': 'equals', 'value': '홍길동', 'evidence': '홍길동 담당'},
            {'field': 'effort_person', 'operator': 'equals', 'value': '석지원', 'evidence': '석지원 공수'},
            {'field': 'delivered_at', 'operator': 'between', 'value': ['2026-10-01', '2026-10-31'], 'evidence': '2026년 10월'}])
        c = validate_plan('홍길동 담당 이슈의 2026년 10월 석지원 공수', p, {}, {})['resolved_constraints']
        self.assertTrue(issue_matches_constraints(issue(1, '2026-10-01', .5, handler='홍길동'), c))
        self.assertFalse(issue_matches_constraints(issue(2, '2026-10-01', .5, handler='김철수'), c))
        wrong = deepcopy(issue(3, '2026-10-01', .5, handler='홍길동'))
        wrong['custom_fields'][1]['value'] = '홍길동(1)'
        self.assertFalse(issue_matches_constraints(wrong, c))

    def test_ranking_uses_delivery_period_and_keeps_source_evidence(self):
        rows = IssueBatch([issue(1, '2026-10-01', .75), issue(2, '', 100, created='2026-10-02')],
                          {'search_complete': True})
        with patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(rows)):
            result = effort.run_effort_ranking_report('10월 공수 순위', 2026, 10, ascending=True)
        self.assertEqual(result['effortRanking']['topPeople'], [
            {'label': '석지원', 'amount': .75}, {'label': '다른사람', 'amount': 9}])
        self.assertEqual([s['issue_id'] for s in result['sources']], [1])

    def test_trend_assigns_effort_to_delivery_month_and_counts_to_registration_month(self):
        import tempfile
        from pathlib import Path
        from test_effort_trend import issue_document
        from backend.reports.effort_trend import run_effort_trend_report
        from backend.tools.mantis_index import build_mantis_index
        first = issue_document(1, created_at='2026-09-01', issue_type='오류',
                               effort_line='- 3PC공수: 석지원(0.75), 다른사람(9)')
        first = first.replace('고객사전달일: 2026-09-01', '고객사전달일: 2026-10-01')
        second = issue_document(2, created_at='2026-10-02', delivered_at='', issue_type='오류',
                                effort_line='- 3PC공수: 석지원(100)')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index.sqlite3'
            build_mantis_index(path, [first, second])
            result = run_effort_trend_report('두 달 추이', months=2, index_path=path,
                end_year=2026, end_month=10, constraints={'filter_clauses': [
                    {'field': 'effort_person', 'operator': 'equals', 'value': '석지원'}]})
        series = next(r for r in result['effortTrend']['series'] if r['label'] == '오류')
        self.assertEqual(series['counts'], [1, 1])
        self.assertEqual(series['efforts'], [0, .75])

    def test_verification_checks_delivery_date_and_contributor_independently(self):
        from backend.evaluation.verification import build_verification_report
        from backend.intent.confirmed_context import confirmed_constraints_scope
        q = '2026년 10월 내 공수'
        plan = validate_plan(q, proposal(), {'uid': 'user1', 'mantisUsername': '석지원'}, {})
        rows = IssueBatch([issue(1, '2026-10-01', .75)], {'search_complete': True})
        with confirmed_constraints_scope(q, plan['resolved_constraints']), \
             patch('backend.sources.mantis_store.stream_complete_store', side_effect=lambda: nullcontext(rows)):
            result = effort.build_effort_report_from_unified_plan(q, plan)
            result['retrieval']['query_plan'] = plan
            checked = build_verification_report(q, result['content'], result['sources'], result['retrieval'], result['raw']['mode'])
            checks = {c['id']: c for d in checked['dimensions'] for c in d['checks']}
            self.assertEqual(checks['constraints']['status'], 'pass')
            self.assertEqual(checks['effort_recalculation']['status'], 'pass')
            result['sources'][0]['detail']['date_fields']['고객사전달일'] = '2026-11-01'
            bad = build_verification_report(q, result['content'], result['sources'], result['retrieval'], result['raw']['mode'])
        self.assertEqual(bad['status'], 'fail')
