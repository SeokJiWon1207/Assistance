"""점검 모드 상태 관리 — 플래그 파일 하나로 켜고 끈다(Layer 0).

`scripts/local/AI비서_점검모드.command`가 플래그 파일을 만들고 지운다.
서버를 재시작하지 않아도 즉시 반영되도록, 상태는 캐시하지 않고 요청마다 읽는다.
다른 backend 모듈을 참조하지 않는다(순환 방지).
"""

import json
from datetime import datetime
from typing import Any, Dict, Optional

from backend.config import RUNTIME_DIR


MAINTENANCE_FLAG_PATH = RUNTIME_DIR / "maintenance.json"
DEFAULT_MAINTENANCE_MESSAGE = "서버 점검중입니다."


def get_maintenance_state() -> Dict[str, Any]:
    """점검 모드 상태를 돌려준다. 플래그 파일이 없으면 비활성."""
    if not MAINTENANCE_FLAG_PATH.exists():
        return {"active": False, "message": "", "startedAt": "", "note": ""}
    try:
        data = json.loads(MAINTENANCE_FLAG_PATH.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        # 파일이 깨졌더라도 점검 중이라는 사실 자체는 유지한다. 점검 의도로 만든
        # 파일이 있는데 파싱 실패를 이유로 서비스를 열어버리면 더 위험하다.
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {
        "active": True,
        "message": str(data.get("message") or DEFAULT_MAINTENANCE_MESSAGE),
        "startedAt": str(data.get("startedAt") or ""),
        "note": str(data.get("note") or ""),
    }


def start_maintenance(message: str = "", note: str = "") -> Dict[str, Any]:
    """점검 모드를 켠다. 이미 켜져 있으면 시작 시각을 유지한다."""
    current = get_maintenance_state()
    started_at = current["startedAt"] if current["active"] and current["startedAt"] else (
        datetime.now().astimezone().isoformat(timespec="seconds")
    )
    payload = {
        "message": message.strip() or DEFAULT_MAINTENANCE_MESSAGE,
        "startedAt": started_at,
        "note": note.strip(),
    }
    MAINTENANCE_FLAG_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"active": True, **payload}


def stop_maintenance() -> Dict[str, Any]:
    """점검 모드를 끈다. 켜져 있지 않아도 오류로 보지 않는다."""
    MAINTENANCE_FLAG_PATH.unlink(missing_ok=True)
    return get_maintenance_state()


def build_maintenance_chat_response(model_id: str) -> Dict[str, Any]:
    """점검 중일 때 채팅 요청에 돌려줄 답변.

    오류가 아니라 정상 응답으로 돌려준다. 사용자가 보는 것은 실패 메시지가
    아니라 안내여야 하고, 프런트엔드도 평소 흐름 그대로 표시하면 된다."""
    state = get_maintenance_state()
    lines = [f"🛠️ {state['message']}"]
    if state["note"]:
        lines.extend(["", state["note"]])
    if state["startedAt"]:
        lines.extend(["", f"점검 시작: {state['startedAt']}"])
    lines.extend(["", "점검이 끝나면 다시 이용하실 수 있습니다."])
    return {
        "modelId": model_id,
        "content": "\n".join(lines),
        "sources": [],
        "retrieval": None,
        "raw": {"mode": "maintenance"},
        "verification": None,
        "auditId": "",
        "followUps": [],
    }
