"""모델·감사로 보내는 도구 결과의 개인정보·인증정보 패턴을 가린다.

마스킹은 보조 장치다. 전송 허용 여부는 서버의 전송 정책이 판단한다.
"""

import json
import re
from typing import Any

_PATTERNS = (
    (re.compile(r"(?i)\b(bearer|token|api[_-]?key|secret|password|passwd|pass|pwd)\s*[:=]\s*\S+"), r"\1=[가림]"),
    (re.compile(r"(?i)(비밀번호|암호|인증키|토큰)\s*[:=]\s*\S+"), r"\1=[가림]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{12,}=*"), "Bearer [가림]"),
    (re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"), "[키 가림]"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "[키 가림]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{6,}\b"), "[토큰 가림]"),
    (re.compile(r"(?<!\d)\d{6}-[1-8]\d{6}(?!\d)"), "[주민번호 가림]"),
    (re.compile(r"(?<!\d)\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}(?!\d)"), "[카드번호 가림]"),
    (re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), "[이메일 가림]"),
    (re.compile(r"(?<!\d)01[016789][- .]?\d{3,4}[- .]?\d{4}(?!\d)"), "[전화번호 가림]"),
    (re.compile(r"(?<!\d)0(?:2|[3-6][1-5])[- .]\d{3,4}[- .]\d{4}(?!\d)"), "[전화번호 가림]"),
)


def redact_text(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any) -> Any:
    """문자열·목록·사전을 재귀적으로 가린다. 키 이름은 바꾸지 않는다."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    return value


def redact_prompt_text(content: str) -> str:
    """원문 선택·검토 프롬프트에 삽입된 한 줄 JSON은 값별로 가려 문법과 문단 ID를 유지한다."""
    lines = []
    for line in content.split("\n"):
        if line.lstrip().startswith(("{", "[")):
            try:
                structured = json.loads(line)
            except ValueError:
                pass
            else:
                if isinstance(structured, (list, dict)):
                    lines.append(json.dumps(redact(structured), ensure_ascii=False, separators=(",", ":")))
                    continue
        lines.append(redact_text(line))
    return "\n".join(lines)
