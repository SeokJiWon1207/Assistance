"""구조화된 조건의 부분 문자열로 소비하면 안 되는 코드 식별자."""
import re

COMPOUND_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)+(?![A-Za-z0-9_])"
)
