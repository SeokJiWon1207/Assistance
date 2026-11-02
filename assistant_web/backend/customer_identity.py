"""고객사 속성의 정규화와 자유 텍스트의 회사명 경계 비교."""
import re
from functools import lru_cache

from typing import List, Optional
from backend.settings.values import AMBIGUOUS_DYNAMIC_CUSTOMER_ALIASES
from backend.utils.text import normalize_for_match
from backend.settings.catalogs import get_customer_aliases


@lru_cache(maxsize=2048)
def customer_alias_owner(alias: str) -> Optional[str]:
    """설정에 명시된 별칭의 고객사를 반환한다. 원본의 옛 표시명보다 우선한다."""
    normalized = normalize_for_match(alias)
    return next((
        key for key, values in get_customer_aliases().items()
        if normalized in {normalize_for_match(value) for value in [key, *values]}
    ), None)


@lru_cache(maxsize=512)
def customer_project_owner(project_name: str) -> Optional[str]:
    """등록된 프로젝트 코드는 괄호 안의 오래된 회사명보다 우선한다."""
    match = re.match(r"^(\d{3}[A-Za-z가-힣][^()\s]*)", project_name.strip())
    if not match:
        return None
    code = re.sub(r"_Project$", "", match.group(1), flags=re.IGNORECASE)
    return customer_alias_owner(code)


def text_matches_customer_aliases(text: str, aliases: List[str]) -> bool:
    """Match customer aliases without short ASCII aliases leaking into words."""
    normalized_text = normalize_for_match(text)
    boundary_text = str(text or "").casefold()
    project_owner = customer_project_owner(text)
    for alias in aliases:
        alias_owner = customer_alias_owner(alias) if project_owner else None
        if alias_owner and alias_owner != project_owner:
            continue
        if alias_owner and alias_owner == project_owner:
            return True
        normalized_alias = normalize_for_match(alias)
        if not normalized_alias:
            continue
        if re.fullmatch(r"[가-힣]{2}", normalized_alias):
            # 짧은 회사명은 기술 용어의 일부(유동부채→동부, 삼성전자→삼성)가 아니다.
            if re.search(rf"(?<![가-힣a-z0-9]){re.escape(normalized_alias)}(?=$|[\s.,:/()]|(?:증권|투자증권|금융투자|선물)(?=$|[\s.,:/()])|(?:에서|의|은|는|이|가|만|도|을|를|와|과|랑|이랑|하고|에게|한테|으로|로)(?:\s|$))", boundary_text):
                return True
            continue
        if re.fullmatch(r"[a-z0-9]+", normalized_alias) and len(normalized_alias) <= 3:
            if re.search(
                rf"(?<![a-z]){re.escape(normalized_alias)}(?![a-z])",
                boundary_text,
            ):
                return True
            continue
        if normalized_alias in normalized_text:
            return True
    return False


def add_customer_alias(aliases: set[str], value: str) -> None:
    normalized = value.strip()
    if not normalized:
        return
    aliases.add(normalized)
    for part in re.split(r"[,/·]", normalized) if "(" not in normalized and ")" not in normalized else []:
        part = part.strip()
        if part and part != normalized:
            add_customer_alias(aliases, part)
    without_project_suffix = re.sub(r"_Project$", "", normalized)
    aliases.add(without_project_suffix)
    without_number = re.sub(r"^\d+", "", without_project_suffix)
    if without_number:
        aliases.add(without_number)

    parenthetical_parts = re.findall(r"\(([^)]+)\)", without_project_suffix)
    for part in parenthetical_parts:
        add_customer_alias(aliases, part)

    outside_parentheses = re.sub(r"\([^)]*\)", "", without_project_suffix).strip()
    if outside_parentheses:
        aliases.add(outside_parentheses)
        aliases.add(re.sub(r"^\d+", "", outside_parentheses))

    for suffix in ("투자증권", "증권", "선물", "금융투자"):
        if normalized.endswith(suffix) and len(normalized) > len(suffix) + 1:
            aliases.add(normalized[: -len(suffix)])


def derive_customer_aliases_from_project(project_name: str) -> List[str]:
    aliases: set[str] = set()
    add_customer_alias(aliases, project_name)
    blocked = {normalize_for_match(alias) for alias in AMBIGUOUS_DYNAMIC_CUSTOMER_ALIASES}
    return sorted(
        alias
        for alias in aliases
        if alias and normalize_for_match(alias) not in blocked
    )


def customer_forms(key):
    # 등록된 별칭만 사용한다. '증권/선물' 접미사를 합성하면 별도 회사가 합쳐진다.
    return list(dict.fromkeys([key, *get_customer_aliases().get(key, [])]))


def canonical_customer(value):
    value = str(value or '')
    owner = customer_project_owner(value)
    if owner:
        return owner
    normalized = normalize_for_match(value)
    for key in get_customer_aliases():
        if normalized in {normalize_for_match(form) for form in customer_forms(key)}:
            return key
    return normalized


@lru_cache(maxsize=512)
def _mention_pattern(form):
    return re.compile(
        r'(?<![가-힣A-Za-z0-9_])' + re.escape(form).replace(r'\ ', r'\s*')
        + r'(?=$|[\s.,:/()\[\]{}<>"\'“”‘’!?;·]|'
          r'(?:에서|에게|한테|으로|까지|부터|하고|이랑|랑|의|은|는|이|가|만|도|을|를|와|과|로)'
          r'(?=$|[\s.,:/()\[\]!?;]))', re.I)


def customer_mentions(text):
    """등록된 별칭의 위치를 반환한다. 긴 회사명 안의 짧은 회사명은 제외한다."""
    matches = []
    for key in get_customer_aliases():
        for form in customer_forms(key):
            for match in _mention_pattern(form).finditer(str(text or '')):
                matches.append((match.start(), match.end(), key, match.group()))
    selected: list[tuple[int, int, str, str]] = []
    for item in sorted(matches, key=lambda item: (item[0], -(item[1]-item[0]))):
        if not any(item[0] < other[1] and other[0] < item[1] for other in selected):
            selected.append(item)
    return selected


def customer_value_matches(actual, requested, *, text_fallback=False):
    expected = {canonical_customer(value) for value in requested}
    if text_fallback:
        found = {item[2] for item in customer_mentions(actual)}
        # 등록되지 않은 정확 프로젝트 값도 문서에서 경계를 지켜 찾는다.
        found.update(canonical_customer(value) for value in requested
                     if canonical_customer(value) not in get_customer_aliases()
                     and _mention_pattern(str(value)).search(str(actual or '')))
    else:
        values = actual if isinstance(actual, (list, tuple)) else re.split(r'[,;\n/·]+', str(actual or ''))
        found = {canonical_customer(value.strip()) for value in values if str(value).strip()}
    return bool(found & expected)
