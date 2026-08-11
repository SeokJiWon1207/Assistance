"""기존 Notion 파사드의 monkeypatch를 실제 구현에 전달한다."""

import sys
from typing import Any, Callable, TypeVar


Dependency = TypeVar("Dependency", bound=Callable[..., Any])


def compat_dependency(name: str, default: Dependency) -> Dependency:
    core = sys.modules.get("backend.notion.core")
    return getattr(core, name, default) if core is not None else default


__all__ = ["compat_dependency"]
