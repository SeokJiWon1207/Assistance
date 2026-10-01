"""공유 JSON 원본은 읽기 전용으로, 명시적인 복사본은 수정 가능하게 만든다."""
from copy import deepcopy


def _deny(*args, **kwargs):
    raise TypeError("공유 원본은 수정할 수 없습니다. 필요한 행을 복사해 사용하세요.")


class FrozenDict(dict):
    __setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = __ior__ = _deny

    def __deepcopy__(self, memo):
        return {key: deepcopy(value, memo) for key, value in self.items()}


class FrozenList(list):
    __setitem__ = __delitem__ = append = extend = insert = pop = remove = clear = reverse = sort = __iadd__ = __imul__ = _deny

    def __deepcopy__(self, memo):
        return [deepcopy(value, memo) for value in self]


def freeze(value):
    if isinstance(value, dict):
        return FrozenDict({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return FrozenList(freeze(item) for item in value)
    return value
