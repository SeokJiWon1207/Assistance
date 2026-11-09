"""OpenAI 호환 Chat Completions의 도구 호출 응답을 해석한다.

기존 생성 경로의 "본문이 없으면 실패" 규칙과 달리 tool_calls만 있는 응답을 정상 중간
응답으로 다루고, 빈 응답·잘린 응답·파서가 꺼진 서버의 텍스트형 호출을 구분한다.
"""

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# 서버에 도구 호출 파서가 없으면 모델이 호출 형식을 본문 텍스트로 출력한다.
UNPARSED_TOOL_CALL = re.compile(r"<tool_call>|</tool_call>|<function=|<\|tool_call|\"tool_calls\"\s*:", re.I)


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments_text: str
    # 공급자 고유 필드(예: Gemini thought signature)는 다음 요청에 그대로 돌려준다.
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AssistantTurn:
    """kind: tool_calls, final, truncated_final, truncated_tool_calls, unparsed_tool_call, empty, invalid"""

    kind: str
    content: str = ""
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)

    def history_message(self) -> Dict[str, Any]:
        """다음 모델 요청에 보낼 assistant 메시지. tool_calls 원형과 순서를 보존한다."""
        message: Dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            message["tool_calls"] = [
                {**call.extra, "id": call.id, "type": "function",
                 "function": {"name": call.name, "arguments": call.arguments_text}}
                for call in self.tool_calls
            ]
        return message


def _usage(data: Dict[str, Any]) -> Dict[str, Any]:
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return {}
    return {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool)}


def parse_assistant_turn(data: Any) -> AssistantTurn:
    if not isinstance(data, dict):
        return AssistantTurn("invalid")
    choices = data.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
    message = choice.get("message") if choice else None
    if not isinstance(message, dict):
        return AssistantTurn("invalid", usage=_usage(data))
    finish_reason = str(choice.get("finish_reason") or "") if choice else ""
    content = message.get("content")
    content = content if isinstance(content, str) else ""
    raw_calls = message.get("tool_calls")
    usage = _usage(data)
    if raw_calls not in (None, []):
        if not isinstance(raw_calls, list):
            return AssistantTurn("invalid", content, finish_reason=finish_reason, usage=usage)
        calls: List[ToolCall] = []
        for raw in raw_calls:
            function = raw.get("function") if isinstance(raw, dict) else None
            call_id = raw.get("id") if isinstance(raw, dict) else None
            name = function.get("name") if isinstance(function, dict) else None
            arguments = function.get("arguments") if isinstance(function, dict) else None
            if not isinstance(call_id, str) or not call_id.strip() or not isinstance(name, str) or not name.strip():
                # id 없이는 결과를 대응시킬 수 없으므로 응답 전체를 실행하지 않는다.
                return AssistantTurn("invalid", content, finish_reason=finish_reason, usage=usage)
            if isinstance(arguments, dict):
                arguments = json.dumps(arguments, ensure_ascii=False)
            extra = {key: value for key, value in raw.items() if key not in {"id", "type", "function", "index"}}
            calls.append(ToolCall(call_id, name.strip(), arguments if isinstance(arguments, str) else "", extra))
        kind = "truncated_tool_calls" if finish_reason == "length" else "tool_calls"
        return AssistantTurn(kind, content, tuple(calls), finish_reason, usage)
    if UNPARSED_TOOL_CALL.search(content):
        return AssistantTurn("unparsed_tool_call", content, finish_reason=finish_reason, usage=usage)
    if content.strip():
        kind = "truncated_final" if finish_reason == "length" else "final"
        return AssistantTurn(kind, content, finish_reason=finish_reason, usage=usage)
    return AssistantTurn("empty", finish_reason=finish_reason, usage=usage)


def parse_arguments(call: ToolCall) -> tuple[Optional[Dict[str, Any]], str]:
    """인자 문자열을 객체로 바꾼다. 실패 코드는 도구 결과로 모델에 돌려준다."""
    text = call.arguments_text.strip() or "{}"
    try:
        value = json.loads(text)
    except ValueError:
        return None, "argument_parse_error"
    if not isinstance(value, dict):
        return None, "invalid_arguments"
    return value, ""
