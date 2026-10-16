"""설명 서술과 검색 조건의 경계가 실제 조회 범위를 바꾸는 회귀를 검사한다."""

import unittest
from unittest.mock import patch

from backend.intent.explanation import explanation_constraints, wants_issue_explanation
from backend.query_planning import build_query_plan, infer_operation
from backend.sources.mantis import issue_matches_constraints
from backend.understanding import extract_query_constraints


class IntentBoundaryRegressions(unittest.TestCase):
    def test_copular_handler_names_are_applied_to_actual_rows(self):
        for name, suffix, customer in [
            ('석지원', '인', '유안타증권'),
            ('이영희', '이고 종료 상태인', '유진증권'),
            ('홍길동', '이며 종료 상태인', '유진증권'),
        ]:
            query = (f'전사 기준 2026년 6월 {customer} 등록 이슈 중 담당자가 '
                     f'{name}{suffix} 건은 총 몇 건이야? 번호와 제목을 전부 보여줘.')
            with self.subTest(name=name), patch('backend.intent.entities.load_known_people', return_value=((name,), ())):
                constraints = extract_query_constraints(query)
                self.assertEqual(constraints['people']['names'], [name])
                self.assertEqual(constraints['people']['role'], 'handler')
                plan = build_query_plan(query, {'source': 'mantis'}, constraints)
                self.assertEqual(plan['filters']['people']['names'], [name])
                row = {'id': 101, 'summary': '등록 이슈', 'project': {'name': customer},
                       'created_at': '2026-06-10', 'status': {'name': 'closed'},
                       'handler': {'real_name': name}}
                self.assertTrue(issue_matches_constraints(row, constraints))
                self.assertFalse(issue_matches_constraints({**row, 'handler': {'real_name': '다른담당자'}}, constraints))
                self.assertFalse(issue_matches_constraints({**row, 'created_at': '2026-07-10'}, constraints))

    def test_unknown_explicit_copular_name_and_real_name_ending_in_in(self):
        for roster, query, expected in [
            ((), '담당자가 홍길동인 이슈 보여줘', '홍길동'),
            ((), '담당자가 홍길동이고 종료된 이슈 보여줘', '홍길동'),
            ((), '담당자 김지인 항목 찾아줘', '김지인'),
            (('김서인',), '담당자 김서인 이슈 보여줘', '김서인'),
            (('남궁서인',), '담당자가 남궁서인 이슈 보여줘', '남궁서인'),
            (('이지은',), '담당자 이지은 이슈 보여줘', '이지은'),
        ]:
            with self.subTest(query=query), patch('backend.intent.entities.load_known_people', return_value=(roster, ())):
                self.assertEqual(extract_query_constraints(query)['people']['names'], [expected])

    def test_separate_words_do_not_form_count_nouns(self):
        queries = [
            'KB 선옵포지션에서 미니 옵션 잔고가 잘못 보이던 건 수정본을 보낸 뒤에도 왜 같은 증상이 남았어? 서버 조치와 소스 버전 확인 내용을 구분해줘.',
            'NH에서 전달한 건 수신됐는지 왜 확인이 필요했어?',
            'IBK에서 장애 두 개 수정한 뒤 왜 다시 재현됐어?',
        ]
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(infer_operation(query), 'search')
                self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))
        for query in ['KB 등록 건수 알려줘', 'KB 등록건수와 이슈 번호를 보여줘',
                      'NH 이슈 개수는?', 'NH 이슈 몇 개야?', '이슈 총 몇 건이야?',
                      'IBK에서 수정본을 보낸 건 중 종료된 이슈는 몇 건이야?']:
            with self.subTest(query=query):
                self.assertEqual(infer_operation(query), 'count')
                self.assertFalse(wants_issue_explanation(query, extract_query_constraints(query)))

    def test_last_review_and_delivery_state_routes_to_explanation(self):
        query = ('NH 3600 해외주식 잔고 전문 변경에서 필드 순서까지 바뀐 거야? '
                 '소수점 거래량 표시 규칙과 마지막 검수·전달 상태도 함께 정리해줘.')
        self.assertTrue(wants_issue_explanation(query, extract_query_constraints(query)))
        count_query = query + ' 관련 이슈는 총 몇 건이야?'
        self.assertFalse(wants_issue_explanation(count_query, extract_query_constraints(count_query)))

    def test_space_separated_count_nouns_keep_count_plan(self):
        for query in ['이슈 건 수 알려줘', '이슈 개 수 알려줘', 'KB 등록 건 수 알려줘',
                      'KB 미종료 건 수와 이슈 번호를 알려줘', 'KB 이슈 개 수는?',
                      'KB 이슈 건 수', 'KB 이슈 건 수를 알려줘']:
            with self.subTest(query=query):
                self.assertEqual(infer_operation(query), 'count')
                constraints = extract_query_constraints(query)
                plan = build_query_plan(query, {'source': 'mantis'}, constraints)
                self.assertEqual(plan['operation'], 'count')
                self.assertFalse(wants_issue_explanation(query, constraints))

    def test_runtime_termination_cause_does_not_filter_closed_issues(self):
        query = ('IBK에서 전략 탭을 종합차트에 넣는 작업은 왜 바로 배포 못 했어? '
                 '전략을 해제할 때 종료되는 원인과 버튼 글자 겹침 원인을 구분하고 마지막 서버 재배포 일정도 확인해줘.')
        original = extract_query_constraints(query)
        corrected = explanation_constraints(query, original)
        self.assertTrue(wants_issue_explanation(query, original))
        self.assertEqual(corrected['statuses'], [])
        self.assertFalse(any(clause['field'] == 'status' for clause in corrected['filter_clauses']))
        for query in ['IBK 종료된 이슈 중 전략 해제할 때 종료되는 원인을 설명해줘',
                      'IBK 상태=종료 이슈에서 종료되는 원인을 설명해줘']:
            with self.subTest(query=query):
                constraints = extract_query_constraints(query)
                self.assertEqual(explanation_constraints(query, constraints), constraints)
                self.assertEqual(constraints['statuses'], ['closed'])
        inherited = {**original, 'inherited_from_audit_id': 'confirmed-scope'}
        self.assertEqual(explanation_constraints(query, inherited), inherited)
