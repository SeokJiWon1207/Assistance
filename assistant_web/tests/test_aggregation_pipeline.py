import os
import unittest
from copy import deepcopy
from unittest.mock import patch

from backend.schemas import ChatMessage, ChatRequest
from backend.services.chat_service import chat_service
from backend.evaluation.verification import build_verification_report
from test_aggregation import complete_store, raw_issue
from test_aggregation_spec import proposal, specification


class AggregationPipelineTests(unittest.TestCase):
    def test_invalid_top_n_is_retried_before_accepting_corrected_plan(self):
        from backend.intent import unified
        question = '전사 2026년 8월부터 9월까지 고객사와 월별로 Mantis 등록 건수를 집계해줘.'
        valid = proposal(aggregation=specification(metrics=['issue_count'], comparison=None, explain=False))
        for invalid_value in (None, 0):
            with self.subTest(top_n=invalid_value):
                invalid = deepcopy(valid)
                invalid['aggregation']['top_n'] = invalid_value
                calls = []

                def propose(model, key, payload, *, calls=calls, invalid=invalid):
                    calls.append(deepcopy(payload))
                    return deepcopy(invalid if len(calls) == 1 else valid)

                with patch.object(unified, '_propose', side_effect=propose), patch.object(
                        unified.llm, 'get_model_registry', return_value=[]):
                    plan = unified.plan_question(question, {'id': 'fixture'}, 'test', {}, {})
                self.assertEqual(len(calls), 2)
                self.assertEqual(plan['aggregation'], valid['aggregation'])

    def test_retry_explains_conflicting_outer_fields_without_changing_aggregation(self):
        from backend.intent import unified
        question = ('전사 2026년 8월부터 9월까지 고객사별 월별 Mantis 등록 건수를 전월 대비 분석해줘. '
                    '상위 3개 고객사와 기타로 보여줘.')
        valid = proposal(aggregation=specification(metrics=['issue_count'], top_n=3))
        invalid = {**valid, 'group_by': 'customer', 'limit': 3}
        calls = []

        def propose(model, key, payload):
            calls.append(deepcopy(payload))
            return deepcopy(invalid if len(calls) == 1 else valid)

        with patch.object(unified, '_propose', side_effect=propose), patch.object(
                unified.llm, 'get_model_registry', return_value=[]):
            plan = unified.plan_question(question, {'id': 'fixture'}, 'test', {}, {})
        self.assertEqual(len(calls), 2)
        feedback = calls[1]['messages'][-1]['content']
        self.assertIn('최상위 group_by=null, limit=null', feedback)
        self.assertIn('aggregation.top_n', feedback)
        self.assertEqual(plan['aggregation'], valid['aggregation'])
        self.assertIsNone(plan['group_by'])
        self.assertIsNone(plan['limit'])

    def test_chat_uses_one_planner_call_and_returns_compound_table(self):
        question = '전사 2026년 8월부터 9월까지 고객사별 월별 Mantis 이슈 건수를 보여줘'
        planned = proposal(aggregation=specification(metrics=['issue_count'], explain=False))
        request = ChatRequest(modelId='offline', abTest=False, messages=[ChatMessage(role='user', content=question)],
                              tools={'mantis': True, 'notion': False, 'vectorDb': False})
        with patch.dict(os.environ, {'UNIFIED_QUERY_PLANNER_ENABLED': 'true', 'MANTIS_AGGREGATION_MODE': 'primary'}), patch(
                'backend.services.chat_service.get_private_model', return_value={'id': 'offline', 'status': 'active'}), patch(
                'backend.services.chat_service.get_model_api_key', return_value='test'), patch(
                'backend.intent.unified._propose', return_value=planned) as planner, patch(
                'backend.sources.mantis_store.stream_complete_store', side_effect=lambda: complete_store([raw_issue(1)])):
            result = chat_service.build_response(request, user={'uid': 'aggregation-user'})
        self.assertEqual(result['raw']['mode'], 'validated_aggregation')
        self.assertEqual(result['aggregation']['totals'], {'issue_count': 1})
        self.assertEqual(result['verification']['constraints'], [])
        self.assertEqual(planner.call_count, 1)
        self.assertEqual(result['retrieval']['aggregation_narrative']['calls'], 0)

    def test_verification_exposes_display_filters_without_changing_execution_constraints(self):
        constraints = {'platform_key': 'MTS', 'platform_aliases': ['MTS', 'mts']}
        report = build_verification_report('MTS 집계', '계산 표', [], {
            'query_plan': {'resolved_constraints': constraints},
            'aggregation_narrative': {'status': 'not_requested'},
        }, 'validated_aggregation')
        self.assertEqual(report['constraints'], [
            {'id': 'platform', 'label': '플랫폼', 'value': 'MTS', 'values': ['MTS', 'mts']}])
        self.assertEqual(report['queryPlan']['resolved_constraints'], constraints)

    def test_explanation_is_not_automatically_trusted_as_deterministic_text(self):
        retrieval = {'constraints': {}, 'aggregation_narrative': {'status': 'unavailable', 'calls': 1}}
        result = build_verification_report('집계', '계산 표', [], retrieval, 'validated_aggregation')
        checks = {c['id']: c for c in result['checks']}
        self.assertIn('aggregation_narrative', checks)
        self.assertEqual(checks['aggregation_narrative']['status'], 'warning')
