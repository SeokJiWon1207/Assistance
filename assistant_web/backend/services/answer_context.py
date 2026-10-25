"""선택 답변을 동일 사용자·질문의 서버 기록에서 복원한다."""
from fastapi import HTTPException
from pydantic import ValidationError

from backend.audit import load_audit_answer
from backend.schemas import ChatRequest


def bind_answer_context(request: ChatRequest, user: dict | None) -> ChatRequest:
    if request.contextVariant == "A":
        return request
    selected = None
    # 중지·오류 응답에는 감사 ID가 없다. 그 앞의 성공한 답변과 정확히 같은 질문을 찾는다.
    for index in range(len(request.messages) - 2, -1, -1):
        if request.messages[index].role != "assistant":
            continue
        question = next((message.content for message in reversed(request.messages[:index]) if message.role == "user"), "")
        answer = load_audit_answer(request.contextAuditId or "", str((user or {}).get("uid") or ""), question, variant="B")
        if answer is not None:
            selected = index, answer
            break
    if selected is None:
        raise HTTPException(409, "선택한 B 답변의 저장된 문맥을 확인하지 못했습니다. A 답변을 선택하거나 질문을 다시 입력해 주세요.")
    index, answer = selected
    messages = list(request.messages)
    messages[index] = messages[index].model_copy(update={"content": answer})
    # 서버에서 복원한 답변도 요청 전체의 크기 제한을 지킨다.
    try:
        return ChatRequest.model_validate({**request.model_dump(), "messages": [message.model_dump() for message in messages]})
    except ValidationError as exc:
        raise HTTPException(409, "선택한 B 답변이 문맥 크기 제한을 초과했습니다. 새 대화에서 질문해 주세요.") from exc
