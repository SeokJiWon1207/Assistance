"""추천 생성과 선택 시 현재 근거 검증. GET은 로컬 캐시만 읽는다."""
from datetime import datetime, timezone
from time import monotonic
from threading import Lock
import logging
from fastapi import HTTPException

from backend import auth
from backend.execution import ExecutionBudget, RequestStopped
from backend.storage.workspace_schema import workspace_db_path
from backend.user_data_lock import user_data_operation
from backend.suggestions.contracts import BundleState, Lease, SuggestionBundle
from backend.suggestions.repository import SuggestionRepository
from backend.suggestions.jobs import SuggestionJobs
from backend.suggestions.sources import collect_candidates, current_scope_key, revalidate_sources, SourceRevoked, SourceUnavailable
from backend.suggestions.ranking import build_suggestions, ranked_candidates, render_prompt
from backend.suggestions.selection import select_candidates

logger = logging.getLogger(__name__)


class SuggestionService:
    def __init__(self, repository: SuggestionRepository):
        self.repository = repository
        self.jobs = SuggestionJobs(repository, self.generate)

    def read(self, user: dict, limit: int, now: datetime) -> SuggestionBundle:
        result = self.repository.read(user['uid'], current_scope_key(user), now)
        result.items = result.items[:max(1, min(limit, 3))]
        return result

    def refresh(self, user: dict, now: datetime) -> SuggestionBundle:
        scope = current_scope_key(user)
        lease = self.repository.claim(user['uid'], scope, now)
        if lease and not self.jobs.enqueue(lease):
            self.repository.release(lease, now, retry=True)
            raise HTTPException(429, '추천 갱신 대기가 많습니다. 잠시 후 다시 시도해 주세요.', headers={'Retry-After': '60'})
        return self.read(user, 3, now)

    def generate(self, lease: Lease, budget: ExecutionBudget) -> None:
        started = monotonic()
        # 계정 삭제와 같은 잠금 파일. 갱신 도중 삭제가 진행되어 데이터가 부활하지 않게 한다.
        with user_data_operation(lease.user_uid, auth.users_db_path()):
            user = auth.get_user_by_uid(lease.user_uid)
            if not user or not user.get('approved') or current_scope_key(user) != lease.scope_key:
                return
            batch = collect_candidates(user, now=datetime.now(timezone.utc), budget=budget)
            choices = select_candidates(ranked_candidates(batch), budget=budget)
            now = datetime.now(timezone.utc)
            items = build_suggestions(batch, now=now, choices=choices, limit=24)
            healthy = bool(batch.sourceStatus) and all(v in {'ok', 'unlinked'} for v in batch.sourceStatus.values())
            if not items and not healthy and not any(value == 'ok' for value in batch.sourceStatus.values()):
                items = self.repository.read(lease.user_uid, lease.scope_key, now).items
            budget.remaining()
            fresh_user = auth.get_user_by_uid(lease.user_uid)
            if not fresh_user or not fresh_user.get('approved') or current_scope_key(fresh_user) != lease.scope_key:
                return
            state: BundleState = 'ready' if healthy else 'partial' if items else 'unavailable'
            bundle = SuggestionBundle(items=items, state=state, sourceStatus=batch.sourceStatus, generationId=lease.token)
            published = budget.publish(lambda: self.repository.publish(lease, bundle, now))
            logger.info('home_suggestions published=%s elapsed_ms=%d candidates_mantis=%d candidates_notion=%d items=%d partial=%s',
                        published, int((monotonic() - started) * 1000),
                        sum(c.sources[0].type == 'mantis' for c in batch.candidates),
                        sum(c.sources[0].type == 'notion' for c in batch.candidates), min(len(items), 3), state != 'ready')

    def prepare(self, user: dict, identifier: str, now: datetime) -> dict:
        with user_data_operation(user['uid'], auth.users_db_path()):
            fresh = auth.get_user_by_uid(user['uid'])
            if not fresh or not fresh.get('approved') or current_scope_key(fresh) != current_scope_key(user):
                raise HTTPException(410, '사용자 업무 범위가 변경되었습니다. 추천을 다시 불러와 주세요.')
            scope = current_scope_key(fresh)
            bundle = self.repository.read(user['uid'], scope, now)
            item = next((i for i in bundle.items if i.id == identifier), None)
            if item is None:
                raise HTTPException(404, '추천을 찾을 수 없습니다. 만료되었거나 숨긴 항목일 수 있습니다.')
            budget = ExecutionBudget(monotonic() + 15)
            try:
                revalidate_sources(fresh, item.sources, budget=budget)
                budget.remaining()
                latest = auth.get_user_by_uid(user['uid'])
                if not latest or not latest.get('approved') or current_scope_key(latest) != scope:
                    raise SourceRevoked()
            except SourceRevoked as exc:
                self.repository.invalidate_item(user['uid'], scope, item.id)
                raise HTTPException(410, '원문 또는 담당 범위가 변경되었습니다. 추천을 갱신해 주세요.') from exc
            except (SourceUnavailable, RequestStopped) as exc:
                raise HTTPException(503, '현재 원문을 확인하지 못했습니다. 잠시 후 다시 시도해 주세요.') from exc
            finally:
                budget.cancel()
                budget.wait_for_workers()
            return {'prompt': render_prompt(item), 'revision': item.revision}

    def dismiss(self, user: dict, identifier: str, now: datetime) -> None:
        if not self.repository.dismiss(user['uid'], current_scope_key(user), identifier, now):
            raise HTTPException(404, '추천을 찾을 수 없습니다.')


_services: dict[str, SuggestionService] = {}
_lock = Lock()


def get_service() -> SuggestionService:
    path = workspace_db_path()
    with _lock:
        key = str(path)
        if key not in _services:
            _services[key] = SuggestionService(SuggestionRepository(path))
        return _services[key]


def stop_services() -> None:
    with _lock:
        services = list(_services.values())
        _services.clear()
    for service in services:
        service.jobs.stop()
