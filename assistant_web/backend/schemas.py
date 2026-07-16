"""FastAPI 요청 본문 pydantic 모델(Layer 1).

pydantic 외의 backend 모듈을 참조하지 않는다(순환 방지).
app.py는 `from backend.schemas import *`로 이 모델들을 그대로 재노출한다.
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChromaSearchRequest",
    "NotionPageWriteRequest",
    "NotionPageUpdateRequest",
    "EvaluationRunRequest",
    "AccuracyRunRequest",
]


class ChatMessage(BaseModel):
    role: str = Field(pattern="^(system|user|assistant)$")
    content: str


class ChatRequest(BaseModel):
    modelId: str
    messages: List[ChatMessage]
    temperature: float = 0.3
    topP: Optional[float] = None
    topK: Optional[int] = None
    minP: Optional[float] = None
    maxTokens: Optional[int] = None
    presencePenalty: Optional[float] = None
    frequencyPenalty: Optional[float] = None
    repetitionPenalty: Optional[float] = None
    seed: Optional[int] = None
    stop: Optional[List[str]] = None
    responseFormat: str = "text"
    answerStyle: str = "normal"
    searchLimit: Optional[int] = None
    showSources: bool = True
    abTest: bool = True
    stream: bool = False
    tools: Dict[str, bool] = Field(default_factory=dict)
    # 직전 assistant 답변의 감사 로그 ID. "그중 3번째" 같은 서수 참조를
    # 그 답변에서 실제로 표시한 결과 목록으로 해석하는 데 사용한다(선택).
    contextAuditId: Optional[str] = None


class ChromaSearchRequest(BaseModel):
    query: str
    limit: int = 5


class NotionPageWriteRequest(BaseModel):
    databaseId: Optional[str] = None
    databaseLabel: Optional[str] = None
    title: str
    status: Optional[str] = None
    owner: Optional[str] = None
    dueDate: Optional[str] = None
    pageType: Optional[str] = None
    customer: Optional[str] = None
    media: Optional[str] = None
    memo: Optional[str] = None
    content: Optional[str] = None
    properties: Dict[str, Any] = Field(default_factory=dict)


class NotionPageUpdateRequest(BaseModel):
    title: Optional[str] = None
    status: Optional[str] = None
    owner: Optional[str] = None
    dueDate: Optional[str] = None
    pageType: Optional[str] = None
    customer: Optional[str] = None
    media: Optional[str] = None
    memo: Optional[str] = None
    content: Optional[str] = None
    properties: Dict[str, Any] = Field(default_factory=dict)


class EvaluationRunRequest(BaseModel):
    caseIds: List[str] = Field(default_factory=list)
    stream: bool = False


class AccuracyRunRequest(BaseModel):
    questions: int = 10
