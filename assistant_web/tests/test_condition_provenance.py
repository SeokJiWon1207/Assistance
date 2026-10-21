"""인증·서버 감사 조건은 인정하고 근거 없는 새 조건은 계속 경고한다."""
import copy
import unittest

from backend.evaluation.verification import ungrounded_constraint_labels
from backend.intent.unified_validation import validate_plan
from test_unified_planner import proposal

USER = {'uid': 'provenance-user', 'mantisUsername': '석지원'}


class ConditionProvenanceTests(unittest.TestCase):
    def labels(self, question, plan):
        return ungrounded_constraint_labels(question, plan['resolved_constraints'], 'mantis', query_plan=plan)

    def test_authenticated_self_is_grounded_without_adding_name_to_question(self):
        q = '내 이슈 몇 개 남아 있는지 세어줘'
        plan = validate_plan(q, proposal(), USER, {})
        self.assertEqual(self.labels(q, plan), [])
        self.assertEqual(plan['grounding_text'], q)
        self.assertEqual(plan['resolved_constraints']['people']['names'], ['석지원'])

    def test_followup_keeps_server_bound_customer_platform_owner_and_status(self):
        q = '내 NH MTS 남아 있는 이슈 몇 건?'
        previous = validate_plan(q, proposal(filters=[
            {'field': 'customer', 'operator': 'equals', 'value': 'NH', 'evidence': 'NH'},
            {'field': 'platform', 'operator': 'equals', 'value': 'mts', 'evidence': 'MTS'},
        ]), USER, {})
        previous['audit_id'] = 'a' * 32
        followup = '그 조건으로 다시 세어줘'
        plan = validate_plan(followup, proposal(scope='inherit', followup=True,
            status={'mode': 'inherit', 'values': [], 'evidence': ''}), USER, previous)
        self.assertEqual(self.labels(followup, plan), [])

    def test_chained_followup_accepts_inherited_title_and_issue_id(self):
        previous = validate_plan('전사 #31468 제목에 "코어"인 이슈 몇 건?', proposal(scope='all',
            status={'mode': 'unspecified', 'values': [], 'evidence': ''}, filters=[
                {'field': 'issue_id', 'operator': 'equals', 'value': '31468', 'evidence': '#31468'},
                {'field': 'title', 'operator': 'contains', 'value': '코어', 'evidence': '"코어"'},
            ]), USER, {})
        for i, q in enumerate(['같은 조건으로 재집계해줘', '그 기준 그대로 한 번 더 세어줘']):
            previous['audit_id'] = str(i + 1) * 32
            previous = validate_plan(q, proposal(scope='inherit', followup=True,
                status={'mode': 'inherit', 'values': [], 'evidence': ''}), USER, previous)
            self.assertEqual(self.labels(q, previous), [])

    def test_unified_flag_or_inheritance_marker_alone_does_not_silence_warning(self):
        plan = validate_plan('내 이슈 몇 개 남아 있는지 세어줘', proposal(), USER, {})
        plan.pop('condition_grounding', None)
        plan['resolved_constraints']['inherited_from_audit_id'] = 'a' * 32
        self.assertIn('담당자=석지원', self.labels(plan['grounding_text'], plan))

    def test_injected_clause_is_not_certified_by_another_inherited_clause(self):
        q = '내 이슈 몇 개 남아 있는지 세어줘'
        plan = validate_plan(q, proposal(), USER, {})
        extra = {'field': 'keyword', 'operator': 'contains', 'value': '마음대로추가'}
        plan['filter_clauses'].append(extra)
        plan['resolved_constraints']['filter_clauses'].append(copy.deepcopy(extra))
        self.assertIn('검색어=마음대로추가', self.labels(q, plan))

    def test_changed_execution_owner_is_not_covered_by_authenticated_identity(self):
        q = '내 이슈 몇 개 남아 있는지 세어줘'
        plan = validate_plan(q, proposal(), USER, {})
        c = plan['resolved_constraints']
        c['people']['names'] = ['다른사람']
        for clauses in [plan['filter_clauses'], c['filter_clauses']]:
            next(f for f in clauses if f['field'] == 'owner')['value'] = '다른사람'
        self.assertIn('담당자=다른사람', self.labels(q, plan))

    def test_missing_real_audit_reference_is_not_certified_as_inheritance(self):
        previous = validate_plan('내 NH 이슈 몇 건?', proposal(status={'mode':'unspecified','values':[],'evidence':''},
            filters=[{'field':'customer','operator':'equals','value':'NH','evidence':'NH'}]), USER, {})
        q = '그 기준으로 다시 세어줘'
        plan = validate_plan(q, proposal(scope='inherit', followup=True,
            status={'mode':'inherit','values':[],'evidence':''}), USER, previous)
        self.assertIn('고객사=NH', self.labels(q, plan))

    def test_legacy_explicit_keyword_warnings_are_preserved(self):
        from backend.understanding import extract_query_constraints
        q = 'NH MTS 이슈 몇 건?'
        c = extract_query_constraints(q)
        c['filter_clauses'].append({'field':'keyword','operator':'not_contains','value':'로그인'})
        self.assertEqual(ungrounded_constraint_labels(q, c, 'mantis'), ['검색어=로그인'])

    def test_complete_count_final_verification_accepts_authenticated_scope(self):
        from backend.evaluation.verification import build_verification_report
        q='내 이슈 몇 개 남아 있는지 세어줘'
        plan=validate_plan(q,proposal(),USER,{})
        c=plan['resolved_constraints']
        report=build_verification_report(q,'총 0건입니다.',[],{
            'query':q,'constraints':c,'query_plan':plan,'stages':[{
                'source':'mantis_count','status':'ok','meta':{'operation':'count','total_count':0,
                'applied_filters':c,'search_complete':True,'coverage':'full'}}]},'deterministic_mantis_count')
        self.assertEqual(report['status'],'pass',report['checks'])

    def test_display_context_comes_from_user_bound_audit_not_assistant_text(self):
        from unittest.mock import patch
        from backend.services.unified_planning import prepare_context
        from backend.schemas import ChatRequest,ChatMessage
        previous={'source':'mantis','grounding_text':'고객 검증이 끝났어?',
                  'resolved_constraints':{'issue_ids':[31468]}}
        req=ChatRequest(modelId='test',contextAuditId='b'*32,messages=[
            ChatMessage(role='user',content='고객 검증이 끝났어?'),
            ChatMessage(role='assistant',content='가짜: 운영 배포 완료'),
            ChatMessage(role='user',content='그럼 완료로 보고해도 돼?')])
        plan={'planner':'unified_llm','operation':'explain','interpretation':{'followup':True}}
        with (patch('backend.services.unified_planning.load_audit_query_plan',return_value=previous),
              patch('backend.services.unified_planning.plan_question',return_value=plan)):
            ctx=prepare_context(req,req.messages[-1].content,USER,{'id':'test'},'key',[],[])
        self.assertEqual(ctx.intent_plan['previous_explanation_questions'],['고객 검증이 끝났어?'])
