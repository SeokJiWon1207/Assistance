"""사용자별 캐시와 숨김, 프로세스 간 갱신 lease."""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from backend.storage.profiles import WORKSPACE
from backend.storage.sqlite import open_sqlite
from backend.storage.workspace_schema import init_workspace_db
from backend.suggestions.contracts import Lease, SuggestionBundle
from backend.suggestions.settings import CACHE_SECONDS, COOLDOWN_SECONDS, LEASE_SECONDS


class SuggestionRepository:
    def __init__(self, path: Path):
        self.path = path
        init_workspace_db(path)

    @contextmanager
    def database(self):
        with closing(open_sqlite(self.path, profile=WORKSPACE)) as db, db:
            yield db

    def read(self, user_uid: str, scope_key: str, now: datetime) -> SuggestionBundle:
        with self.database() as db:
            row = db.execute('SELECT payload, expires FROM suggestion_cache WHERE user_uid=? AND scope_key=?',
                             (user_uid, scope_key)).fetchone()
            job = db.execute('SELECT token, expires, started FROM suggestion_jobs WHERE user_uid=? AND scope_key=?',
                             (user_uid, scope_key)).fetchone()
            hidden = set(db.execute('SELECT dedupe_key, revision FROM suggestion_dismissals WHERE user_uid=? AND scope_key=?',
                                    (user_uid, scope_key)))
        result = SuggestionBundle()
        if row and row[1] > now.timestamp():
            result = SuggestionBundle.model_validate_json(row[0])
            result.items = [item for item in result.items if item.expiresAt > now
                            and (item.dedupeKey, item.revision) not in hidden]
        if job:
            result.generationId = job[0]
            if job[1] > now.timestamp():
                result.state = 'pending'
                result.retryAfterSeconds = 5
            else:
                result.retryAfterSeconds = max(0, int(job[2] + COOLDOWN_SECONDS - now.timestamp()))
        return result

    def claim(self, user_uid: str, scope_key: str, now: datetime) -> Lease | None:
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT scope_key, expires, started FROM suggestion_jobs WHERE user_uid=?', (user_uid,)).fetchone()
            if row and (row[1] > now.timestamp() or (row[0] == scope_key and row[2] + COOLDOWN_SECONDS > now.timestamp())):
                return None
            lease = Lease(token=uuid4().hex, user_uid=user_uid, scope_key=scope_key,
                          expires_at=now + timedelta(seconds=LEASE_SECONDS))
            db.execute('INSERT INTO suggestion_jobs VALUES (?,?,?,?,?) ON CONFLICT(user_uid) DO UPDATE SET '
                       'scope_key=excluded.scope_key,token=excluded.token,expires=excluded.expires,started=excluded.started',
                       (user_uid, scope_key, lease.token, lease.expires_at.timestamp(), now.timestamp()))
            db.execute('DELETE FROM suggestion_cache WHERE expires < ?', (now.timestamp() - 86400,))
            db.execute('DELETE FROM suggestion_dismissals WHERE touched < ?', (now.timestamp() - 30 * 86400,))
            db.execute('DELETE FROM suggestion_jobs WHERE expires < ? AND started < ?',
                       (now.timestamp(), now.timestamp() - 86400))
            return lease

    def publish(self, lease: Lease, bundle: SuggestionBundle, now: datetime) -> bool:
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT token,scope_key,expires FROM suggestion_jobs WHERE user_uid=?', (lease.user_uid,)).fetchone()
            if not row or row[:2] != (lease.token, lease.scope_key) or row[2] <= now.timestamp():
                return False
            expiry = min([now.timestamp() + CACHE_SECONDS, *[x.expiresAt.timestamp() for x in bundle.items]])
            db.execute('INSERT INTO suggestion_cache VALUES (?,?,?,?) ON CONFLICT(user_uid,scope_key) DO UPDATE SET '
                       'payload=excluded.payload,expires=excluded.expires',
                       (lease.user_uid, lease.scope_key, bundle.model_dump_json(), expiry))
            db.execute('UPDATE suggestion_jobs SET expires=0 WHERE user_uid=? AND token=?', (lease.user_uid, lease.token))
            return True

    def release(self, lease: Lease, now: datetime, *, retry: bool = False) -> None:
        with self.database() as db:
            db.execute('UPDATE suggestion_jobs SET expires=0 WHERE user_uid=? AND token=?', (lease.user_uid, lease.token))
            if retry:
                db.execute('UPDATE suggestion_jobs SET started=? WHERE user_uid=? AND token=?',
                           (now.timestamp() - COOLDOWN_SECONDS, lease.user_uid, lease.token))

    def renew(self, lease: Lease, now: datetime) -> bool:
        with self.database() as db:
            changed = db.execute('UPDATE suggestion_jobs SET expires=? WHERE user_uid=? AND scope_key=? AND token=? AND expires>?',
                ((now + timedelta(seconds=LEASE_SECONDS)).timestamp(), lease.user_uid, lease.scope_key, lease.token, now.timestamp()))
            return changed.rowcount == 1

    def dismiss(self, user_uid: str, scope_key: str, suggestion_id: str, now: datetime) -> bool:
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM suggestion_cache WHERE user_uid=? AND scope_key=?',
                             (user_uid, scope_key)).fetchone()
            if not row:
                return False
            item = next((item for item in SuggestionBundle.model_validate_json(row[0]).items if item.id == suggestion_id), None)
            if item is None:
                return False
            db.execute('INSERT INTO suggestion_dismissals VALUES (?,?,?,?,?) ON CONFLICT(user_uid,scope_key,dedupe_key,revision) '
                       'DO UPDATE SET touched=excluded.touched',
                       (user_uid, scope_key, item.dedupeKey, item.revision, now.timestamp()))
            return True

    def invalidate(self, user_uid: str, scope_key: str) -> None:
        with self.database() as db:
            db.execute('DELETE FROM suggestion_cache WHERE user_uid=? AND scope_key=?', (user_uid, scope_key))

    def invalidate_item(self, user_uid: str, scope_key: str, identifier: str) -> None:
        # prepare 도중 새 세대가 게시됐으면 동일 ID(동일 버전)만 제거한다.
        with self.database() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT payload FROM suggestion_cache WHERE user_uid=? AND scope_key=?',
                             (user_uid, scope_key)).fetchone()
            if row is None:
                return
            bundle = SuggestionBundle.model_validate_json(row[0])
            bundle.items = [item for item in bundle.items if item.id != identifier]
            db.execute('UPDATE suggestion_cache SET payload=? WHERE user_uid=? AND scope_key=?',
                       (bundle.model_dump_json(), user_uid, scope_key))
