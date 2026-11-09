"""홈 추천 조회, 갱신, 숨김, 입력 준비. 실제 질문은 기존 /api/chat이 처리한다."""
from datetime import datetime, timezone
from fastapi import APIRouter, HTTPException, Query, Request
from backend.auth import current_request_user
from backend.suggestions.contracts import SuggestionBundle
from backend.suggestions.settings import enabled
from backend.suggestions.service import get_service

router = APIRouter(prefix='/api/suggestions', tags=['suggestions'])


def _user(request: Request, *, mutation: bool = False) -> dict:
    user = current_request_user(request)
    if not user:
        raise HTTPException(401, '로그인이 필요합니다.')
    if mutation and not enabled():
        raise HTTPException(409, '업무 추천 기능이 비활성화되어 있습니다.')
    return user


@router.get('')
def suggestions(request: Request, limit: int = Query(default=3, ge=1, le=3)) -> SuggestionBundle:
    user = _user(request)
    if not enabled():
        return SuggestionBundle(state='disabled')
    return get_service().read(user, limit, datetime.now(timezone.utc))


@router.post('/refresh', status_code=202)
def refresh(request: Request) -> SuggestionBundle:
    user = _user(request, mutation=True)
    return get_service().refresh(user, datetime.now(timezone.utc))


@router.post('/{identifier}/dismiss')
def dismiss(identifier: str, request: Request) -> dict:
    user = _user(request, mutation=True)
    get_service().dismiss(user, identifier, datetime.now(timezone.utc))
    return {'dismissed': True}


@router.post('/{identifier}/prepare')
def prepare(identifier: str, request: Request) -> dict:
    user = _user(request, mutation=True)
    return get_service().prepare(user, identifier, datetime.now(timezone.utc))
