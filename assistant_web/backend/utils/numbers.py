"""숫자 변환과 범위 제한. 기존 변환 예외를 그대로 전달한다."""
from typing import Optional


def clamp_float(value: Optional[float], minimum: float, maximum: float) -> Optional[float]:
    if value is None:
        return None
    return max(minimum, min(float(value), maximum))


def clamp_int(value: Optional[int | str], minimum: int, maximum: int) -> Optional[int]:
    if value is None:
        return None
    return max(minimum, min(int(value), maximum))
