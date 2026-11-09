"""호출의 작업 사본과 요청 소유자의 확정 상태 사이의 게시 경계."""
from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from threading import RLock
from typing import TYPE_CHECKING, Any

from backend.execution import CURRENT_BUDGET, ExecutionBudget, checkpoint, execution_slot

if TYPE_CHECKING:
    from backend.tools.registry import ToolContext


class ToolStateConflict(RuntimeError):
    """읽은 뒤 다른 호출이 확정 상태를 바꿨으므로 다시 계획해야 한다."""


@dataclass
class CallOwner:
    version: int = 0
    lock: Any = field(default_factory=RLock, repr=False)


_STATE = ('ledger', 'signatures', 'condition_refs', 'notion_read_cache', 'query_contracts',
          'query_scopes', 'local_state')
_INPUTS = ('conditions', 'enabled_sources')


def snapshot(context: 'ToolContext') -> dict:
    # 같은 페이지/조건 객체를 가리키는 원장·캐시 사이의 별칭도 사본 안에서만 유지한다.
    return deepcopy({name: getattr(context, name) for name in (*_STATE, *_INPUTS)})


def restore(context: 'ToolContext', state: dict) -> None:
    for name, value in state.items():
        setattr(context, name, value)


@dataclass
class PendingCall:
    root: 'ToolContext'
    context: 'ToolContext'
    version: int
    budget: ExecutionBudget | None
    base: dict

    def publish(self) -> None:
        # 게시 후 작업자가 사본을 변경해도 확정 근거에는 영향을 줄 수 없다.
        state = snapshot(self.context)
        owner = self.root._call_owner
        with execution_slot(owner.lock):
            if owner.version != self.version:
                raise ToolStateConflict()

            def apply():
                # AgentRun이 가진 원장 객체는 유지하며 내용의 소유권만 넘긴다.
                for item in fields(self.root.ledger):
                    setattr(self.root.ledger, item.name, getattr(state['ledger'], item.name))
                for name in _STATE:
                    if name != 'ledger':
                        setattr(self.root, name, state[name])
                owner.version += 1

            if self.budget is not None:
                self.budget.publish(apply)
            else:
                checkpoint()
                apply()


def begin_call(context: 'ToolContext') -> PendingCall:
    root = context._root or context
    with execution_slot(root._call_owner.lock):
        base = snapshot(root)
        local = replace(root, **deepcopy(base), call_id=context.call_id, _root=root)
        return PendingCall(root, local, root._call_owner.version, CURRENT_BUDGET.get(), base)


def _merge_mapping(base: dict, local: dict, current: dict) -> dict:
    missing = object()
    merged = dict(current)
    for key in base.keys() | local.keys():
        old, new, actual = base.get(key, missing), local.get(key, missing), current.get(key, missing)
        if old == new:
            continue
        if actual != old and actual != new:
            raise ToolStateConflict()
        if new is missing:
            merged.pop(key, None)
        else:
            merged[key] = new
    return merged


def _merge_list(base: list, local: list, current: list) -> list:
    if len(local) < len(base) or len(current) < len(base):
        raise ToolStateConflict()
    merged = list(current)
    for index, old in enumerate(base):
        if local[index] != old:
            if current[index] != old and current[index] != local[index]:
                raise ToolStateConflict()
            merged[index] = local[index]
    return merged + local[len(base):]


def _merge_state(pending: PendingCall, current: dict) -> dict:
    """기준 대비 변경한 키·기존 단계만 반영하고 추가 단계는 호출 순서대로 붙인다."""
    base, local = pending.base, snapshot(pending.context)
    merged = deepcopy(current)
    # 계획/권한 변경은 병렬 읽기 계약 밖이다. 입력을 변경한 작업은 게시하지 않는다.
    if any(local[name] != base[name] for name in _INPUTS):
        raise ToolStateConflict()
    old_ledger, new_ledger, ledger = base['ledger'], local['ledger'], merged['ledger']
    offset = len(ledger.stages) - len(old_ledger.stages)
    for step in new_ledger.condition_steps[len(old_ledger.condition_steps):]:
        if any(index < len(old_ledger.stages) or index >= len(new_ledger.stages)
               or new_ledger.stages[index].call_id != pending.context.call_id for index in step['stage_indices']):
            raise ToolStateConflict()
        step['stage_indices'] = [index + offset for index in step['stage_indices']]
    for item in fields(ledger):
        old, new, actual = (getattr(obj, item.name) for obj in (old_ledger, new_ledger, ledger))
        merge = _merge_list if isinstance(old, list) else _merge_mapping
        setattr(ledger, item.name, merge(old, new, actual))
    if len(set(ledger.notion_refs.values())) != len(ledger.notion_refs):
        raise ToolStateConflict()
    for name in _STATE:
        if name != 'ledger':
            merged[name] = _merge_mapping(base[name], local[name], merged[name])
    return merged


def publish_group(pending: list[PendingCall]) -> list[bool]:
    """동일 기준에서 시작한 읽기 묶음만 조정자가 한 번에 확정한다. 작업자는 게시하지 않는다."""
    first = pending[0]
    owner = first.root._call_owner
    with execution_slot(owner.lock):
        if owner.version != first.version or any(
                p.root is not first.root or p.version != first.version or p.base != first.base for p in pending):
            return [False] * len(pending)
        state = snapshot(first.root)
        accepted = []
        for item in pending:
            try:
                state = _merge_state(item, state)
                accepted.append(True)
            except ToolStateConflict:
                accepted.append(False)

        def apply():
            for field_ in fields(first.root.ledger):
                setattr(first.root.ledger, field_.name, getattr(state['ledger'], field_.name))
            for name in _STATE:
                if name != 'ledger':
                    setattr(first.root, name, state[name])
            owner.version += 1

        if first.budget is not None:
            first.budget.publish(apply)
        else:
            checkpoint()
            apply()
        return accepted
