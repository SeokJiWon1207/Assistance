"""리뷰에서 재현한 긴 근거와 비교 후속의 과업 보존."""
from unittest import TestCase

from backend.reports import comparison_facts, similar
from backend.intent.unified_validation import validate_plan
from test_unified_planner import proposal


class LongComparisonPassageTests(TestCase):
    def issue(self):
        text = '현장 검사 ' + '동일 환경에서 장시간 확인 ' * 35 + ' 원인: 객체 해제 누락 때문에 메모리 누수 발생 확인됨, 객체 삭제 수정함'
        return {'id': 101, 'summary': '차트 화면 메모리 누수', 'notes': [{'id': 7, 'text': text}]}

    def test_long_note_tail_is_available_to_model_with_its_own_note_reference(self):
        rows = comparison_facts._catalog(self.issue())
        selected = [row for row in rows if row['field'] == 'note:7']
        self.assertTrue(selected)
        self.assertTrue(any('객체 해제 누락' in row['text'] for row in selected))
        self.assertTrue(all(len(row['text']) <= 450 for row in selected))
        self.assertEqual(len({row['id'] for row in rows}), len(rows))

    def test_fallback_excerpt_keeps_cause_and_actual_action_at_end_of_long_note(self):
        cause = similar._recorded_cause(self.issue())[0]
        action = similar._recorded_fix(self.issue())[0]
        self.assertIn('객체 해제 누락', cause)
        self.assertIn('객체 삭제 수정함', action)
        self.assertIn('노트 7', cause)
        self.assertIn('노트 7', action)

    def test_long_request_is_not_promoted_to_completed_action(self):
        issue = self.issue()
        issue['notes'][0]['text'] = '수정 요청: ' + '로그 확인 ' * 100 + '객체 삭제를 수정해주세요'
        self.assertEqual(similar._recorded_fix(issue), ('', ''))


class ComparisonTaskContinuationTests(TestCase):
    def previous(self):
        return validate_plan('#9000과 과거 CPU 사례의 원인과 조치를 비교해줘', proposal(
            operation='report', report_kind='similar', scope='target',
            status={'mode':'unspecified','values':[],'evidence':''},
            filters=[{'field':'issue_id','operator':'equals','value':9000,'evidence':'#9000'}],
            terms=['CPU']), {'mantisUsername':'석지원'}, {})

    def test_condition_only_followup_rejects_search_or_get_instead_of_comparison(self):
        question = '기준과 CPU는 그대로 두고 KB증권 사례로만 좁혀줘'
        for operation in ['search','get','explain']:
            with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, '비교'):
                validate_plan(question, proposal(operation=operation, scope='inherit', followup=True,
                    status={'mode':'inherit','values':[],'evidence':''}, filters=[
                        {'field':'customer','operator':'equals','value':'KB증권','evidence':'KB증권'}]),
                    {'mantisUsername':'석지원'}, self.previous())

    def test_explicit_switch_to_base_detail_is_allowed(self):
        plan = validate_plan('비교는 끝내고 기준 이슈 하나만 상세 조회해줘', proposal(
            operation='get', scope='inherit', followup=True,
            status={'mode':'inherit','values':[],'evidence':''}), {'mantisUsername':'석지원'}, self.previous())
        self.assertEqual(plan['operation'], 'get')
        self.assertEqual(plan['resolved_constraints']['issue_ids'], [9000])

    def test_explicit_base_continuation_cannot_drop_followup_flag(self):
        with self.assertRaisesRegex(ValueError, '비교'):
            validate_plan('기준과 CPU는 그대로 두고 KB증권 사례로만 좁혀줘', proposal(
                operation='search', scope='all', followup=False,
                status={'mode':'unspecified','values':[],'evidence':''}, filters=[
                    {'field':'customer','operator':'equals','value':'KB증권','evidence':'KB증권'}]),
                {'mantisUsername':'석지원'}, self.previous())


class DocumentTaskContinuationTests(TestCase):
    def test_agenda_format_change_cannot_replace_a_postmortem_with_customer_notice(self):
        previous = validate_plan('#9000 이슈 사후 분석을 다섯 줄로 작성해줘', proposal(
            operation='report', report_kind='postmortem', scope='target',
            status={'mode':'unspecified','values':[],'evidence':''}, filters=[
                {'field':'issue_id','operator':'equals','value':9000,'evidence':'#9000'}]),
            {'mantisUsername':'석지원'}, {})
        with self.assertRaisesRegex(ValueError, '문서'):
            validate_plan('같은 이슈와 근거로 회의 안건 형식으로 바꿔줘', proposal(
                operation='report', report_kind='notice', scope='inherit', followup=True,
                status={'mode':'inherit','values':[],'evidence':''}), {'mantisUsername':'석지원'}, previous)
        correct = validate_plan('같은 이슈와 근거로 회의 안건 형식으로 바꿔줘', proposal(
            operation='report', report_kind='postmortem', scope='inherit', followup=True,
            status={'mode':'inherit','values':[],'evidence':''}), {'mantisUsername':'석지원'}, previous)
        self.assertEqual(correct['report_kind'], 'postmortem')
        notice = validate_plan('같은 이슈를 고객 안내 메일로 바꿔줘', proposal(
            operation='report', report_kind='notice', scope='inherit', followup=True,
            status={'mode':'inherit','values':[],'evidence':''}), {'mantisUsername':'석지원'}, previous)
        self.assertEqual(notice['report_kind'], 'notice')

    def test_explicit_retrospective_document_cannot_be_a_general_explanation(self):
        question = '#9000 검수 내용을 바탕으로 회고 내용을 5줄 요약으로 보여줘'
        bad = proposal(operation='explain', scope='target',
            status={'mode':'unspecified','values':[],'evidence':''}, filters=[
                {'field':'issue_id','operator':'equals','value':9000,'evidence':'#9000'}])
        with self.assertRaisesRegex(ValueError, '문서'):
            validate_plan(question, bad, {'mantisUsername':'석지원'}, {})
        bad.update(operation='report',report_kind='postmortem')
        self.assertEqual(validate_plan(question,bad,{'mantisUsername':'석지원'}, {})['report_kind'],'postmortem')

    def test_weekly_retrospective_without_an_issue_id_keeps_its_existing_report(self):
        plan = validate_plan('지난주 우리 팀 회고를 작성해줘', proposal(
            operation='report',report_kind='weekly',scope='team',
            status={'mode':'unspecified','values':[],'evidence':''}), {'mantisUsername':'석지원'}, {})
        self.assertEqual(plan['report_kind'],'weekly')
