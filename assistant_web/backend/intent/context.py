"""대화 문맥, 후속 질문과 지시어 참조를 해석한다."""

import calendar
import os
import re
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar

import requests

from backend.audit import load_audit_sources
from backend.common import normalize_for_match
from backend.intent.constants import (
    DEMONSTRATIVE_ISSUE_PATTERN,
    FOLLOW_UP_CONTINUATION_MARKERS,
    FOLLOW_UP_STRONG_MARKERS,
    FOLLOW_UP_WEAK_MARKERS,
    FOLLOW_UP_WEAK_MAX_LEN,
    KOREAN_ORDINAL_WORDS,
)
from backend.intent.constraints import extract_query_constraints, has_targeting_constraint
from backend.llm import extract_model_content, post_model_chat
from backend.query_planning import extract_created_at_range
from backend.schemas import ChatMessage


Dependency = TypeVar("Dependency", bound=Callable[..., Any])


def _compat_dependency(name: str, default: Dependency) -> Dependency:
    """기존 understanding 경로에 적용된 테스트 패치를 전달한다."""
    core = sys.modules.get("backend.intent.core")
    return getattr(core, name, default) if core is not None else default


def get_latest_user_text(messages: List[ChatMessage]) -> str:
    for message in reversed(messages):
        if message.role == "user":
            return message.content.strip()
    return ""


def get_recent_user_texts(messages: List[ChatMessage], limit: int = 5) -> List[str]:
    texts: List[str] = []
    for message in reversed(messages):
        if message.role == "user":
            content = (message.content or "").strip()
            if content:
                texts.append(content)
            if len(texts) >= limit:
                break
    return texts


def is_follow_up_question(text: str) -> bool:
    normalized = normalize_for_match(text)
    if any(normalize_for_match(marker) in normalized for marker in FOLLOW_UP_STRONG_MARKERS):
        return True
    # "고객사는 그대로 두고 상태만 confirmed로 바꿔줘"처럼 직전 조건의
    # 일부를 유지하면서 다른 조건만 교정하는 문장은 길더라도 명백한 후속이다.
    # 약한 마커의 길이 제한에 맡기면 고객사 상속과 LLM 재작성 모두 건너뛴다.
    if re.search(
        r"(?:고객사|프로젝트|플랫폼|매체|상태|담당자)(?:는|은|를|을)?\s*"
        r"(?:그대로|유지)",
        text,
    ) and re.search(r"(?:만\s*)?.*(?:바꿔|변경|수정)", text):
        return True
    # "핵심 해결 내용을 한 문장으로 요약해줘"처럼 길이 제한을 넘지만 자체
    # 검색 조건이 전혀 없고 직전 결과를 가공해 달라는 요청은 후속 질문이다.
    # 조건이 하나라도 있으면 독립 질문일 수 있으므로 건드리지 않는다.
    if any(
        normalize_for_match(marker) in normalized
        for marker in FOLLOW_UP_CONTINUATION_MARKERS
    ) and not has_targeting_constraint(extract_query_constraints(text)):
        return True
    if len(text.strip()) > FOLLOW_UP_WEAK_MAX_LEN:
        return False
    return any(normalize_for_match(marker) in normalized for marker in FOLLOW_UP_WEAK_MARKERS)


def parse_ordinal_reference(text: str) -> Optional[int]:
    """'그중 3번째', '첫 번째', '마지막 이슈'에서 목록 위치를 뽑는다.

    1-base 인덱스를 반환하고 '마지막'은 -1. 참조 표현이 없으면 None.
    '마지막'은 "마지막으로 하나만 더..." 같은 담화 표지로도 흔히 쓰여서,
    뒤에 목록 항목을 가리키는 명사가 오거나 '그중'과 함께 쓰일 때만 인정한다."""
    match = re.search(r"(\d{1,2})\s*번째", text)
    if match:
        return int(match.group(1))
    match = re.search(r"(첫|두|세|네|다섯|여섯|일곱|여덟|아홉|열)\s*번째", text)
    if match:
        return KOREAN_ORDINAL_WORDS[match.group(1)]
    if re.search(r"첫째|첫번째", text):
        return 1
    if re.search(r"그\s*중\s*마지막|마지막\s*(?:이슈|항목|건|거|것|내용|결과)", text):
        return -1
    return None


def has_demonstrative_issue_reference(text: str) -> bool:
    """'이 이슈의', '그 건은'처럼 직전에 보던 대상을 가리키는 표현인지 판별한다."""
    return bool(DEMONSTRATIVE_ISSUE_PATTERN.search(str(text or "")))


def resolve_reference_from_context(
    text: str,
    audit_id: Optional[str],
    user_uid: str = "",
) -> Optional[str]:
    """서수 참조 후속 질문을 직전 답변의 실제 결과 목록으로 해석한 라우팅 텍스트.

    "그중 3번째 자세히"는 재검색으로 같은 목록이 다시 나오길 기대하는 대신,
    감사 로그에 저장된 '사용자가 실제로 본' 목록의 3번째 항목(이슈ID/제목)을
    직접 가리키게 한다. 해석에 실패하면 None을 반환해 기존 규칙 기반
    상속/LLM 재작성 경로로 폴백한다."""
    audit_source_loader = _compat_dependency("load_audit_sources", load_audit_sources)
    if not audit_id:
        return None
    ordinal = parse_ordinal_reference(text)
    demonstrative = ordinal is None and has_demonstrative_issue_reference(text)
    if ordinal is None and not demonstrative:
        return None
    latest_constraints = extract_query_constraints(text)
    if (
        latest_constraints.get("customer_key")
        or latest_constraints.get("platform_key")
        or latest_constraints.get("issue_ids")
        or latest_constraints.get("screen_numbers")
    ):
        # 자체 조건을 가진 독립 질문이면 서수는 목록 참조가 아닐 가능성이 크다.
        return None
    sources = audit_source_loader(audit_id, user_uid=user_uid)
    if not sources:
        return None
    if demonstrative:
        # 서수 없이 "이 이슈"라고만 하면 직전 답변이 가리키던 대상 하나로 본다.
        # 여러 건을 보여준 뒤라면 어느 것인지 알 수 없으므로 추측하지 않는다.
        issue_sources = [source for source in sources if source.get("issue_id")]
        unique_ids = {int(source["issue_id"]) for source in issue_sources}
        notion_sources = [source for source in sources if source.get("type") == "notion" and source.get("title")]
        if not unique_ids and len(notion_sources) == 1:
            return f'노션에서 "{notion_sources[0]["title"]}" {text}'
        if len(unique_ids) != 1:
            return None
        return f"{unique_ids.pop():07d} {text}"
    assert ordinal is not None
    index = len(sources) - 1 if ordinal == -1 else ordinal - 1
    if index < 0 or index >= len(sources):
        return None
    source = sources[index]
    issue_id = source.get("issue_id")
    if issue_id:
        # extract_issue_ids가 5자리 미만 ID도 인식하도록 7자리 0패딩으로 표기한다.
        return f"{int(issue_id):07d} {text}"
    if source.get("type") == "notion" and source.get("title"):
        return f'노션에서 "{source["title"]}" {text}'
    return None


def resolve_recent_created_at(texts: List[str]) -> Optional[Dict[str, str]]:
    """최신순 텍스트 목록에서 마지막으로 확정된 등록일 범위를 돌려준다.

    "그중 4월 것만"처럼 연도 없이 월만 좁힌 턴은 그 자체로는 범위를 만들지
    못하므로, 더 이전 턴에서 확정된 연도를 물려받아 해석한다."""
    year: Optional[int] = None
    resolved: Optional[Dict[str, str]] = None
    for text in reversed(texts):
        own = extract_created_at_range(text)
        if own:
            resolved = own
            year = int(str(own["from"])[:4])
            continue
        month_match = re.search(r"(?<!\d)(1[0-2]|0?[1-9])\s*월", text)
        if month_match and year and not re.search(r"20\d{2}\s*년", text):
            month = int(month_match.group(1))
            last_day = calendar.monthrange(year, month)[1]
            resolved = {
                "from": f"{year:04d}-{month:02d}-01",
                "to": f"{year:04d}-{month:02d}-{last_day:02d}",
                "precision": "month",
            }
    return resolved


def build_contextual_retrieval_text(messages: List[ChatMessage]) -> Tuple[str, bool]:
    """검색 라우팅용 텍스트를 만든다.

    마지막 사용자 메시지가 자체 조건(고객사/플랫폼/인물/이슈ID/화면번호)을 가진
    독립 질문이면 그대로 사용한다. 자체 조건이 없는 후속 질문이면 직전 사용자 턴에서
    조건을 물려받아 앞에 붙인다. 이 텍스트는 검색/라우팅에만 쓰이고 모델이나 화면에
    사용자 메시지로 노출되지 않는다(모델은 별도로 전체 히스토리를 받는다).

    반환값은 (텍스트, LLM 재작성이 필요한지 여부)다. 두 번째 값은 마지막 메시지가
    후속 질문으로 보이는데도 규칙 기반으로 아무 조건도 상속하지 못했을 때만 True다
    (예: 마커 사전에 없는 새로운 표현으로 참조하는 경우). 이 경우 호출부에서
    LLM 기반 재작성(rewrite_follow_up_query_with_model)으로 보완할 수 있다."""
    recent = get_recent_user_texts(messages, limit=5)
    if not recent:
        return "", False
    latest = recent[0]
    latest_constraints = extract_query_constraints(latest)
    follow_up = is_follow_up_question(latest)
    if (
        latest_constraints.get("customer_key")
        or latest_constraints.get("platform_key")
        or latest_constraints.get("issue_ids")
        or latest_constraints.get("screen_numbers")
        or latest_constraints.get("people", {}).get("names")
    ) and not follow_up:
        return latest, False
    if not follow_up:
        return latest, False

    inherited: List[str] = []
    # 제외 표현은 바로 뒤 단어를 제외 대상으로 삼으므로 항상 맨 뒤에 모아 붙인다.
    # "mts 제외 유진"처럼 앞에 오면 상속한 고객사가 제외 대상으로 뒤집힌다.
    excluded: List[str] = []
    seen_customer = bool(latest_constraints.get("customer_key") or latest_constraints.get("excluded_customer_keys"))
    seen_platform = bool(latest_constraints.get("platform_key") or latest_constraints.get("excluded_platform_keys"))
    seen_status = bool(latest_constraints.get("statuses") or latest_constraints.get("excluded_statuses"))
    seen_issue_ids = False
    seen_screen_numbers = False
    seen_created_at = bool(latest_constraints.get("created_at"))
    # "그중 4월 것만"처럼 연도 없이 월만 좁히는 후속 질문은 이전 턴의 연도를
    # 물려받아야 한다. 물려받지 못하면 모델이 임의의 연도를 채워 넣는다.
    latest_month = re.search(r"(?<!\d)(1[0-2]|0?[1-9])\s*월", latest)
    latest_has_year = bool(re.search(r"20\d{2}\s*년", latest))
    prior_range = None if seen_created_at else resolve_recent_created_at(recent[1:])
    if prior_range:
        prior_year = int(str(prior_range["from"])[:4])
        if latest_month and not latest_has_year:
            month = int(latest_month.group(1))
            last_day = calendar.monthrange(prior_year, month)[1]
            inherited.append(
                f"{prior_year}년 {month}월 1일부터 {prior_year}년 {month}월 {last_day}일까지"
            )
        elif not latest_month and not latest_has_year:
            start, end = str(prior_range["from"]), str(prior_range["to"])
            inherited.append(
                f"{start[:4]}년 {int(start[5:7])}월 {int(start[8:])}일부터 "
                f"{end[:4]}년 {int(end[5:7])}월 {int(end[8:])}일까지"
            )

    for prior in recent[1:]:
        prior_constraints = extract_query_constraints(prior)
        # 이슈ID/화면번호처럼 특정 대상을 정확히 가리키는 조건을 최우선으로 상속한다.
        # 그래야 "이슈 3187번 알려줘" -> "그 이슈 상태 어때?" 같은 후속 질문이
        # 대상을 잃지 않는다.
        if not seen_issue_ids and prior_constraints.get("issue_ids"):
            inherited.extend(str(issue_id) for issue_id in prior_constraints["issue_ids"])
            seen_issue_ids = True
        if not seen_screen_numbers and prior_constraints.get("screen_numbers"):
            # extract_screen_numbers는 대괄호([1234]) 또는 "화면번호" 접두어가 있어야
            # 다시 인식하므로, 상속 시에도 같은 형식으로 감싸야 재추출된다.
            inherited.extend(f"[{number}]" for number in prior_constraints["screen_numbers"])
            seen_screen_numbers = True
        if not seen_customer and prior_constraints.get("customer_aliases"):
            inherited.append(prior_constraints["customer_aliases"][0])
            seen_customer = True
        if not seen_customer and prior_constraints.get("excluded_customer_keys"):
            excluded.extend(f"{value} 제외" for value in prior_constraints["excluded_customer_keys"])
            seen_customer = True
        if not seen_platform and prior_constraints.get("platform_key"):
            inherited.append(prior_constraints["platform_key"])
            seen_platform = True
        if not seen_platform and prior_constraints.get("excluded_platform_keys"):
            excluded.extend(f"{value} 제외" for value in prior_constraints["excluded_platform_keys"])
            seen_platform = True
        if not seen_status and prior_constraints.get("statuses"):
            inherited.extend(prior_constraints["statuses"])
            seen_status = True
        if not seen_status and prior_constraints.get("excluded_statuses"):
            excluded.extend(f"{value} 제외" for value in prior_constraints["excluded_statuses"])
            seen_status = True
        for name in prior_constraints.get("people", {}).get("names") or []:
            if name not in inherited:
                inherited.append(name)
        if seen_customer and seen_platform and seen_status and seen_issue_ids and seen_screen_numbers:
            break

    if not inherited and not excluded:
        return latest, True
    # 교정을 시작하는 담화 표지를 상속 값에 붙이면 'NH 아니'가 되어
    # NH 제외 조건으로 오인된다. 명시적인 'NH 아닌' 등의 필터는 유지한다.
    latest = re.sub(r"^(?:아니|아니요|아니오)\s*[,，]\s*", "", latest)
    return f"{' '.join([*inherited, *excluded])} {latest}".strip(), False


def rewrite_follow_up_query_with_model(
    recent_user_texts: List[str],
    model: Dict[str, Any],
    api_key: str,
) -> Optional[str]:
    """규칙 기반 상속이 실패한 후속 질문을 모델에게 독립형 질문으로 재작성시킨다.

    recent_user_texts는 최신순(가장 최근이 0번)으로 들어온다. 실패하면 None을 반환해
    호출부가 규칙 기반 결과(원문 그대로)로 폴백하게 한다."""
    if len(recent_user_texts) < 2:
        return None
    ordered = list(reversed(recent_user_texts))
    history_block = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(ordered))
    prompt = (
        "아래는 사용자의 최근 대화 턴이다(오래된 순으로 번호가 매겨져 있다). "
        "마지막 번호가 가장 최근 질문이며, 이전 턴을 가리키는 후속 질문이다. "
        "마지막 질문만 그 자체로 검색 가능한 독립형 질문 한 문장으로 다시 써라. "
        "이전 턴에서 언급된 고객사, 담당자, 이슈ID, 화면번호처럼 검색 조건에 필요한 "
        "정보만 반영하고 새로운 사실을 지어내지 마라. "
        "결과는 재작성된 질문 문장 하나만 출력해라. 설명, 번호, 따옴표는 붙이지 마라."
    )
    payload = {
        "model": model.get("model", model["id"]),
        "messages": [
            {"role": "system", "content": prompt},
            {"role": "user", "content": history_block},
        ],
        "temperature": 0,
        "max_tokens": 200,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        response = post_model_chat(
            model,
            api_key,
            payload,
            timeout=max(5, min(int(os.getenv("FOLLOWUP_REWRITE_TIMEOUT_SECONDS", "20")), 45)),
            think=False,
        )
        if response.status_code >= 400:
            return None
        data = response.json()
        content = extract_model_content(data)
    except (requests.RequestException, ValueError, TypeError):
        return None

    rewritten = re.sub(r"^```(?:text)?\s*|\s*```$", "", str(content or "").strip()).strip()
    rewritten = rewritten.strip("\"'“”‘’").strip()
    if not rewritten or len(rewritten) > 300:
        return None
    return rewritten


__all__ = [
    "get_latest_user_text",
    "get_recent_user_texts",
    "is_follow_up_question",
    "parse_ordinal_reference",
    "has_demonstrative_issue_reference",
    "resolve_reference_from_context",
    "resolve_recent_created_at",
    "build_contextual_retrieval_text",
    "rewrite_follow_up_query_with_model",
]
