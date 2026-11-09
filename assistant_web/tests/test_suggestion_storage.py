"""추천 저장의 사용자·원본 버전·작업 소유권 경계."""
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.suggestions.contracts import SourceRef, Suggestion, SuggestionBundle
from backend.suggestions.repository import SuggestionRepository


def sample(now, revision='v1'):
    source = SourceRef(type='mantis', id='12', title='업무', url='https://mantis.example/view.php?id=12',
                       revision=revision, observedAt=now, validUntil=now + timedelta(minutes=10))
    return Suggestion(id='s-12', kind='review_progress', title='현재 상태를 확인해요',
                      promptPreview='#12 최신 상태를 확인해줘', reasonCode='overdue', revision=revision,
                      dedupeKey='mantis:12', sources=[source], generatedAt=now,
                      expiresAt=now + timedelta(minutes=10))


class SuggestionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = SuggestionRepository(Path(self.temp.name) / 'workspace.sqlite3')
        self.now = datetime(2026, 11, 9, tzinfo=timezone.utc)

    def publish(self, user='a', scope='one', revision='v1', now=None):
        now = now or self.now
        lease = self.repo.claim(user, scope, now)
        self.assertIsNotNone(lease)
        self.assertTrue(self.repo.publish(lease, SuggestionBundle(items=[sample(now, revision)], state='ready'), now))

    def test_users_and_connections_are_isolated(self):
        self.publish()
        self.assertEqual(len(self.repo.read('a', 'one', self.now).items), 1)
        self.assertFalse(self.repo.read('b', 'one', self.now).items)
        self.assertFalse(self.repo.read('a', 'two', self.now).items)
        self.assertFalse(self.repo.dismiss('b', 'one', 's-12', self.now))

    def test_dismiss_survives_identical_source_refresh(self):
        self.publish()
        self.assertTrue(self.repo.dismiss('a', 'one', 's-12', self.now))
        self.assertTrue(self.repo.dismiss('a', 'one', 's-12', self.now))
        self.publish(now=self.now + timedelta(seconds=61))
        self.assertFalse(self.repo.read('a', 'one', self.now + timedelta(seconds=62)).items)

    def test_new_revision_can_reappear(self):
        self.publish()
        self.repo.dismiss('a', 'one', 's-12', self.now)
        self.publish(revision='v2', now=self.now + timedelta(seconds=61))
        self.assertEqual(len(self.repo.read('a', 'one', self.now + timedelta(seconds=62)).items), 1)

    def test_old_lease_cannot_publish(self):
        first = self.repo.claim('a', 'one', self.now)
        self.assertIsNone(self.repo.claim('a', 'one', self.now + timedelta(seconds=1)))
        later = self.now + timedelta(seconds=91)
        second = self.repo.claim('a', 'one', later)
        self.assertIsNotNone(second)
        self.assertFalse(self.repo.publish(first, SuggestionBundle(items=[sample(self.now)], state='ready'), later))
        self.assertTrue(self.repo.publish(second, SuggestionBundle(state='ready'), later))

    def test_expired_items_are_hidden_and_need_refresh(self):
        self.publish()
        result = self.repo.read('a', 'one', self.now + timedelta(minutes=11))
        self.assertFalse(result.items)
        self.assertEqual(result.state, 'unavailable')

    def test_generation_completion_does_not_restart_refresh_cooldown(self):
        lease = self.repo.claim('u', 'scope', self.now)
        self.repo.release(lease, self.now + timedelta(seconds=40))
        self.assertIsNotNone(self.repo.claim('u', 'scope', self.now + timedelta(seconds=61)))

    def test_late_item_invalidation_preserves_new_revision_and_other_scope(self):
        self.publish(user='other')
        lease = self.repo.claim('a', 'one', self.now)
        new = sample(self.now, 'v2').model_copy(update={'id': 's-new'})
        self.repo.publish(lease, SuggestionBundle(items=[new], state='ready'), self.now)
        self.repo.invalidate_item('a', 'one', 's-old')
        self.assertEqual(self.repo.read('a', 'one', self.now).items[0].id, 's-new')
        self.repo.invalidate_item('a', 'one', 's-new')
        self.assertFalse(self.repo.read('a', 'one', self.now).items)
        self.assertEqual(len(self.repo.read('other', 'one', self.now).items), 1)
        self.repo.invalidate_item('missing', 'one', 's-new')
