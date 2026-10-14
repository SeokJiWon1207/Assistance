"""목록의 단일 필드에 들어간 원문 구분 문자를 가역적으로 보존한다."""
import json
import re
from typing import Any


def encode_issue_field(value: Any) -> str:
    text = str(value or "")
    if not re.search(r'[|"\\\x00-\x1f*`<>\x85\u2028\u2029]', text):
        return text
    encoded = json.dumps(text, ensure_ascii=False)
    for character in "|*`<>\x85\u2028\u2029":
        encoded = encoded.replace(character, f"\\u{ord(character):04x}")
    return encoded


def decode_issue_field(value: str) -> str:
    if value.startswith('"') and value.endswith('"'):
        try:
            decoded = json.loads(value)
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            pass
    return value
