"""FastAPI 요청 본문 pydantic 모델(Layer 1).

pydantic 외의 backend 모듈을 참조하지 않는다(순환 방지).
"""

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, model_validator

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
    "ConversationMessage",
    "PlaybookCandidate",
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
    notionActionId: str = Field(default="", max_length=32)
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=30000)


class ChatRequest(BaseModel):
    modelId: str = Field(min_length=1, max_length=128)
    messages: List[ChatMessage] = Field(min_length=1, max_length=80)
    temperature: float = 0.3
    topP: Optional[float] = None
    topK: Optional[int] = None
    minP: Optional[float] = None
    maxTokens: Optional[int] = None
    presencePenalty: Optional[float] = None
    frequencyPenalty: Optional[float] = None
    repetitionPenalty: Optional[float] = None
    seed: Optional[int] = None
    stop: Optional[List[str]] = Field(default=None, max_length=10)
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
    contextVariant: Literal["A", "B"] = "A"

    @model_validator(mode="after")
    def validate_total_message_size(self):
        if sum(len(message.content) for message in self.messages) > 120000:
            raise ValueError("메시지 전체 길이는 120,000자를 넘을 수 없습니다.")
        if self.stop and any(len(item) > 200 for item in self.stop):
            raise ValueError("중단 문자열은 항목당 200자를 넘을 수 없습니다.")
        return self


class ConversationSourceDetail(BaseModel):
    screen_number: str = Field(default="", max_length=128)
    work_subject: str = Field(default="", max_length=256)
    description: str = Field(default="", max_length=4000)
    content: str = Field(default="", max_length=4000)
    database_label: str = Field(default="", max_length=256)
    status: str = Field(default="", max_length=256)
    owner: str = Field(default="", max_length=256)
    due_date: str = Field(default="", max_length=64)
    last_edited_time: str = Field(default="", max_length=64)
    url: str = Field(default="", max_length=2000)
    effort_fields: Dict[str, Any] = Field(default_factory=dict)
    date_fields: Dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def cap_dynamic_fields(self):
        self.effort_fields = {
            str(key)[:128]: str(value)[:500]
            for key, value in list(self.effort_fields.items())[:30]
        }
        self.date_fields = {
            str(key)[:128]: str(value)[:500]
            for key, value in list(self.date_fields.items())[:30]
        }
        return self


class ConversationSource(BaseModel):
    type: str = Field(default="", max_length=32)
    source: str = Field(default="", max_length=64)
    issue_id: Optional[int] = None
    page_id: str = Field(default="", max_length=128)
    title: str = Field(default="", max_length=1000)
    summary: str = Field(default="", max_length=1200)
    project: str = Field(default="", max_length=500)
    status: str = Field(default="", max_length=256)
    handler: str = Field(default="", max_length=256)
    owner: str = Field(default="", max_length=256)
    created_at: str = Field(default="", max_length=64)
    updated_at: str = Field(default="", max_length=64)
    due_date: str = Field(default="", max_length=64)
    database_label: str = Field(default="", max_length=256)
    preview: str = Field(default="", max_length=4000)
    content: str = Field(default="", max_length=4000)
    document: str = Field(default="", max_length=4000)
    url: str = Field(default="", max_length=2000)
    detail: Optional[ConversationSourceDetail] = None


class PlaybookCandidate(BaseModel):
    title: str = Field(default="", max_length=200)
    symptom: str = Field(default="", max_length=4000)
    cause: str = Field(default="", max_length=4000)
    resolution: str = Field(default="", max_length=8000)
    verification: str = Field(default="", max_length=4000)
    tags: List[str] = Field(default_factory=list, max_length=12)
    sourceIssueIds: List[int] = Field(default_factory=list, max_length=20)


class ConversationMessage(BaseModel):
    notionActionId: str = Field(default="", max_length=32)
    role: Literal["user", "assistant"]
    content: str = Field(default="", max_length=30000)
    sources: List[ConversationSource] = Field(default_factory=list, max_length=20)
    followUps: List[str] = Field(default_factory=list, max_length=10)
    verification: Optional[Dict[str, Any]] = None
    auditId: str = Field(default="", max_length=64)
    templateId: str = Field(default="", max_length=64)
    feedback: Optional[Dict[str, Any]] = None
    abTest: Optional[Dict[str, Any]] = None
    followupVariant: Literal["A", "B"] = "A"
    # 차트 payload 를 스키마에 두지 않으면 서버 저장 시 버려져
    # 다른 기기나 재로그인 후 차트가 사라진다.
    effortChart: Optional[Dict[str, Any]] = None
    effortRanking: Optional[Dict[str, Any]] = None
    effortTrend: Optional[Dict[str, Any]] = None
    playbookCandidate: Optional[PlaybookCandidate] = None
    playbookSaved: bool = False
    createdAt: str = Field(default="", max_length=64)
    questionAskedAt: str = Field(default="", max_length=64)

    @model_validator(mode="after")
    def cap_follow_ups(self):
        self.followUps = [str(item)[:1000] for item in self.followUps]
        return self


class ChromaSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    limit: int = Field(default=5, ge=1, le=100)


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
