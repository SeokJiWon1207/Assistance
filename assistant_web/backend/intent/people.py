"""인물 조건의 출처와 실제 필터 적용 여부를 일관되게 관리한다."""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, Optional

from backend.common import is_valid_handler_name, normalize_for_match


FILTERABLE_PERSON_ORIGINS = frozenset({"known", "explicit_unverified"})
COMMON_KOREAN_SURNAMES = frozenset(
    "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주"
    "우구민진지엄채원천방공현함변염여추도소석선설마길연위표명기반라왕금옥육인맹제모"
)
ROLE_FOLLOWING_NOUNS = frozenset({
    "업무", "이슈", "현황", "부서", "목록", "명단", "정보", "내역", "건수",
    "인수인계", "교육", "문서", "자료", "가이드", "규정", "지침", "전달",
})
_ORIGIN_PRIORITY = {
    "inferred": 0,
    "account_unverified": 1,
    "explicit_unverified": 2,
    "known": 3,
}


def empty_people() -> Dict[str, Any]:
    return {"names": [], "role": None, "name_origins": {}, "candidates": []}


def roster_person_origin(name: str) -> str:
    """스냅샷 계정값을 사람으로 확정할 수 있는지 보수적으로 분류한다.

    현재 스냅샷에는 ``svn`` 같은 시스템 계정이 섞여 있고 계정 유형 메타데이터가
    없다. 한글 실명과 공백으로 구분된 영문 실명만 known 으로 올리고, 나머지 계정형
    값은 후보로 남겨 명시적 담당자 요청에서만 필터로 사용할 수 있게 한다.
    """
    value = str(name or "").strip()
    if re.fullmatch(r"[가-힣]{2,4}", value):
        return "known"
    if re.fullmatch(r"[A-Za-z]{2,}(?:[ .'-][A-Za-z]{2,})+", value):
        return "known"
    return "account_unverified"


def plausible_explicit_person_name(name: str) -> bool:
    """명부 밖에서 사용자가 명시한 값을 담당자 이름으로 받을 수 있는지 판정한다."""
    value = str(name or "").strip()
    if not is_valid_handler_name(value) or value in ROLE_FOLLOWING_NOUNS:
        return False
    if re.fullmatch(r"[가-힣]{2,4}", value):
        return value[0] in COMMON_KOREAN_SURNAMES
    return True


def build_people(
    pairs: Iterable[tuple[str, str]],
    role: Optional[str],
) -> Dict[str, Any]:
    """이름·출처 쌍에서 정규화된 people 객체를 만든다."""
    selected: Dict[str, tuple[str, str]] = {}
    order: list[str] = []
    for raw_name, raw_origin in pairs:
        name = str(raw_name or "").strip()
        origin = str(raw_origin or "inferred").strip()
        normalized = normalize_for_match(name)
        if not name or not normalized:
            continue
        current = selected.get(normalized)
        if current is None:
            order.append(normalized)
        if current is None or _ORIGIN_PRIORITY.get(origin, -1) > _ORIGIN_PRIORITY.get(current[1], -1):
            selected[normalized] = (name, origin)

    names: list[str] = []
    origins: Dict[str, str] = {}
    candidates: list[Dict[str, str]] = []
    for normalized in order:
        name, origin = selected[normalized]
        if origin in FILTERABLE_PERSON_ORIGINS:
            names.append(name)
            origins[name] = origin
        else:
            candidates.append({"name": name, "origin": origin})

    return {
        "names": names,
        "role": role if names or candidates else None,
        "name_origins": origins,
        "candidates": candidates,
    }


def _pairs_from_people(people: Dict[str, Any]) -> list[tuple[str, str]]:
    origins = people.get("name_origins") or {}
    pairs = [
        (str(name), str(origins.get(name) or "known"))
        for name in people.get("names") or []
    ]
    pairs.extend(
        (str(item.get("name") or ""), str(item.get("origin") or "inferred"))
        for item in people.get("candidates") or []
        if isinstance(item, dict)
    )
    return pairs


def merge_people(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    """두 people 객체를 합치면서 이름과 출처를 함께 보존한다."""
    role = incoming.get("role") or base.get("role")
    return build_people([*_pairs_from_people(base), *_pairs_from_people(incoming)], role)


def drop_people(people: Dict[str, Any], names: Iterable[str]) -> Dict[str, Any]:
    """지정한 이름을 필터와 후보에서 함께 제거한다."""
    excluded = {normalize_for_match(str(name)) for name in names if str(name).strip()}
    return build_people(
        [
            (name, origin)
            for name, origin in _pairs_from_people(people)
            if normalize_for_match(name) not in excluded
        ],
        people.get("role"),
    )


def filterable_people(people: Dict[str, Any]) -> Dict[str, Any]:
    """실제 검색 필터에 적용 가능한 이름만 반환한다."""
    return build_people(
        [
            (name, origin)
            for name, origin in _pairs_from_people(people)
            if origin in FILTERABLE_PERSON_ORIGINS
        ],
        people.get("role"),
    )


def grounded_model_people(
    grounding_text: str,
    base: Dict[str, Any],
    model_names: Iterable[str],
    role: Optional[str],
) -> Dict[str, Any]:
    """원문에 근거가 있는 모델 담당자만 병합한다.

    규칙 추출이 이미 잡은 이름은 그대로 유지한다. 새 이름은 사용자가 담당자라고
    명시하고 원문에 실제 문자열이 있을 때만 explicit_unverified 로 받는다.
    """
    normalized_text = normalize_for_match(grounding_text)
    base_by_normalized = {
        normalize_for_match(name): (name, origin)
        for name, origin in _pairs_from_people(base)
    }
    incoming: list[tuple[str, str]] = []
    for raw_name in model_names:
        name = str(raw_name or "").strip()
        normalized = normalize_for_match(name)
        if not normalized or normalized not in normalized_text:
            continue
        if normalized in base_by_normalized:
            incoming.append(base_by_normalized[normalized])
        elif normalized.endswith(("으로", "로")) and any(
            normalized == base_name + suffix for base_name in base_by_normalized for suffix in ("으로", "로")
        ):
            # 모델이 변경 조사를 이름에 붙여도 규칙에서 확인한 담당자를 중복 추가하지 않는다.
            continue
        elif plausible_explicit_person_name(name) and re.search(
            rf"(?:담당자|요청자)(?:가|는|를|도)?\s*[:=]?\s*{re.escape(name)}"
            rf"(?=[^가-힣A-Za-z0-9_.-]|$)",
            grounding_text,
            re.IGNORECASE,
        ):
            incoming.append((name, "explicit_unverified"))
    return merge_people(base, build_people(incoming, role))


__all__ = [
    "FILTERABLE_PERSON_ORIGINS",
    "COMMON_KOREAN_SURNAMES",
    "ROLE_FOLLOWING_NOUNS",
    "build_people",
    "drop_people",
    "empty_people",
    "filterable_people",
    "grounded_model_people",
    "merge_people",
    "plausible_explicit_person_name",
    "roster_person_origin",
]
