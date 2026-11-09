import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from backend.suggestions.repository import SuggestionRepository
from backend.suggestions.service import SuggestionService
from backend.suggestions.jobs import SuggestionJobs
from backend.suggestions.contracts import EvidenceBatch, SuggestionBundle
from test_suggestion_storage import sample


class SuggestionJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SuggestionRepository(Path(self.temp.name) / 'workspace.sqlite3')
        self.service = SuggestionService(self.repo)
        self.addCleanup(self.service.jobs.stop)
        self.user = {'uid': 'a', 'displayName': '김담당', 'approved': True}
        self.now = datetime.now(timezone.utc)

    def test_get_never_calls_network(self):
        with patch('backend.suggestions.service.collect_candidates', side_effect=AssertionError('network')):
            self.assertFalse(self.service.read(self.user, 3, self.now).items)

    def test_duplicate_refresh_shares_generation(self):
        with patch.object(self.service.jobs, 'start'):
            first = self.service.refresh(self.user, self.now)
            second = self.service.refresh(self.user, self.now)
        self.assertEqual(first.generationId, second.generationId)
        self.assertEqual(self.service.jobs.pending_count(), 1)

    def test_queue_has_eight_user_limit(self):
        with patch.object(self.service.jobs, 'start'):
            for i in range(8):
                self.service.refresh({**self.user, 'uid': str(i)}, self.now)
            with self.assertRaisesRegex(Exception, '대기'):
                self.service.refresh({**self.user, 'uid': 'ninth'}, self.now)

    def test_deleted_or_remapped_user_cannot_publish(self):
        from backend.suggestions.sources import current_scope_key
        lease = self.repo.claim('a', current_scope_key(self.user), self.now)
        with patch('backend.suggestions.service.auth.get_user_by_uid', return_value=None), \
             patch('backend.suggestions.service.collect_candidates') as collect:
            self.service.generate(lease, None)
        collect.assert_not_called()
        self.assertFalse(self.service.read(self.user, 3, self.now).items)

    def test_shutdown_waits_for_actual_workers(self):
        entered, leave, stopped = threading.Event(), threading.Event(), threading.Event()
        def run(lease, budget):
            entered.set()
            leave.wait(3)
        jobs = SuggestionJobs(self.repo, run)
        lease = self.repo.claim('a', 'scope', self.now)
        jobs.enqueue(lease)
        self.assertTrue(entered.wait(2))
        stopper = threading.Thread(target=lambda: (jobs.stop(), stopped.set()))
        stopper.start()
        self.assertFalse(stopped.wait(.05))
        leave.set()
        stopper.join(3)
        self.assertTrue(stopped.is_set())

    def test_prepare_revocation_clears_cache(self):
        from backend.suggestions.sources import current_scope_key, SourceRevoked
        scope = current_scope_key(self.user)
        lease = self.repo.claim('a', scope, self.now)
        self.repo.publish(lease, SuggestionBundle(items=[sample(self.now)], state='ready'), self.now)
        with patch('backend.suggestions.service.revalidate_sources', side_effect=SourceRevoked()), \
             patch('backend.suggestions.service.auth.get_user_by_uid', return_value=self.user):
            with self.assertRaisesRegex(Exception, '변경'):
                self.service.prepare(self.user, 's-12', self.now)
        self.assertFalse(self.service.read(self.user, 3, self.now).items)

    def test_partial_generation_preserves_successful_source(self):
        from backend.suggestions.sources import current_scope_key
        from test_suggestion_ranking import candidate
        scope = current_scope_key(self.user)
        lease = self.repo.claim('a', scope, self.now)
        c = candidate()
        c.sources[0].validUntil = self.now + timedelta(minutes=10)
        with patch('backend.suggestions.service.auth.get_user_by_uid', return_value=self.user), \
             patch('backend.suggestions.service.collect_candidates', return_value=EvidenceBatch(
                 candidates=[c], scopeKey=scope, sourceStatus={'mantis': 'ok', 'notion': 'unavailable'})):
            from backend.execution import ExecutionBudget
            self.service.generate(lease, ExecutionBudget.create())
        result = self.service.read(self.user, 3, self.now)
        self.assertEqual(result.state, 'partial')
        self.assertEqual(len(result.items), 1)

    def test_total_source_failure_keeps_unexpired_items(self):
        from backend.suggestions.sources import current_scope_key
        from backend.execution import ExecutionBudget
        scope = current_scope_key(self.user)
        old = self.repo.claim('a', scope, self.now)
        self.repo.publish(old, SuggestionBundle(items=[sample(self.now)], state='ready'), self.now)
        self.repo.release(old, self.now, retry=True)
        lease = self.repo.claim('a', scope, self.now)
        with patch('backend.suggestions.service.auth.get_user_by_uid', return_value=self.user), \
             patch('backend.suggestions.service.collect_candidates', return_value=EvidenceBatch(
                 scopeKey=scope, sourceStatus={'mantis': 'unavailable', 'notion': 'unavailable'})):
            self.service.generate(lease, ExecutionBudget.create())
        result = self.service.read(self.user, 3, self.now)
        self.assertEqual(result.items[0].id, 's-12')
        self.assertEqual(result.state, 'partial')

    def test_prepare_timeout_is_service_unavailable(self):
        from backend.suggestions.sources import current_scope_key
        from backend.execution import RequestStopped
        from fastapi import HTTPException
        scope = current_scope_key(self.user)
        lease = self.repo.claim('a', scope, self.now)
        self.repo.publish(lease, SuggestionBundle(items=[sample(self.now)], state='ready'), self.now)
        with patch('backend.suggestions.service.auth.get_user_by_uid', return_value=self.user), \
             patch('backend.suggestions.service.revalidate_sources', side_effect=RequestStopped()):
            with self.assertRaises(HTTPException) as caught:
                self.service.prepare(self.user, 's-12', self.now)
        self.assertEqual(caught.exception.status_code, 503)
        self.assertTrue(self.service.read(self.user, 3, self.now).items)

    def test_cancelled_generation_cannot_publish_after_last_identity_check(self):
        from backend.suggestions.sources import current_scope_key
        from backend.execution import ExecutionBudget, RequestStopped
        from test_suggestion_ranking import candidate
        scope = current_scope_key(self.user)
        lease = self.repo.claim('a', scope, self.now)
        budget = ExecutionBudget.create()
        calls = 0
        def identity(uid):
            nonlocal calls
            calls += 1
            if calls == 2:
                budget.cancel()
            return self.user
        with patch('backend.suggestions.service.auth.get_user_by_uid', side_effect=identity), \
             patch('backend.suggestions.service.collect_candidates', return_value=EvidenceBatch(candidates=[candidate()], sourceStatus={'mantis': 'ok'})), \
             patch.object(self.repo, 'publish') as publish:
            with self.assertRaises(RequestStopped):
                self.service.generate(lease, budget)
        publish.assert_not_called()

    def test_worker_logs_exception_type_without_sensitive_message(self):
        entered = threading.Event()
        def fail(lease, budget):
            entered.set()
            raise ValueError('secret-token and private source body')
        jobs = SuggestionJobs(self.repo, fail)
        lease = self.repo.claim('logging', 'scope', self.now)
        with self.assertLogs('backend.suggestions.jobs', level='WARNING') as captured:
            jobs.enqueue(lease)
            self.assertTrue(entered.wait(2))
            jobs.stop()
        output = '\n'.join(captured.output)
        self.assertIn('ValueError', output)
        self.assertNotIn('secret-token', output)
        self.assertNotIn('private source body', output)
