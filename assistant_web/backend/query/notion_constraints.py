"""명단을 입력으로 받는 Notion 제목·담당자·상태·기한 해석 규칙."""
import re
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple
from backend.domain.people import is_valid_person_name, strip_korean_particle
from backend.utils.text import normalize_for_match
from backend.notion.constants import NOTION_STATUS_QUERY_TERMS, NOTION_STATUS_TYPOS
from backend.query.operations import extract_group_by
from backend.query.dates import extract_created_at_range, extract_yearless_temporal_month_range
from backend.query.notion_text import notion_literal_title, notion_structural_query, notion_registration_sort
from backend.query_filters import notion_temporal_field


# 의존 명사와 조사는 붙여 쓰기도 하지만, 다음 일반 명사의 일부까지
# 소비해서는 안 된다. 이 표현 뒤에는 반드시 어절 경계를 확인한다.
_STATUS_BOUND_NOUN = r"(?:것|거|건)(?:들)?(?:만|은|는|이|가|을|를|도)?"


_STATUS_SPOKEN_SUFFIX = rf"(?:(?:인|된)\s*)?{_STATUS_BOUND_NOUN}"


_STATUS_WORD_END = r"(?:\s|$|[,.;!?])"


_OWNER_PREDICATE = r"(?:맡|담당|처리)[가-힣]*"


_OWNER_SUFFIXES = ("이면서", "으로", "이고", "이며", "인", "이", "가", "은", "는", "의", "로")


def trim_owner_adnominal(candidate: str, following_text: str = "", *, known_people: tuple[tuple[str, ...], tuple[str, ...]]) -> str:
    """이름 자체를 우선 보존하고 확인 가능한 조사만 정리한다."""
    name = str(candidate or "").strip()
    handlers, reporters = known_people
    known = set(handlers) | set(reporters)
    if name in known or len(name) <= 3:
        return name
    for suffix in _OWNER_SUFFIXES:
        if name.endswith(suffix) and len(name) > 2 and name[: -len(suffix)] in known:
            return name[: -len(suffix)]
    # 명단에 없는 4글자 이름 자체를 조사로 줄이지 않는다. 긴 연결
    # 어미나 원래 이름이 될 수 없는 길이는 분리하고, 짧은 조사는
    # 뒤 문법까지 확인한다 ('김지은이 맡은', '김지은의 문서').
    for suffix in _OWNER_SUFFIXES:
        if name.endswith(suffix) and 3 <= len(name) - len(suffix) <= 4:
            grammatical_suffix = (
                suffix in {"이", "가", "은", "는"}
                and re.match(rf"\s+{_OWNER_PREDICATE}", following_text)
            ) or (
                suffix == "의" and re.match(r"\s+(?:문서|항목|업무|이슈)", following_text)
            )
            if len(suffix) > 1 or len(name) > 4 or grammatical_suffix:
                return name[: -len(suffix)]
    return name


_IMPLICIT_OWNER_NAME = re.compile(r"[가-힣]{2,4}|[A-Z][a-z]{1,19}")


@lru_cache(maxsize=8)
def _known_notion_owner_matcher(
    handlers: tuple[str, ...], reporters: tuple[str, ...],
) -> tuple[Optional[re.Pattern[str]], Dict[str, Tuple[int, str]]]:
    """스냅샷에서 읽은 명단 내용별로 검증·정렬·정규식 생성을 한 번만 한다."""
    names = sorted(
        (
            name for name in set(handlers) | set(reporters)
            if _IMPLICIT_OWNER_NAME.fullmatch(name) and is_valid_person_name(name)
        ),
        key=lambda name: (-len(name), name),
    )
    if not names:
        return None, {}
    pattern = re.compile(
        r"(?<![가-힣A-Za-z])(?P<owner>" + "|".join(re.escape(name) for name in names) + r")(?:님)?"
        rf"(?:(?:이|가|은|는)?\s+(?:{_OWNER_PREDICATE}|소유|작성|등록|할당)"
        r"|의\s+(?:문서|항목|업무|이슈))",
        re.IGNORECASE,
    )
    return pattern, {name.casefold(): (rank, name) for rank, name in enumerate(names)}


def known_notion_owner(text: str, *, known_people: tuple[tuple[str, ...], tuple[str, ...]]) -> str:
    """담당자 표지가 없는 표현은 실제 명단에 있는 이름만 인정한다."""
    # 호출자가 전달한 명단 내용이 바뀌면 새 matcher를 만들고, 같으면 재사용한다.
    handlers, reporters = known_people
    pattern, names = _known_notion_owner_matcher(tuple(handlers), tuple(reporters))
    if pattern is None:
        return ""
    # 기존의 긴 이름 우선 선택을 유지하면서 텍스트는 합친 패턴으로 검색한다.
    match = min(
        pattern.finditer(text), key=lambda item: names[item.group("owner").casefold()][0], default=None,
    )
    return names[match.group("owner").casefold()][1] if match else ""


def extract_notion_database_label_from_text(text: str) -> Optional[str]:
    normalized_text = normalize_for_match(text)
    if "이슈리스트" in normalized_text:
        return "이슈리스트"
    if "주요이슈" in normalized_text or "주요이슈현황" in normalized_text:
        return "주요이슈"
    # 라벨은 질문의 표현과 DB 제목 양쪽에 공통으로 들어가는 부분이어야
    # 검색어 목록에서 빠지고 DB 범위도 좁혀진다. ('조직도' → 'DB00_조직')
    if "조직도" in normalized_text:
        return "조직"
    # UI와 대화에서는 DB14_문서양식을 보통 "템플릿" 또는 "양식"이라고 부른다.
    # 목록 탐색일 때만 DB 별칭으로 취급해 구체적인 문서 키워드 검색은 넓게 유지한다.
    if (
        any(alias in normalized_text for alias in ("템플릿", "양식"))
        and any(marker in normalized_text for marker in ("목록", "리스트", "전체", "보여", "어떤"))
    ):
        return "문서양식"
    # 질문에 쓰는 표현이 DB 제목의 일부이면 그대로 라벨로 쓴다.
    for label in (
        "DB02_통합_프로젝트", "DB03_작업", "버전관리", "기술공유",
        "회의록", "사내규정", "휴가신청", "정기보고서", "정기점검",
        "문서양식", "주간업무", "팀세미나",
    ):
        if normalize_for_match(label) in normalized_text:
            return label
    return None


def parse_notion_constraints(text: str, *, known_people: tuple[tuple[str, ...], tuple[str, ...]]) -> Dict[str, Any]:
    exact_title = notion_literal_title(text)
    constraint_text = notion_structural_query(text)
    # 인용된 제목은 사용자가 지정한 정확한 문자열이므로 건드리지 않는다.
    # DB 옵션은 고유한 값이다. '결제완료'가 실제 옵션인 DB도 있으므로
    # 스키마를 보지 않은 상태에서 '결재완료'로 고쳐 쓰지 않는다.
    status_typo = None
    status = ""
    excluded_statuses: List[str] = []
    # 기존 상태명은 다양한 활용형(완료건수/완료처리된/시작전이었던)을
    # 계속 인식한다. 합성어와 모호한 '대기'에만 엄격한 뒤 경계를 둔다.
    remaining_status_text = constraint_text
    for candidate in sorted(NOTION_STATUS_QUERY_TERMS | NOTION_STATUS_TYPOS.keys(), key=len, reverse=True):
        spelling = r"\s*".join(re.escape(char) for char in candidate)
        boundary = (
            rf"(?=(?:{_STATUS_SPOKEN_SUFFIX}|상태|인|으로|로|만|은|는|이|가|을|를|된)?{_STATUS_WORD_END})"
            if candidate == "대기" else ""
        )
        pattern = re.compile(r"(?<![가-힣A-Za-z0-9_])" + spelling + boundary, re.IGNORECASE)
        for match in pattern.finditer(remaining_status_text):
            if re.match(
                rf"\s*(?:{_STATUS_SPOKEN_SUFFIX}|상태|항목|문서|이슈)?"
                r"\s*(?:은|는|이|가)?\s*(?:아닌|말고|제외|빼고)",
                remaining_status_text[match.end():],
            ):
                excluded_statuses.append(candidate)
            elif not status:
                status = candidate
        remaining_status_text = pattern.sub(" ", remaining_status_text)

    # DB별 사용자 정의 상태도 명시적인 'X 상태는 빼고'에서 추출한다.
    # 제목은 이미 제거했으므로 제목 속 단어를 필터로 해석하지 않는다.
    for match in re.finditer(
        r"(?<![가-힣A-Za-z0-9_])([가-힣A-Za-z0-9_()-]+)\s+상태"
        r"\s*(?:은|는|이|가|을|를)?\s*(?:아닌|말고|제외|빼고)",
        constraint_text,
    ):
        value = match.group(1)
        if value not in excluded_statuses:
            excluded_statuses.append(value)
        if status == value:
            status = ""

    # 사용자 정의 포함 상태는 명시적인 '상태가 X인' 표현만 받는다.
    # '상태가 변경된 항목'의 형용사를 옵션 이름으로 만들지 않는다.
    explicit_status = re.search(
        r"상태\s*(?:가|는|=|:)\s*([가-힣A-Za-z0-9_()-]+?)"
        rf"(?:인(?=(?:\s*{_STATUS_BOUND_NOUN})?{_STATUS_WORD_END})|(?:으로|로)(?={_STATUS_WORD_END}))",
        constraint_text,
    )
    if explicit_status:
        value = explicit_status.group(1).strip()
        if value and value not in excluded_statuses and not any(
            word in explicit_status.group(0) for word in ("말고", "제외", "빼고", "아닌")
        ):
            status = value

    group_by = extract_group_by(constraint_text)
    owner_text = re.sub(
        r"(?:담당자|담당)\s*(?:별(?:로)?|기준(?:으)?로|마다)",
        " ",
        constraint_text,
    )
    owner = known_notion_owner(owner_text, known_people=known_people)
    # "담당자 미지정 항목만"은 미지정이라는 이름을 찾는 게 아니라 담당자가
    # 비어 있는 항목을 찾는 요청이다.
    unassigned_owner = bool(
        re.search(
            r"담당자?\s*(?:가|는|를|도)?\s*"
            r"(?:없는|없이|없음|미지정|미배정|안\s*정해진|지정\s*안\s*된|할당\s*안\s*된|배정\s*안\s*된)",
            constraint_text,
        )
    )
    owner_match = re.search(
        # 조사 "가"를 함께 소비하지 않으면 "담당자가"가 담당+자가로 쪼개져
        # "자가"가 이름으로 잡힌다.
        r"(?:담당자|담당|owner|기여자|공헌자|공동\s*작업자|contributors?)\s*(?:가|는|은|를|을|만|=|:)?\s*"
        r"([가-힣]{2,8}|[A-Za-z]{2,20})(?=\s|$|[,.;!?])",
        owner_text,
        re.IGNORECASE,
    )
    if owner_match and not unassigned_owner and not owner:
        candidate = trim_owner_adnominal(owner_match.group(1), owner_text[owner_match.end(1):], known_people=known_people)
        # 조사가 붙은 일반 명사가 이름으로 잡히지 않도록 어간까지 검증한다.
        if candidate not in {"문서", "항목", "업무", "자료", "페이지", "이슈", "모두"} and is_valid_person_name(candidate) and is_valid_person_name(
            strip_korean_particle(candidate)
        ):
            owner = candidate
    if not owner and not unassigned_owner:
        owner = known_notion_owner(owner_text, known_people=known_people)
    if unassigned_owner:
        owner = ""

    result = {
        "database_label": extract_notion_database_label_from_text(constraint_text) or "",
        "exact_title": exact_title,
        "status": status,
        "excluded_statuses": excluded_statuses,
        "owner": owner,
        "unassigned_owner": unassigned_owner,
        "status_typo": status_typo,
        "native_status_constraint": bool(status or excluded_statuses),
        "group_by": group_by,
    }
    temporal_field = notion_temporal_field(constraint_text)
    date_range = (
        extract_created_at_range(constraint_text)
        or extract_yearless_temporal_month_range(constraint_text, temporal_field)
        if temporal_field != "created_at" else None
    )
    if date_range:
        result["date_filter"] = {"field": temporal_field, "operator": "between", "value": [date_range["from"], date_range["to"]]}
    registration_sort = notion_registration_sort(text)
    if registration_sort:
        result["sort"] = registration_sort
    return result
