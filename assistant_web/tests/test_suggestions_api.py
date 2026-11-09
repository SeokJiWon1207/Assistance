import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from backend.api import suggestions as api
from backend.suggestions.repository import SuggestionRepository
from backend.suggestions.service import SuggestionService
from backend.suggestions.sources import current_scope_key
from backend.suggestions.contracts import SuggestionBundle
from test_suggestion_storage import sample


class SuggestionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = SuggestionService(SuggestionRepository(Path(self.temp.name) / 'ws.db'))
        self.addCleanup(self.service.jobs.stop)
        self.user = {'uid': 'a', 'displayName': '담당', 'approved': True}
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {'HOME_SUGGESTIONS_ENABLED': 'true'}).start()
        self.identity = patch.object(api, 'current_request_user', return_value=self.user).start()
        self.provider = patch.object(api, 'get_service', return_value=self.service).start()
        app = FastAPI()
        app.include_router(api.router)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_anonymous_denied(self):
        self.identity.return_value = None
        self.assertEqual(self.client.get('/api/suggestions').status_code, 401)

    def test_disabled_has_no_source_calls(self):
        with patch.dict(os.environ, {'HOME_SUGGESTIONS_ENABLED': 'false'}):
            self.assertEqual(self.client.get('/api/suggestions').json()['state'], 'disabled')
            self.assertEqual(self.client.post('/api/suggestions/refresh').status_code, 409)
        self.provider.assert_not_called()

    def test_limit_is_bounded(self):
        self.assertEqual(self.client.get('/api/suggestions?limit=4').status_code, 422)
        self.assertEqual(self.client.get('/api/suggestions?limit=0').status_code, 422)

    def test_refresh_is_idempotent_while_pending(self):
        with patch.object(self.service.jobs, 'start'):
            first = self.client.post('/api/suggestions/refresh')
            second = self.client.post('/api/suggestions/refresh')
        self.assertEqual(first.status_code, 202)
        self.assertEqual(first.json()['generationId'], second.json()['generationId'])

    def test_dismiss_is_user_scoped_and_idempotent(self):
        now = datetime.now(timezone.utc)
        lease = self.service.repository.claim('a', current_scope_key(self.user), now)
        self.service.repository.publish(lease, SuggestionBundle(items=[sample(now)], state='ready'), now)
        for _ in range(2):
            self.assertEqual(self.client.post('/api/suggestions/s-12/dismiss').status_code, 200)
        self.identity.return_value = {**self.user, 'uid': 'b'}
        self.assertEqual(self.client.post('/api/suggestions/s-12/dismiss').status_code, 404)
        with patch('backend.suggestions.service.auth.get_user_by_uid', return_value=self.identity.return_value):
            self.assertEqual(self.client.post('/api/suggestions/s-12/prepare').status_code, 404)
