"""검증된 값을 고정하고 실패 항목만 한 번 보정하는 공통 거래.

원문·사실 검증은 호출 경로의 기존 검증기가 담당한다. 이 하위 모듈은 검증 기준이나
조회 조건을 고치지 않으며, 원문 부족·권한·취소를 재작성으로 해결하지 않는다.
"""
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, TypeVar

from backend.execution import CURRENT_BUDGET, checkpoint

T = TypeVar('T')
REWRITABLE = frozenset({'expression', 'citation', 'numeric'})
STOPPED = frozenset({'policy_blocked', 'permission_denied', 'cancelled', 'serving_changed', 'serving_fingerprint_changed'})


@dataclass(frozen=True)
class Failure:
    kind: str
    reason: str


def repair_items(verified: dict[str, T], failures: dict[str, Failure], *,
                 propose: Callable[[dict[str, Failure]], dict[str, T]],
                 validate: Callable[[dict[str, T]], dict[str, T]],
                 revalidate: Callable[[dict[str, T]], bool],
                 can_attempt: Callable[[], bool], audit: dict, stop_reason: str = '') -> dict[str, T]:
    """확정된 값은 변경하지 않고 검증된 차분만 반영한다. 예외는 호출부에 전달한다."""
    original = deepcopy(verified)
    targets = {key: value for key, value in failures.items() if key not in original and value.kind in REWRITABLE}
    audit.update(version=1, attempted=False, preserved=list(original), repaired=[], ignored=[],
                 failures=[{'id': k, 'kind': v.kind, 'reason': v.reason} for k, v in failures.items()],
                 unresolved=list(failures), status='not_needed' if not failures else 'incomplete')
    checkpoint()
    if not targets or stop_reason in STOPPED or not can_attempt():
        return original
    audit.update(attempted=True, status='attempted')
    proposed = propose(deepcopy(targets))
    checkpoint()
    if not isinstance(proposed, dict):
        audit['status'] = 'invalid_patch'
        return original
    audit['ignored'] = [k for k in proposed if k not in targets]
    candidates = {k: deepcopy(v) for k, v in proposed.items() if k in targets}
    accepted = validate(candidates) if candidates else {}
    checkpoint()
    if not isinstance(accepted, dict) or any(k not in candidates for k in accepted):
        audit['status'] = 'invalid_validation'
        return original
    combined = {**deepcopy(original), **deepcopy(accepted)}
    if not revalidate(deepcopy(combined)):
        audit['status'] = 'combination_rejected'
        return original
    def publish():
        audit.update(repaired=list(accepted), unresolved=[k for k in failures if k not in accepted],
                     status='accepted' if len(accepted) == len(failures) else 'partial' if accepted else 'rejected')
        return combined
    budget = CURRENT_BUDGET.get()
    return budget.publish(publish) if budget is not None else publish()
