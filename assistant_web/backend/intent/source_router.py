"""질문의 검색 필요성과 최종 데이터 소스 근거를 판정한다."""

import re
from enum import Enum
from typing import Any, Dict, List

from backend.common import normalize_for_match
from backend.intent.constants import (
    COMBINED_SOURCE_MARKERS,
    EXPLICIT_MANTIS_MARKERS,
    EXPLICIT_NOTION_MARKERS,
    GENERAL_INTENT_KEYWORDS,
    MANTIS_TARGET_OBJECT_MARKERS,
    NOTION_TARGET_OBJECT_MARKERS,
    RETRIEVAL_INTENT_KEYWORDS,
    TRIAGE_REQUEST_PATTERN,
)
from backend.intent.entities import is_simple_greeting
from backend.intent.general import is_general_question
from backend.query_planning import extract_group_by


class SourceAuthority(str, Enum):
    AUTHORITATIVE = "authoritative"
    ADVISORY = "advisory"
    ABSTAIN = "abstain"


def _contains_normalized_marker(normalized: str, markers: Any) -> bool:
    return any(normalize_for_match(marker) in normalized for marker in markers)


def rule_based_search_intent(text: str, constraints: Dict[str, Any]) -> Dict[str, Any]:
    """소스 대상과 검색 범위를 분리해 규칙 기반 검색 소스를 제안한다."""
    normalized = normalize_for_match(text)
    explicit_notion = _contains_normalized_marker(normalized, EXPLICIT_NOTION_MARKERS)
    explicit_mantis = _contains_normalized_marker(normalized, EXPLICIT_MANTIS_MARKERS)
    combined_marker = _contains_normalized_marker(normalized, COMBINED_SOURCE_MARKERS)
    notion_target = _contains_normalized_marker(normalized, NOTION_TARGET_OBJECT_MARKERS)
    notion_target = notion_target or bool(
        re.search(
            r"(?:신입사원|교육|업무|운영|프로세스|절차).{0,16}(?:문서|자료)"
            r"|(?:문서|자료).{0,16}(?:신입사원|교육|업무|운영|프로세스|절차)",
            normalized,
        )
    )
    mantis_target = _contains_normalized_marker(normalized, MANTIS_TARGET_OBJECT_MARKERS) or bool(
        TRIAGE_REQUEST_PATTERN.search(text)
    )
    platform_group = extract_group_by(text) == "platform"
    grouped_issue_analysis = bool(
        extract_group_by(text)
        and re.search(r"이슈.{0,16}(?:추이|비교|분석|요약|경향)", normalized)
    )
    exact_mantis_target = bool(constraints.get("issue_ids") or constraints.get("screen_numbers"))
    issue_request = bool(
        re.search(
            r"(?:이슈|장애|오류|버그).{0,16}(?:보여|찾아|조회|목록|리스트|건수|몇|상세|알려|정리)"
            r"|(?:보여|찾아|조회|목록|리스트|건수|몇|상세|알려|정리).{0,16}(?:이슈|장애|오류|버그)",
            normalized,
        )
    )
    # "가장 최근 이슈 하나만"처럼 동사 없이 대상과 정렬 표현만 남은 목록 요청도
    # Mantis 조회다. 규칙이 general로 물러나면 최종 계획의 소스도 general이 되어
    # 검색 단계가 하나도 실행되지 않고, 근거 없이 "찾지 못했다"는 답변이 나간다.
    issue_list_request = bool(
        re.search(
            r"(?:최근|최신|마지막).{0,8}(?:이슈|장애|오류|버그)"
            r"|(?:이슈|장애|오류|버그).{0,8}(?:최근|최신)",
            normalized,
        )
    )
    document_hint = _contains_normalized_marker(
        normalized,
        ("문서", "자료", "신입사원", "교육"),
    )
    filterable_person = bool(constraints.get("people", {}).get("names"))
    scope_only = bool(
        constraints.get("customer_key")
        or constraints.get("platform_key")
        or constraints.get("people", {}).get("names")
        or constraints.get("created_at")
        or constraints.get("statuses")
        or constraints.get("date_fields")
    )
    scoped_issue = scope_only and bool(re.search(r"이슈|버그", normalized))
    evidence: List[Dict[str, str]] = []

    if is_simple_greeting(text) or is_general_question(text):
        return {
            "source": "general",
            "terms": [],
            "reason": "사내 조회가 필요 없는 일반 질문",
            "planned": True,
            "planner": "rule",
            "confidence": 1.0,
            "authority": SourceAuthority.AUTHORITATIVE.value,
            "evidence": [],
        }
    if explicit_notion and explicit_mantis:
        source, authority, reason = "both", SourceAuthority.AUTHORITATIVE, "두 검색 소스를 모두 명시"
        evidence.append({"kind": "explicit_source", "source": "both"})
    elif combined_marker and notion_target and (mantis_target or exact_mantis_target or issue_request):
        source, authority, reason = "both", SourceAuthority.AUTHORITATIVE, "두 검색 소스의 비교·교차를 명시"
        evidence.append({"kind": "explicit_combination", "source": "both"})
    elif explicit_notion:
        source, authority, reason = "notion", SourceAuthority.AUTHORITATIVE, "Notion을 검색 대상으로 명시"
        evidence.append({"kind": "explicit_source", "source": "notion"})
    elif explicit_mantis:
        source, authority, reason = "mantis", SourceAuthority.AUTHORITATIVE, "Mantis를 검색 대상으로 명시"
        evidence.append({"kind": "explicit_source", "source": "mantis"})
    elif exact_mantis_target:
        source, authority, reason = "mantis", SourceAuthority.AUTHORITATIVE, "이슈 식별자를 인식"
        evidence.append({"kind": "exact_identifier", "source": "mantis"})
    elif notion_target:
        source, authority, reason = "notion", SourceAuthority.AUTHORITATIVE, "문서 대상 표현을 인식"
        evidence.append({"kind": "target_object", "source": "notion"})
    elif mantis_target or issue_request or issue_list_request or platform_group or grouped_issue_analysis or scoped_issue:
        source, authority, reason = "mantis", SourceAuthority.AUTHORITATIVE, "이슈 대상 표현을 인식"
        evidence.append({"kind": "target_object", "source": "mantis"})
    elif document_hint:
        source, authority, reason = "notion", SourceAuthority.ADVISORY, "문서·자료 표현을 인식"
        evidence.append({"kind": "topic", "source": "notion"})
    elif filterable_person:
        source, authority, reason = "mantis", SourceAuthority.ADVISORY, "담당자 검색 범위를 인식"
        evidence.append({"kind": "scope", "source": "mantis"})
    elif scope_only:
        source, authority, reason = "general", SourceAuthority.ABSTAIN, "검색 범위만 인식해 소스 판단을 보류"
        evidence.append({"kind": "scope", "source": "general"})
    elif any(normalize_for_match(keyword) in normalized for keyword in GENERAL_INTENT_KEYWORDS):
        source, authority, reason = "general", SourceAuthority.AUTHORITATIVE, "일반 대화 키워드를 인식"
    else:
        source, authority, reason = "general", SourceAuthority.ABSTAIN, "명확한 검색 소스를 규칙으로 확정하지 못함"

    confidence = {
        SourceAuthority.AUTHORITATIVE: 1.0,
        SourceAuthority.ADVISORY: 0.75,
        SourceAuthority.ABSTAIN: 0.35,
    }[authority]

    return {
        "source": source,
        "terms": [],
        "reason": reason,
        "planned": True,
        "planner": "rule",
        "confidence": confidence,
        "authority": authority.value,
        "evidence": evidence,
    }


def should_run_retrieval(text: str, constraints: Dict[str, Any]) -> bool:
    if is_simple_greeting(text) or is_general_question(text):
        return False
    normalized_text = normalize_for_match(text)
    if has_notion_retrieval_signal(text):
        return True
    if constraints.get("customer_key") or constraints.get("platform_key"):
        return True
    if (
        constraints.get("issue_ids")
        or constraints.get("screen_numbers")
        or constraints.get("statuses")
        or constraints.get("date_fields")
    ):
        return True
    if constraints.get("people", {}).get("names"):
        return True
    if any(normalize_for_match(keyword) in normalized_text for keyword in RETRIEVAL_INTENT_KEYWORDS):
        return True
    if any(normalize_for_match(keyword) in normalized_text for keyword in GENERAL_INTENT_KEYWORDS):
        return False
    return False


def has_notion_retrieval_signal(text: str) -> bool:
    """Notion 표현이 검색 파이프라인 시작 근거인지 판단한다.

    실제 Notion 단계 실행 여부는 최종 ``plan_source``가 결정한다.
    """
    normalized_text = normalize_for_match(text)
    if is_general_question(text):
        return False
    return bool(
        _contains_normalized_marker(normalized_text, EXPLICIT_NOTION_MARKERS)
        or _contains_normalized_marker(normalized_text, NOTION_TARGET_OBJECT_MARKERS)
        or _contains_normalized_marker(normalized_text, ("문서", "자료", "신입사원", "교육"))
    )


# 공개 import 호환성은 유지하되 새 호출부는 역할이 드러나는 이름을 사용한다.
should_run_notion_search = has_notion_retrieval_signal


__all__ = [
    "SourceAuthority",
    "rule_based_search_intent",
    "should_run_retrieval",
    "has_notion_retrieval_signal",
    "should_run_notion_search",
]
