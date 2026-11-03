"""인증 사용자와 개인 조회 범위를 판단하는 공유 규칙."""

import re
from typing import Any, Dict, Optional

from backend.domain.personal_scope import (
    PERSONAL_SCOPE_COMPACT_PATTERN as PERSONAL_SCOPE_COMPACT_PATTERN,
    PERSONAL_SCOPE_DIRECT_PATTERN as PERSONAL_SCOPE_DIRECT_PATTERN,
    PERSONAL_SCOPE_GROUP_MARKERS as PERSONAL_SCOPE_GROUP_MARKERS,
    PERSONAL_SCOPE_SPACED_PATTERN as PERSONAL_SCOPE_SPACED_PATTERN,
    has_personal_mantis_scope_phrase as has_personal_mantis_scope_phrase,
)
from backend.intent.constraints import extract_query_constraints


IDENTITY_QUESTIONS = {
    "내가누구야",
    "나는누구야",
    "내가누구지",
    "나는누구지",
    "내이름뭐야",
    "내이름이뭐야",
    "내정보알려줘",
    "내계정정보알려줘",
    "내로그인정보알려줘",
    "지금로그인한사람누구야",
}


PERSONAL_SCOPE_PRONOUN_NAMES = {
    "나", "나에게", "나한테", "내", "내게", "내가",
    "저", "저에게", "저한테", "제", "제가", "본인",
}


NOTION_PERSONAL_SCOPE_PATTERN = re.compile(
    r"(?:^|[\s,])(?:"
    r"(?:내|나의|제)\s*(?:관련\s*(?:된|있는)?\s*)?(?:문서|항목|페이지|업무|자료)|"
    r"(?:나|저)(?:랑|와|과)\s*관련\s*(?:된|있는)?\s*(?:문서|항목|페이지|업무|자료)|"
    r"(?:내가|제가)\s*(?:담당|맡|작성|등록|참여|기여)[가-힣]*\s*(?:문서|항목|페이지|업무|자료)?|"
    r"(?:내게|나에게|나한테|저에게|저한테)\s*(?:할당|배정|지정|맡겨)[가-힣]*"
    r")",
    re.IGNORECASE,
)


def is_identity_question(text: str) -> bool:
    normalized = re.sub(r"[\s?!.,]+", "", str(text or "")).casefold()
    return normalized in IDENTITY_QUESTIONS


def format_authenticated_identity(user: Optional[Dict[str, Any]]) -> str:
    if not user:
        return "현재 로그인한 사용자 정보를 확인할 수 없습니다. 다시 로그인해 주세요."
    role_labels = {"admin": "관리자", "manager": "매니저", "member": "사용자"}
    display_name = str(user.get("displayName") or user.get("email") or "사용자").strip()
    role = role_labels.get(str(user.get("role") or "member"), str(user.get("role") or "사용자"))
    team = str(user.get("team") or "미설정").strip()
    mantis_name = str(user.get("mantisUsername") or display_name).strip()
    return (
        f"{display_name}님으로 로그인되어 있습니다.\n\n"
        f"- 역할: {role}\n"
        f"- 팀: {team}\n"
        f"- Mantis 담당자: {mantis_name}"
    )


def should_scope_to_authenticated_mantis_user(text: str) -> bool:
    """개인 업무 요청인지 판정해 로그인 사용자의 Mantis 담당 범위를 적용한다."""
    normalized = re.sub(r"\s+", "", str(text or "")).casefold()
    if re.search(r"(?:내|제)\s*(?:건|것|이슈|업무)(?:은|는)?\s*(?:말고|빼고|제외)", text):
        return False
    if any(marker in normalized for marker in ("notion", "노션", "노선")):
        return False
    constraints = extract_query_constraints(text)
    if constraints.get("mantis_team_members") and constraints.get("mantis_team_scope") != "default":
        return False
    explicit_names = {
        str(name or "").strip().casefold()
        for name in constraints.get("people", {}).get("names") or []
    } - PERSONAL_SCOPE_PRONOUN_NAMES
    if explicit_names:
        return False
    if any(
        marker in normalized
        for marker in ("업무보고서", "주간보고서", "주간업무보고", "주간업무")
    ):
        # 고객사를 명시한 주간 보고서는 그 고객사 전체 현황이다. 로그인 사용자의
        # 담당 이슈로 다시 좁히면 팀 템플릿의 집계값이 사람마다 달라진다.
        return not bool(constraints.get("customer_key") or constraints.get("customer_exact"))
    return has_personal_mantis_scope_phrase(text) or bool(re.search(r"^(?:나|저)(?:는)?\s+.*(?:뭐부터|무엇부터)", text))


def should_scope_to_authenticated_notion_user(text: str) -> bool:
    """Notion의 1인칭 관련 문서를 로그인 사용자 범위로 해석한다."""
    raw_text = str(text or "").strip()
    normalized = re.sub(r"\s+", "", raw_text).casefold()
    if not any(marker in normalized for marker in ("notion", "노션", "노선")):
        return False
    constraints = extract_query_constraints(raw_text)
    explicit_names = {
        str(name or "").strip().casefold()
        for name in constraints.get("people", {}).get("names") or []
    } - PERSONAL_SCOPE_PRONOUN_NAMES
    if explicit_names:
        return False
    return bool(NOTION_PERSONAL_SCOPE_PATTERN.search(raw_text))


def apply_authenticated_mantis_scope(
    text: str,
    user: Optional[Dict[str, Any]],
    *,
    scope_text: Optional[str] = None,
) -> str:
    """개인 Mantis 요청에 인증 계정의 담당자명을 검색 조건으로 명시한다."""
    mantis_username = str((user or {}).get("mantisUsername") or (user or {}).get("displayName") or "").strip()
    if not mantis_username or not should_scope_to_authenticated_mantis_user(scope_text or text):
        return text
    return f"{text} 담당자: {mantis_username}".strip()


def apply_authenticated_notion_scope(
    text: str,
    user: Optional[Dict[str, Any]],
    *,
    scope_text: Optional[str] = None,
) -> str:
    """개인 Notion 요청에 인증 계정의 표시 이름을 담당 조건으로 명시한다."""
    notion_username = str(
        (user or {}).get("displayName") or (user or {}).get("mantisUsername") or ""
    ).strip()
    if (
        not notion_username
        or "@" in notion_username
        or not should_scope_to_authenticated_notion_user(scope_text or text)
    ):
        return text
    return f"{text} 담당자: {notion_username}".strip()
