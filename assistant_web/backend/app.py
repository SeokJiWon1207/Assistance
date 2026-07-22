"""하위 호환 ASGI 진입점.

새 코드는 backend.main과 각 api/services 모듈을 직접 사용한다.
"""

from backend.main import app

__all__ = ["app"]
