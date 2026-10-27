"""일반 질문은 사내 검색·이슈 근거 생성 경로를 실행하지 않는다."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.intent.constraints import extract_query_constraints
from backend.intent.context import build_contextual_retrieval_text
from backend.intent.explanation import wants_issue_explanation
from backend.intent.source_router import rule_based_search_intent, should_run_retrieval
from backend.schemas import ChatMessage, ChatRequest


def general_cases():
    fixture = Path(__file__).parent / 'fixtures/routing_cases.jsonl'
    return [case for line in fixture.read_text().splitlines()
            if (case := json.loads(line)).get('general_chat')]


class GeneralRoutingTests(unittest.TestCase):
    def test_general_questions_do_not_search_even_when_they_contain_work_keywords(self):
        for case in general_cases():
            with self.subTest(question=case['q']):
                constraints = extract_query_constraints(case['q'])
                self.assertEqual(rule_based_search_intent(case['q'], constraints)['source'], 'general')
                self.assertFalse(should_run_retrieval(case['q'], constraints))
                self.assertFalse(wants_issue_explanation(case['q'], constraints))

    def test_independent_general_question_does_not_inherit_previous_issue(self):
        question = '그럼 파이썬 TypeError 오류를 고치는 방법 알려줘'
        messages = [ChatMessage(role='user', content='NH MTS #31105 이슈 보여줘'),
                    ChatMessage(role='assistant', content='이슈 조회 결과'),
                    ChatMessage(role='user', content=question)]
        self.assertEqual(build_contextual_retrieval_text(messages), (question, False))

    def test_issue_followup_keeps_its_target_when_asking_for_an_explanation(self):
        question = '그 이슈가 무엇인지 설명해줘'
        messages = [ChatMessage(role='user', content='NH MTS #31105 이슈 보여줘'),
                    ChatMessage(role='assistant', content='이슈 조회 결과'),
                    ChatMessage(role='user', content=question)]
        text, rewrite = build_contextual_retrieval_text(messages)
        self.assertIn('31105', text)
        self.assertFalse(rewrite)
        self.assertTrue(wants_issue_explanation(text, extract_query_constraints(text)))

    def test_general_plan_uses_normal_generation_in_both_planners(self):
        import backend.services.chat_service as service
        question = 'NH투자증권이 어떤 회사인지 일반적으로 설명해줘'
        for unified in ('true', 'false'):
            with (
                self.subTest(unified=unified),
                patch.dict('os.environ', {'UNIFIED_QUERY_PLANNER_ENABLED': unified}),
                patch.object(service, 'get_private_model', return_value={'id': 'test', 'status': 'active'}),
                patch.object(service, 'get_model_api_key', return_value='key'),
                patch('backend.intent.unified._propose', return_value={
                    'source': 'general', 'operation': 'explain', 'scope': 'default', 'followup': False,
                    'filters': [], 'status': {'mode': 'unspecified', 'values': [], 'evidence': ''},
                    'clear_fields': [], 'terms': [], 'group_by': None, 'limit': None,
                }),
                patch.object(service, 'generate_model_response', return_value={'content': '일반 회사 소개', 'sources': [], 'retrieval': None}),
                patch('backend.services.grounded_explanation.generate_grounded_explanation',
                      side_effect=AssertionError('일반 질문에 이슈 근거 생성을 실행함')),
                patch.object(service, 'run_retrieval_pipeline', side_effect=AssertionError('일반 질문에 사내 검색을 실행함')),
                patch.object(service, 'persist_chat_response', side_effect=lambda request, text, result, **kw: result),
            ):
                request = ChatRequest(modelId='test', messages=[ChatMessage(role='user', content=question)])
                result = service.chat_service.build_admitted_response(request, user={'uid': 'test', 'mantisUsername': '석지원'})
                self.assertEqual(result['content'], '일반 회사 소개')
                self.assertEqual(result['sources'], [])
                self.assertIsNone(result.get('retrieval'))
                from backend.evaluation.verification import build_verification_report
                checks = build_verification_report(question, result['content'], [], result.get('retrieval'), 'model')['checks']
                evidence = next(check for check in checks if check['id'] == 'evidence')
                self.assertEqual(evidence['status'], 'pass')

    def test_general_drafting_plan_cannot_dispatch_an_internal_issue_report(self):
        from backend.intent.scope_extraction import empty_query_constraints
        from backend.services.contracts import ChatPipelineContext
        from backend.services.unified_planning import execute_plan
        question = '친구에게 생일 축하 메일 써줘'
        context = ChatPipelineContext(question, {}, '', [], question, {}, [], empty_query_constraints(), 5,
            intent_plan={'source': 'general', 'operation': 'report', 'report_kind': 'notice'})
        request = ChatRequest(modelId='test', messages=[ChatMessage(role='user', content=question)])
        with patch('backend.reports.issue_documents.build_issue_document_report',
                   side_effect=AssertionError('일반 메일 작성에 이슈 문서 생성을 실행함')):
            self.assertIsNone(execute_plan(request, context, None))
