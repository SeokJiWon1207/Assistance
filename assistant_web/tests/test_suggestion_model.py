import json
import os
import unittest
from unittest.mock import Mock, patch
from backend.execution import ExecutionBudget
from backend.suggestions.selection import select_candidates
from test_suggestion_ranking import candidate


class SuggestionModelTests(unittest.TestCase):
    def test_no_model_configuration_calls_no_model(self):
        with patch('backend.llm.post_model_chat') as post:
            result = select_candidates([candidate()], budget=ExecutionBudget.create())
        post.assert_not_called()
        self.assertEqual(result, [('mantis12', 'review_progress')])

    def test_allowlist_and_invalid_selection_fall_back(self):
        model = {'id': 'company', 'endpoint': 'https://company.test/v1/chat/completions', 'model': 'company'}
        env = {'HOME_SUGGESTIONS_MODEL_ID': 'company', 'HOME_SUGGESTIONS_ALLOWED_MODEL_IDS': 'company',
               'HOME_SUGGESTIONS_ALLOWED_ENDPOINT_ORIGINS': 'https://company.test'}
        for content in ('{"items":[{"id":"forged","action":"review_progress"}]}',
                        '{"items":[{"id":"mantis12","action":"delete"}]}', 'bad json'):
            response = Mock(status_code=200)
            response.json.return_value = {'choices': [{'message': {'content': content}}]}
            with patch.dict(os.environ, env), patch('backend.llm.get_private_model', return_value=model), \
                 patch('backend.llm.get_model_api_key', return_value='test'), \
                 patch('backend.llm.post_model_chat', return_value=response) as post:
                self.assertEqual(select_candidates([candidate()], budget=ExecutionBudget.create()), [('mantis12', 'review_progress')])
                post.assert_called_once()
                response.close.assert_called_once()
        with patch.dict(os.environ, {**env, 'HOME_SUGGESTIONS_ALLOWED_ENDPOINT_ORIGINS': 'https://other.test'}), \
             patch('backend.llm.get_private_model', return_value=model), patch('backend.llm.post_model_chat') as post:
            select_candidates([candidate()], budget=ExecutionBudget.create())
            post.assert_not_called()

    def test_valid_selection_and_masked_input(self):
        model = {'id': 'company', 'endpoint': 'https://company.test/v1/chat/completions', 'model': 'company'}
        response = Mock(status_code=200)
        response.json.return_value = {'choices': [{'message': {'content': json.dumps({'items': [
            {'id': 'mantis12', 'action': 'prepare_checklist'}]})}}]}
        with patch.dict(os.environ, {'HOME_SUGGESTIONS_MODEL_ID': 'company', 'HOME_SUGGESTIONS_ALLOWED_MODEL_IDS': 'company',
                                     'HOME_SUGGESTIONS_ALLOWED_ENDPOINT_ORIGINS': 'https://company.test'}), \
             patch('backend.llm.get_private_model', return_value=model), patch('backend.llm.get_model_api_key', return_value='test'), \
             patch('backend.llm.post_model_chat', return_value=response):
            self.assertEqual(select_candidates([candidate()], budget=ExecutionBudget.create()), [('mantis12', 'prepare_checklist')])
