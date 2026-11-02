"""이름 후보, 담당자 식별자와 한국어 조사를 판정한다."""
import re
from typing import List
from backend.settings.catalogs import get_customer_aliases
from backend.settings.values import PERSON_BLOCKLIST, PERSON_ROLE_ALIASES, PLATFORM_ALIASES, STATUS_ALIASES, STOPWORDS
from backend.utils.text import normalize_for_match


def extract_from_names(text: str) -> List[str]:
    names = set()
    for match in re.finditer(r"From\.\s*([가-힣A-Za-z]{2,6})", text, re.IGNORECASE):
        name = match.group(1).strip()
        if is_valid_person_name(name):
            names.add(name)
    for match in re.finditer(r"([가-힣]{2,4})\s*(?:선임|차장|과장|대리|주임|수석|매니저|팀장|부장|사원)", text):
        name = match.group(1).strip()
        if is_valid_person_name(name):
            names.add(name)
    return sorted(names)


def is_valid_person_name(name: str) -> bool:
    normalized = name.strip()
    if not re.fullmatch(r"[가-힣]{2,4}|[A-Za-z]{2,20}", normalized):
        return False
    lowered = normalized.lower()
    if normalized in STOPWORDS or lowered in STOPWORDS or normalized in PERSON_BLOCKLIST:
        return False
    for aliases in ([key, *values] for key, values in get_customer_aliases().items()):
        if any(normalize_for_match(normalized) == normalize_for_match(alias) for alias in aliases):
            return False
    for aliases in PLATFORM_ALIASES.values():
        if any(normalize_for_match(normalized) == normalize_for_match(alias) for alias in aliases):
            return False
    for aliases in STATUS_ALIASES.values():
        if any(normalize_for_match(normalized) == normalize_for_match(alias) for alias in aliases):
            return False
    for aliases in PERSON_ROLE_ALIASES.values():
        if any(normalize_for_match(normalized) == normalize_for_match(alias) for alias in aliases):
            return False
    return True


def is_valid_handler_name(name: str) -> bool:
    normalized = name.strip()
    if not re.fullmatch(r"[가-힣]{2,4}|[A-Za-z][A-Za-z0-9_.-]{1,31}", normalized):
        return False
    return normalized not in STOPWORDS and normalized not in PERSON_BLOCKLIST


# known_people(과거 이슈에서 추출한 실제 담당자/보고자 이름) 캐시엔 "파트너매니저"
# 같은 화면/시스템 이름이 "파트너"+"매니저(직함)"로 잘못 쪼개져 섞여 들어갈 수 있다.
# 단순 substring 포함 매칭(normalize_for_match 방식)을 쓰면 "파트너사업팀"처럼
# 이름이 더 큰 복합어의 일부로 들어있을 때도 인물로 오인한다. 뒤에 한글 조사나
# 비한글 문자/문자열 끝이 올 때만 "독립된 단어"로 인정해 이런 오탐을 막는다.
KOREAN_PARTICLE_PATTERN = "가|이|는|은|을|를|와|과|도|만|의|에게|한테|랑|이랑|께|에서|으로|로"


def strip_korean_particle(name: str) -> str:
    """조사가 붙어 있으면 떼어낸 어간을, 없으면 원래 값을 돌려준다.

    "이슈의 담당자", "제목과 담당자로"처럼 일반 명사에 조사가 붙은 말이
    이름 후보로 잡혔는지 판별할 때 쓴다.
    """
    normalized = str(name or "").strip()
    match = re.fullmatch(rf"([가-힣]{{2,}})(?:{KOREAN_PARTICLE_PATTERN})", normalized)
    return match.group(1) if match else normalized


def text_contains_person_name(text: str, name: str) -> bool:
    pattern = re.compile(
        rf"{re.escape(name)}(?:{KOREAN_PARTICLE_PATTERN})?(?=[^가-힣]|$)",
        re.IGNORECASE,
    )
    return bool(pattern.search(text))
