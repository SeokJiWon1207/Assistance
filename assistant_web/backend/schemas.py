"""FastAPI 요청 본문 pydantic 모델(Layer 1).

pydantic 외의 backend 모듈을 참조하지 않는다(순환 방지).
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

__all__ = [
    "ChatMessage",
    "ChatRequest",
    "ChromaSearchRequest",
    "NotionPageWriteRequest",
    "NotionPageUpdateRequest",
    "EvaluationRunRequest",
    "AccuracyRunRequest",
    "AnswerFeedbackRequest",
    "FilterClause",
    "SortClause",
    "StructuredQueryPlan",
]


FilterOperator = Literal[
    "equals",
    "not_equals",
    "in",
    "not_in",
    "contains",
    "not_contains",
    "exists",
    "not_exists",
    "gte",
    "lte",
    "between",
]


class FilterClause(BaseModel):
    """데이터 소스와 무관한 단일 검색 조건.

    자연어 표현을 이 공통 형태로 먼저 바꾼 뒤 Mantis/Notion 어댑터가
    각 API가 이해하는 필터 또는 로컬 후처리로 컴파일한다.
    """

    field: str = Field(min_length=1, max_length=64)
    operator: FilterOperator
    value: Any = None


class SortClause(BaseModel):
    field: str = Field(min_length=1, max_length=64)
    direction: Literal["asc", "desc"] = "desc"


class StructuredQueryPlan(BaseModel):
    version: int = 2
    source: Literal["mantis", "notion", "both", "general"] = "general"
    operation: Literal["search", "count", "get", "aggregate"] = "search"
    filters: List[FilterClause] = Field(default_factory=list)
    sort: List[SortClause] = Field(default_factory=list)
    group_by: Optional[str] = None
    limit: Optional[int] = Field(default=None, ge=1, le=1000)
    terms: List[str] = Field(default_factory=list)


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
    # 선택 모델과 동일한 검색 근거로 비교할 후보 모델. UI에서 생략하면
    # AB_COMPARISON_MODEL_ID 환경변수를 사용한다.
    abModelId: Optional[str] = None
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
    followUpScenarios: int = 4


class AnswerFeedbackRequest(BaseModel):
    auditId: str = Field(pattern=r"^[0-9a-f]{32}$")
    rating: Literal["helpful", "needs_improvement"]
    reason: Optional[
        Literal[
            "missing_results",
            "wrong_filters",
            "incorrect_facts",
            "stale_data",
            "hard_to_read",
            "other",
        ]
    ] = None
    comment: str = Field(default="", max_length=500)
    templateId: Optional[str] = Field(default=None, max_length=64)
