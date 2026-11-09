"""추천 데이터 계약. 원천 문자열은 명령이 아닌 데이터다."""
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field

Relation = Literal['owner', 'related', 'contributor']
BundleState = Literal['disabled', 'ready', 'pending', 'partial', 'unavailable']
Action = Literal['review_progress', 'summarize_changes', 'compare_records', 'prepare_checklist']


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid')


class SourceRef(Contract):
    type: Literal['mantis', 'notion']
    id: str
    title: str
    url: str
    revision: str
    observedAt: datetime
    validUntil: datetime
    grade: Literal['properties', 'body'] = 'properties'
    relation: Relation = 'owner'


class Candidate(Contract):
    id: str
    kind: Action = 'review_progress'
    sources: list[SourceRef]
    facts: dict[str, Any] = Field(default_factory=dict)
    allowedActions: list[Action] = Field(default=['review_progress'])
    dedupeKey: str
    revision: str
    priority: int = 0
    relevance: int = 0


class Suggestion(Contract):
    id: str
    kind: Action
    title: str
    promptPreview: str
    reasonCode: str
    revision: str
    dedupeKey: str
    sources: list[SourceRef]
    generatedAt: datetime
    expiresAt: datetime


class EvidenceBatch(Contract):
    candidates: list[Candidate] = Field(default_factory=list)
    sourceStatus: dict[str, str] = Field(default_factory=dict)
    scopeKey: str = ''


class SuggestionBundle(Contract):
    items: list[Suggestion] = Field(default_factory=list)
    state: BundleState = 'unavailable'
    generationId: str = ''
    sourceStatus: dict[str, str] = Field(default_factory=dict)
    retryAfterSeconds: int = 0


class Lease(Contract):
    token: str
    user_uid: str
    scope_key: str
    expires_at: datetime
