"""모델 계획·보정·도구·최종 표시에 같은 요청 개수를 적용한다."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from backend.intent.execution_contract import validate_execution_contract
from backend.intent.unified_validation import validate_plan
from backend.services.agent.planning import PlanningSession
from backend.tools.conditions import project_conditions
from backend.tools.query_conditions import build_query_contract
from test_tool_condition_arguments import PROPOSAL


def followup_session(customer='한투', tail='전체 건수와 최근 3건을 다시 보여줘.'):
    original = '전사 NH 미해결 이슈 최근 3건과 두 번째 이슈의 Notion 가이드를 설명해줘.'
    previous_list = validate_plan(original, {
        **deepcopy(PROPOSAL), 'source': 'both', 'operation': 'explain',
        'filters': [{'field': 'customer', 'operator': 'equals', 'value': 'NH', 'evidence': 'NH'}]}, {}, {})
    previous = {'source': 'mantis', 'operation': 'get', 'limit': None,
                'resolved_constraints': {'issue_ids': [43]},
                'list_context': {'audit_id': 'a' * 32, 'question': original, 'plan': previous_list}}
    question = f'고객사만 {customer}로 바꿔서 {tail}'
    proposal = {**deepcopy(PROPOSAL), 'scope': 'inherit', 'followup': True, 'followup_context': 'list',
                'filters': [{'field': 'customer', 'operator': 'equals', 'value': customer, 'evidence': customer}],
                'status': {'mode': 'inherit', 'values': [], 'evidence': ''}}
    return PlanningSession(question, {}, previous, {'turns': [{'question': original}]}), proposal


def answer_fixture(plan, ids, total):
    content = f'적용 조건에 맞는 Mantis 이슈는 {total}건입니다.\n' + '\n'.join(
        f'{i}. #{issue_id} 제목 {issue_id}' for i, issue_id in enumerate(ids, 1))
    stage = {'source': 'mantis_snapshot', 'status': 'ok',
             'issues_list': [{'id': n, 'summary': f'제목 {n}'} for n in ids],
             'meta': {'search_complete': True, 'found_count': total,
                      'matched_issue_ids': list(range(41, 41 + total)), 'ordered_matched_ids': ids,
                      'query_result': {'operation': 'list', 'total_count': total, 'ordered_ids': ids}}}
    sources = [{'type': 'mantis', 'issue_id': n, 'title': f'제목 {n}'} for n in ids]
    return content, sources, {'query_plan': plan, 'constraints': plan['resolved_constraints'],
                              'query': plan['grounding_text'], 'stages': [stage]}


class AgentDisplayLimitTests(unittest.TestCase):
    def test_customer_change_cannot_replace_three_with_two_hundred_during_repair(self):
        cases = [json.loads(line) for line in (Path(__file__).parent / 'fixtures/routing_cases.jsonl').read_text().splitlines()]
        for case in cases:
            if 'agent_display_customer' not in case:
                continue
            with self.subTest(customer=case['agent_display_customer']):
                session, proposal = followup_session(case['agent_display_customer'])
                self.assertEqual(session.question, case['q'])
                proposal['limit'] = 200
                rejected = session.submit(proposal=proposal)
                self.assertFalse(rejected['ok'], rejected)
                self.assertIn('표시 개수', rejected['error']['message'])
                self.assertFalse(session.submit(changes={'limit': 5})['ok'])
                self.assertEqual(session.candidate, proposal)
                self.assertTrue(session.submit(changes={'limit': 3})['ok'], session.error)
                self.assertEqual(session.plan['limit'], 3)
                constraints = session.plan['resolved_constraints']
                self.assertEqual(constraints['customer_key'], {'KB': 'kb'}.get(case['agent_display_customer'], case['agent_display_customer']))
                self.assertEqual(constraints['statuses'], ['open'])
                self.assertEqual(constraints['issue_ids'], [])
                self.assertEqual(constraints['mantis_team_scope'], 'all')

    def test_explicit_size_change_all_results_and_inherited_size_remain_available(self):
        for tail, limit in [('전체 건수와 최근 5건을 보여줘.', 5), ('목록을 모두 보여줘.', 200),
                            ('전체 건수와 목록을 다시 보여줘.', 3)]:
            session, proposal = followup_session(tail=tail)
            self.assertTrue(session.submit(proposal={**proposal, 'limit': limit})['ok'], session.error)
            self.assertEqual(session.plan['limit'], limit)
            self.assertEqual(session.plan['resolved_constraints']['statuses'], ['open'])

    def test_display_size_is_checked_without_conversation_summary(self):
        question = '전사 한투 미해결 이슈 최근 3건을 보여줘.'
        with self.assertRaisesRegex(ValueError, '표시 개수'):
            validate_plan(question, {**deepcopy(PROPOSAL), 'limit': 200}, {}, {})

    def test_validated_plan_cannot_change_display_size_before_tool_execution(self):
        session, proposal = followup_session()
        self.assertTrue(session.submit(proposal=proposal)['ok'])
        plan = deepcopy(session.plan)
        plan['limit'] = 200
        self.assertFalse(validate_execution_contract(session.question, plan)['valid'])
        conditions = project_conditions(plan, plan['resolved_constraints'], 'mantis')
        with self.assertRaises(ValueError):
            build_query_contract(session.question, plan, conditions, 5, {})

    def test_final_gate_uses_question_even_when_plan_and_rows_agree_on_five(self):
        from backend.evaluation.verification import build_verification_report
        from backend.evaluation.delivery import decide_delivery
        session, proposal = followup_session()
        self.assertTrue(session.submit(proposal=proposal)['ok'])
        plan = deepcopy(session.plan)
        plan['limit'] = 200
        content, sources, retrieval = answer_fixture(plan, [45, 44, 43, 42, 41], 5)
        report = build_verification_report(session.question, content, sources, retrieval, 'agent_query_results')
        check = next(c for c in report['checks'] if c['id'] == 'agent_query_result')
        self.assertEqual(check['status'], 'fail', check)
        self.assertIn('agent_query_result', decide_delivery(report).blocked_checks)

    def test_zero_and_fewer_results_keep_the_true_total_and_valid_display(self):
        from backend.evaluation.verification import build_verification_report
        session, proposal = followup_session()
        self.assertTrue(session.submit(proposal=proposal)['ok'])
        for ids, total in [([], 0), ([42, 41], 2), ([45, 44, 43], 5)]:
            with self.subTest(total=total):
                content, sources, retrieval = answer_fixture(session.plan, ids, total)
                report = build_verification_report(session.question, content, sources, retrieval, 'agent_query_results')
                check = next(c for c in report['checks'] if c['id'] == 'agent_query_result')
                self.assertEqual(check['status'], 'pass', check)

    def test_total_count_plan_keeps_a_separate_requested_display_size(self):
        for question in ('전사 한투 미해결 이슈 전체 건수와 최근 3건을 보여줘.',
                         '전사 한투 미해결 이슈 최신 150건 기준으로 전체 건수와 최근 3건을 보여줘.'):
            with self.subTest(question=question):
                plan = validate_plan(question, {**deepcopy(PROPOSAL), 'operation': 'count', 'limit': None}, {}, {})
                conditions = project_conditions(plan, plan['resolved_constraints'], 'mantis')
                self.assertEqual(build_query_contract(question, plan, conditions, 5, {}).list_limit, 3)
                self.assertIsNone(plan['limit'])

    def test_single_item_request_planned_as_get_uses_the_same_display_size_as_verification(self):
        from backend.intent.display_contract import expected_display_limit
        question = '전사 한투 미해결 이슈 중 가장 최근 이슈 하나만 자세히 알려줘.'
        for operation in ('get', 'explain', 'search'):
            with self.subTest(operation=operation):
                plan = validate_plan(question, {**deepcopy(PROPOSAL), 'operation': operation, 'limit': None}, {}, {})
                conditions = project_conditions(plan, plan['resolved_constraints'], 'mantis')
                contract = build_query_contract(question, plan, conditions, 5, {})
                self.assertEqual(contract.list_limit, 1)
                self.assertEqual(contract.list_limit, expected_display_limit(question, plan))

    def test_display_is_not_capped_at_twenty_or_confused_with_population_or_notion(self):
        cases = [('전사 한투 미해결 이슈 최근 50건을 보여줘.', 50, None),
                 ('전사 한투 미해결 이슈 최신 150건 기준으로 최근 3건을 보여줘.', 3, 150),
                 ('전사 한투 미해결 이슈 최근 3건과 Notion 가이드 2개를 보여줘.', 3, None)]
        for question, wanted, population in cases:
            with self.subTest(question=question):
                plan = validate_plan(question, {**deepcopy(PROPOSAL), 'limit': wanted}, {}, {})
                conditions = project_conditions(plan, plan['resolved_constraints'], 'mantis')
                contract = build_query_contract(question, plan, conditions, 5, {})
                self.assertEqual(contract.list_limit, wanted)
                self.assertEqual(contract.population_limit, population)

    def test_quoted_title_number_does_not_change_the_display_contract(self):
        question = '전사 한투 미해결 이슈 중 제목에 "최근 3건"이 포함된 목록을 보여줘.'
        proposal = {**deepcopy(PROPOSAL), 'limit': 5}
        proposal['filters'].append({'field': 'title', 'operator': 'contains', 'value': '최근 3건', 'evidence': '최근 3건'})
        plan = validate_plan(question, proposal, {}, {})
        conditions = project_conditions(plan, plan['resolved_constraints'], 'mantis')
        self.assertEqual(build_query_contract(question, plan, conditions, 5, {}).list_limit, 5)


if __name__ == '__main__':
    unittest.main()
