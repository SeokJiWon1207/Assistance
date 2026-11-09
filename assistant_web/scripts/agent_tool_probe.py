#!/usr/bin/env python3
"""설정된 회사 모델의 tool calling 최소 호환성을 확인하고 검증 기록을 남긴다.

사내 원문은 보내지 않는다. 가상의 과일 가격 도구와 합성 질문, 가짜 도구 결과만 사용한다.
결과는 커밋하지 않는 runtime/agent_tool_verification.json에 저장하며, 키·endpoint 주소는
출력하거나 기록하지 않는다(주소는 지문만 남긴다).

사용법(저장소 루트):
    assistant_web/.venv/bin/python assistant_web/scripts/agent_tool_probe.py --model-id company-qwen3.6-35b-a3b
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

APP_DIR = Path(__file__).resolve().parents[1]
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

import requests  # noqa: E402

from backend.environment import load_dotenv  # noqa: E402

load_dotenv(APP_DIR / ".env")

from backend import llm  # noqa: E402
from backend.services.agent.policy import config_fingerprint, save_verification, send_url  # noqa: E402
from backend.services.agent.protocol import AssistantTurn, parse_arguments, parse_assistant_turn  # noqa: E402

# 로컬 모델은 에이전트 지원 대상이 아니다(네이티브 API가 tools를 전달하지 않음).
REFUSED_PROVIDERS = {"ollama"}
TOOL = {
    "type": "function",
    "function": {
        "name": "lookup_fruit_price",
        "description": "가상의 과일 가격표에서 한 품목의 가격을 조회한다.",
        "parameters": {
            "type": "object",
            "properties": {"item": {"type": "string", "description": "과일 이름"}},
            "required": ["item"],
            "additionalProperties": False,
        },
    },
}
SYSTEM = "너는 호환성 시험용 도우미다. 과일 가격은 반드시 lookup_fruit_price 도구로 확인한 뒤 답한다."


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _post(model: Dict[str, Any], api_key: str, messages: List[Dict[str, Any]], *, tool_choice: str = "auto",
          max_tokens: int = 512) -> tuple[int, Dict[str, Any], Dict[str, str]]:
    payload = {"model": model.get("model", model["id"]), "messages": messages, "tools": [TOOL],
               "tool_choice": tool_choice, "temperature": 0, "max_tokens": max_tokens, "stream": False}
    response = llm.post_model_chat(model, api_key, payload, timeout=90)
    try:
        status = response.status_code
        headers = {"server": response.headers.get("server", "")}
        try:
            data = response.json() if status < 400 else {"error_excerpt": response.text[:300]}
        except ValueError:
            data = {"error_excerpt": "non-json response"}
        return status, data if isinstance(data, dict) else {}, headers
    finally:
        response.close()


def _observed(data: Dict[str, Any], turn: AssistantTurn) -> Dict[str, Any]:
    choices = data.get("choices") if isinstance(data.get("choices"), list) else []
    message = (choices[0] or {}).get("message") if choices and isinstance(choices[0], dict) else {}
    return {
        "responseModel": data.get("model"), "systemFingerprint": data.get("system_fingerprint"),
        "finishReason": turn.finish_reason, "kind": turn.kind, "toolCallCount": len(turn.tool_calls),
        "reasoningFieldPresent": bool(isinstance(message, dict) and (message.get("reasoning_content")
                                                                     or message.get("reasoning"))),
        "usageReported": bool(turn.usage),
    }


def _served_models(model: Dict[str, Any], api_key: str) -> Dict[str, Any]:
    """vLLM·SGLang 등이 제공하는 모델 목록에서 확인 가능한 서빙 설정만 기록한다."""
    url = send_url(model)
    if not url.endswith("/chat/completions"):
        return {"available": False, "reason": "unknown_endpoint_shape"}
    try:
        response = requests.get(url[: -len("/chat/completions")] + "/models",
                                headers={"Authorization": f"Bearer {api_key}"}, timeout=10)
    except requests.RequestException as exc:
        return {"available": False, "reason": type(exc).__name__}
    try:
        if response.status_code != 200:
            return {"available": False, "reason": f"HTTP {response.status_code}"}
        rows = response.json().get("data") or []
    except ValueError:
        return {"available": False, "reason": "non-json response"}
    finally:
        response.close()
    return {"available": True, "server": response.headers.get("server", ""), "models": [
        {"id": row.get("id"), "ownedBy": row.get("owned_by"), "maxModelLen": row.get("max_model_len"),
         "rootName": Path(str(row.get("root") or "")).name or None}
        for row in rows if isinstance(row, dict)
    ][:5]}


def probe(model_id: str) -> Dict[str, Any]:
    model = llm.get_private_model(model_id)
    if str(model.get("provider", "")).strip().lower() in REFUSED_PROVIDERS:
        raise SystemExit(f"승인 범위 밖의 모델입니다: {model_id}")
    api_key = llm.get_model_api_key(model)
    if model.get("status") != "active" or not api_key:
        raise SystemExit(f"사용 가능한 모델이 아닙니다: {model_id} ({model.get('status')})")
    checks: Dict[str, Any] = {name: False for name in (
        "tools_request_accepted", "tool_calls_returned", "arguments_json", "expected_arguments",
        "tool_roundtrip", "multiple_tool_calls", "tool_choice_none", "tool_choice_required")}
    observations: Dict[str, Any] = {}
    errors: Dict[str, Any] = {}

    single = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "사과 가격을 확인해서 알려줘."}]
    try:
        status, data, headers = _post(model, api_key, single)
    except requests.RequestException as exc:
        errors["single"] = type(exc).__name__
        status, data, headers = 0, {}, {}
    checks["tools_request_accepted"] = status == 200
    observations["httpServerHeader"] = headers.get("server", "")
    if status != 200:
        errors["single"] = errors.get("single") or {"status": status, **data}
    turn = parse_assistant_turn(data) if status == 200 else AssistantTurn("invalid")
    observations["single"] = _observed(data, turn) if status == 200 else {}
    if turn.kind == "tool_calls" and turn.tool_calls:
        checks["tool_calls_returned"] = True
        call = turn.tool_calls[0]
        arguments, error = parse_arguments(call)
        checks["arguments_json"] = arguments is not None and not error
        checks["expected_arguments"] = bool(call.name == "lookup_fruit_price" and arguments
                                            and "사과" in str(arguments.get("item", "")))
        roundtrip = [*single, turn.history_message(), *(
            {"role": "tool", "tool_call_id": item.id,
             "content": json.dumps({"item": "사과", "price_krw": 1234, "source": "가상 가격표"}, ensure_ascii=False)}
            for item in turn.tool_calls)]
        try:
            status2, data2, _ = _post(model, api_key, roundtrip)
            final = parse_assistant_turn(data2) if status2 == 200 else AssistantTurn("invalid")
            observations["roundtrip"] = _observed(data2, final) if status2 == 200 else {"status": status2}
            checks["tool_roundtrip"] = bool(final.kind == "final" and ("1234" in final.content.replace(",", "")))
        except requests.RequestException as exc:
            errors["roundtrip"] = type(exc).__name__
    elif turn.kind == "unparsed_tool_call":
        errors["single"] = "도구 호출이 본문 텍스트로 반환됨(서버 도구 호출 파서 미설정 추정)"

    multi = [{"role": "system", "content": SYSTEM},
             {"role": "user", "content": "사과와 배의 가격을 각각 도구로 확인해서 비교해줘."}]
    try:
        status3, data3, _ = _post(model, api_key, multi)
        turn3 = parse_assistant_turn(data3) if status3 == 200 else AssistantTurn("invalid")
        observations["multiple"] = _observed(data3, turn3) if status3 == 200 else {"status": status3}
        checks["multiple_tool_calls"] = turn3.kind == "tool_calls" and len(turn3.tool_calls) >= 2
    except requests.RequestException as exc:
        errors["multiple"] = type(exc).__name__

    for choice, user_text, key in (("none", "사과 가격을 확인해서 알려줘.", "tool_choice_none"),
                                   ("required", "안녕하세요.", "tool_choice_required")):
        try:
            status4, data4, _ = _post(model, api_key, [{"role": "system", "content": SYSTEM},
                                                       {"role": "user", "content": user_text}], tool_choice=choice)
            turn4 = parse_assistant_turn(data4) if status4 == 200 else AssistantTurn("invalid")
            observations[key] = _observed(data4, turn4) if status4 == 200 else {"status": status4}
            # none은 구조화 호출이 없는 것만으로 부족하다. 본문에 호출 텍스트를 쓰면 미지원으로 본다.
            checks[key] = (status4 == 200 and turn4.kind == "final") if choice == "none" else (
                status4 == 200 and turn4.kind == "tool_calls")
        except requests.RequestException as exc:
            errors[key] = type(exc).__name__

    try:
        # 최종 답변은 tools 없이 요청한다. 같은 대화가 순수 본문으로 끝나는지 확인한다.
        response = llm.post_model_chat(model, api_key, {
            "model": model.get("model", model["id"]), "temperature": 0, "max_tokens": 256, "stream": False,
            "messages": [{"role": "system", "content": "가상의 가격 자료만 사용해 한 문장으로 답한다."},
                         {"role": "user", "content": "가상 가격표: 사과 1234원. 사과 가격을 알려줘."}]}, timeout=90)
        try:
            final_turn = parse_assistant_turn(response.json()) if response.status_code == 200 else AssistantTurn("invalid")
        finally:
            response.close()
        observations["no_tools_final"] = {"kind": final_turn.kind, "finishReason": final_turn.finish_reason}
        checks["no_tools_final"] = final_turn.kind == "final"
    except (requests.RequestException, ValueError) as exc:
        errors["no_tools_final"] = type(exc).__name__
        checks["no_tools_final"] = False

    required = ("tools_request_accepted", "tool_calls_returned", "arguments_json", "tool_roundtrip")
    serving_fingerprints = sorted({str(item.get("systemFingerprint")) for item in observations.values()
                                   if isinstance(item, dict) and item.get("systemFingerprint")})
    return {
        "verifiedAt": datetime.now(timezone.utc).date().isoformat(),
        "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "result": "supported" if all(checks[name] for name in required) else "unsupported",
        "modelId": model["id"], "model": model.get("model", model["id"]),
        "provider": model.get("provider"), "endpointFingerprint": _fingerprint(send_url(model)),
        "configFingerprint": config_fingerprint(model),
        "servingConfig": {**_served_models(model, api_key), "systemFingerprints": serving_fingerprints},
        # 응답의 system_fingerprint가 이 값과 달라지면 서빙 구성이 바뀐 것으로 보고 재검증한다.
        "servingFingerprint": serving_fingerprints[0] if len(serving_fingerprints) == 1 else None,
        "requestOptions": {"tool_choice": ["auto", "none", "required"], "stream": False, "temperature": 0,
                           "think": model.get("think")},
        "checks": checks, "observations": observations, "errors": errors,
        "input": "합성 질문과 가짜 도구 결과만 사용(사내 원문 없음)",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model-id", default="company-qwen3.6-35b-a3b")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    record = probe(args.model_id)
    if not args.no_save:
        save_verification(args.model_id, record)
    print(json.dumps({key: record[key] for key in (
        "verifiedAt", "result", "modelId", "model", "checks", "observations", "errors", "servingConfig",
        "servingFingerprint",
    )}, ensure_ascii=False, indent=2))
    return 0 if record["result"] == "supported" else 2


if __name__ == "__main__":
    raise SystemExit(main())
