"""로그인 사용자 개인 범위를 가리키는 질문 표현. 팀·조직 범위와 구분한다."""

import re


PERSONAL_SCOPE_GROUP_MARKERS = ("팀", "팀원", "부서", "조직", "전체", "우리")


PERSONAL_SCOPE_DIRECT_PATTERN = re.compile(
    r"(?:^|[\s,])(?:"
    r"내\s*(?:담당|이슈|업무)|나의\s*이슈|"
    # "내가 지금 처리해야 하는", "제가 맡은"처럼 화자가 주어인 표현.
    r"(?:내가|제가)\s*(?:.{0,12}?)(?:맡은|담당한|처리|해결|대응|해야)|"
    # "나한테 배정된", "저에게 할당된"처럼 화자가 수신자인 표현.
    r"(?:내게|나에게|나한테|저에게|저한테|본인에게)\s*(?:.{0,8}?)(?:할당|배정|맡겨|지정)"
    r")",
    re.IGNORECASE,
)


PERSONAL_SCOPE_SPACED_PATTERN = re.compile(
    r"(?:^|[\s,])(?:내|나의|제|나|저)\s+(?P<qualifiers>.{0,40}?)(?:이슈|업무|건)"
    r"(?:를|을|가|은|는|도|만|의)?(?=$|[\s?!.,])",
    re.IGNORECASE,
)


PERSONAL_SCOPE_COMPACT_PATTERN = re.compile(
    r"(?:^|[\s,])(?:내|나의|제)(?:(?:최근|미종료|장기|열린|진행중|처리중|종료된|완료된))*"
    r"(?:이슈|업무|건)",
    re.IGNORECASE,
)


def has_personal_mantis_scope_phrase(text: str) -> bool:
    """개인 소유 표현과 이슈·업무 대상을 함께 확인한다.

    ``내 최근 이슈``처럼 수식어가 사이에 들어간 표현을 허용하되,
    ``내 팀 이슈``나 ``사내 이슈``를 로그인 사용자 개인 범위로 오인하지 않는다.
    """
    raw_text = str(text or "").strip()
    if PERSONAL_SCOPE_DIRECT_PATTERN.search(raw_text):
        return True
    if PERSONAL_SCOPE_COMPACT_PATTERN.search(raw_text):
        return True
    for match in PERSONAL_SCOPE_SPACED_PATTERN.finditer(raw_text):
        qualifiers = re.sub(r"\s+", "", match.group("qualifiers")).casefold()
        if not any(marker in qualifiers for marker in PERSONAL_SCOPE_GROUP_MARKERS):
            return True
    return False
