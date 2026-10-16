"""고객사·플랫폼·담당자·상태·식별자를 추출한다."""

import json
import os
import re
import sqlite3
import tempfile
import threading
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Dict, List, TypeVar

from backend.common import (
    customer_alias_owner,
    customer_project_owner,
    derive_customer_aliases_from_project,
    is_valid_handler_name,
    is_valid_person_name,
    normalize_for_match,
    strip_korean_particle,
    text_contains_person_name,
    text_matches_customer_aliases,
)
from backend.config import (
    CHROMA_DB_PATH,
    CUSTOMER_ALIASES,
    NON_CLOSED_STATUS_PATTERNS,
    PENDING_WORK_QUERY_PATTERN,
    PERSON_ROLE_ALIASES,
    RUNTIME_DIR,
    STATUS_ALIASES,
    STOPWORDS,
)
from backend.intent.constants import (
    CUSTOMER_LIKE_PATTERN,
    GENERIC_CUSTOMER_WORDS,
    SIMPLE_GREETING_TERMS,
)
from backend.intent.dates import remove_date_field_phrases
from backend.intent.people import (
    ROLE_FOLLOWING_NOUNS,
    build_people,
    plausible_explicit_person_name,
    roster_person_origin,
)
from backend.query_filters import add_filter_clause


def extract_mantis_keyword(text: str) -> str:
    cleaned = re.sub(r"[#,:;!?()\[\]{}\"']", " ", text)
    cleaned = re.sub(r"(?:개발\s*)?문의\s*건?\s*포함(?:해서|하여|해)?", " ", cleaned)
    # "3개월"에서 "3개"만 떼어내면 "월"이 홀로 남아 라이브 페이지 스캔의 AND
    # 키워드가 된다. 검색 텍스트의 "월"은 날짜 커스텀 필드가 채워진 이슈에만
    # 있어서, 기간 조건이 "날짜 필드가 입력된 이슈"로 조용히 바뀐다.
    cleaned = re.sub(r"\d+\s*(?:개|건)(?!월)\s*만?", " ", cleaned)
    cleaned = re.sub(r"\d+\s*(?:개|건)(?!월)", " ", cleaned)
    cleaned = re.sub(r"최신\s*\d+\s*건\s*기준(?:으로)?", " ", cleaned)
    tokens = []
    for token in cleaned.split():
        normalized = token.strip()
        if not normalized:
            continue
        if normalized.lower() in STOPWORDS or normalized in STOPWORDS:
            continue
        if normalized.isdigit():
            continue
        tokens.append(normalized)
    # 구조화 조건과 제어어를 제거하기 전에 네 단어로 자르면, 긴 장애 대응
    # 템플릿의 실제 증상이 뒤에 있어 통째로 사라진다. 최종 상한은
    # build_keyword_terms가 정제 후 적용한다.
    return " ".join(tokens)


_FileStamp = tuple[int, int, int, int, int]
_SnapshotKey = tuple[str, _FileStamp, _FileStamp]
_HeaderCatalog = tuple[tuple[str, ...], ...]
_T = TypeVar("_T")
_CATALOG_FIELDS = ("프로젝트(고객사)", "담당자", "요청자")
_CATALOG_MAX_BYTES = 2 * 1024 * 1024
_CATALOG_LOCK = threading.Lock()


class _CatalogSourceChanged(sqlite3.OperationalError):
    """A different source generation became current while reading headers."""


def _file_stamp(path: Path) -> _FileStamp:
    try:
        stat = path.stat()
        return stat.st_dev, stat.st_ino, stat.st_ctime_ns, stat.st_mtime_ns, stat.st_size
    except FileNotFoundError:
        return 0, 0, 0, 0, 0


def _source_cache_key(path: Path) -> _SnapshotKey:
    path = path.resolve()
    return str(path), _file_stamp(path), _file_stamp(Path(str(path) + "-wal"))


def _snapshot_cache_key() -> _SnapshotKey:
    return _source_cache_key(CHROMA_DB_PATH)


def _catalog_path() -> Path:
    return RUNTIME_DIR / "entity-header-catalog-v1.json"


def _read_header_catalog(key: _SnapshotKey) -> _HeaderCatalog | None:
    try:
        with _catalog_path().open("rb") as stream:
            data = stream.read(_CATALOG_MAX_BYTES + 1)
        if len(data) > _CATALOG_MAX_BYTES:
            return None
        payload = json.loads(data)
        if not isinstance(payload, dict) or payload.get("version") != 1:
            return None
        if payload.get("source") != [key[0], list(key[1]), list(key[2])]:
            return None
        fields = payload.get("fields")
        if not isinstance(fields, dict) or set(fields) != set(_CATALOG_FIELDS):
            return None
        if any(not isinstance(values, list) or any(not isinstance(value, str) for value in values)
               for values in fields.values()):
            return None
        return tuple(tuple(fields[field]) for field in _CATALOG_FIELDS)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None


def _write_header_catalog(key: _SnapshotKey, catalog: _HeaderCatalog) -> None:
    data = json.dumps({"version": 1, "source": key,
                       "fields": dict(zip(_CATALOG_FIELDS, catalog, strict=True))},
                      ensure_ascii=False).encode("utf-8")
    if len(data) > _CATALOG_MAX_BYTES:
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=RUNTIME_DIR, prefix=".entity-headers-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _catalog_path())
    except OSError:
        # Read-only runtime directories must not prevent accurate source reads.
        pass
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


@lru_cache(maxsize=1)
def _load_chroma_header_catalog_cached(key: _SnapshotKey) -> _HeaderCatalog:
    path = Path(key[0])
    catalog = _read_header_catalog(key)
    if catalog is None:
        if not path.exists():
            return (), (), ()
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection, connection:
            # One read snapshot keeps the three fields consistent under WAL writers.
            connection.execute("BEGIN")
            catalog = tuple(tuple(_distinct_chroma_header_values(connection, field)) for field in _CATALOG_FIELDS)
        if _source_cache_key(path) != key:
            raise _CatalogSourceChanged("Chroma changed while collecting entity headers")
        _write_header_catalog(key, catalog)
    elif _source_cache_key(path) != key:
        raise _CatalogSourceChanged("Chroma changed while loading entity headers")
    return catalog


def _load_chroma_header_catalog(key: _SnapshotKey) -> _HeaderCatalog:
    # lru_cache alone permits concurrent misses to repeat the expensive scan.
    with _CATALOG_LOCK:
        return _load_chroma_header_catalog_cached(key)


def warm_entity_catalog(database_path: Path | None = None) -> bool:
    """Prime a published source for the next process; report verified disk persistence."""
    source_path = CHROMA_DB_PATH if database_path is None else database_path
    for _ in range(2):
        try:
            key = _source_cache_key(source_path)
            if key[1] == (0, 0, 0, 0, 0):
                return False
            catalog = _load_chroma_header_catalog(key)
            if _source_cache_key(source_path) != key:
                continue
            if _read_header_catalog(key) is None:
                # A prior read may have succeeded while runtime writes were denied.
                _write_header_catalog(key, catalog)
            return _read_header_catalog(key) is not None and _source_cache_key(source_path) == key
        except _CatalogSourceChanged:
            continue
        except (OSError, sqlite3.Error):
            return False
    return False


def _load_current_snapshot(loader: Callable[..., _T], fallback: _T) -> _T:
    for _ in range(2):
        try:
            key = _snapshot_cache_key()
            result = loader(*key)
            if key == _snapshot_cache_key():
                return result
        except _CatalogSourceChanged:
            continue
        except (OSError, sqlite3.Error):
            break
    # Failures are handled outside the LRUs so a transient error is retryable.
    return fallback


@lru_cache(maxsize=1)
def _load_known_people_cached(
    database_path: str,
    database_stamp: _FileStamp,
    wal_stamp: _FileStamp,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    _, handlers, reporters = _load_chroma_header_catalog((database_path, database_stamp, wal_stamp))
    valid_handlers = {
        name
        for name in handlers
        if name not in {"미상", "담당자 미지정"} and is_valid_handler_name(name)
    }
    valid_reporters = {name for name in reporters if name != "미상" and is_valid_person_name(name)}
    return tuple(sorted(valid_handlers)), tuple(sorted(valid_reporters))


def load_known_people() -> tuple[tuple[str, ...], tuple[str, ...]]:
    empty: tuple[tuple[str, ...], tuple[str, ...]] = (tuple(), tuple())
    return _load_current_snapshot(_load_known_people_cached, empty)


def _distinct_chroma_header_values(
    connection: sqlite3.Connection,
    field_name: str,
) -> List[str]:
    """Extract distinct ``- field: value`` headers without loading full docs into Python."""
    marker = f"- {field_name}:"
    rows = connection.execute(
        """
        SELECT DISTINCT trim(
            substr(rest, 1, instr(rest || char(10), char(10)) - 1)
        ) AS value
        FROM (
            SELECT substr(string_value, instr(string_value, ?) + length(?)) AS rest
            FROM embedding_metadata
            WHERE key = 'chroma:document'
              AND instr(string_value, ?) > 0
        )
        """,
        (marker, marker, marker),
    )
    return [str(row[0]).strip() for row in rows if row[0]]


@lru_cache(maxsize=1)
def _load_project_alias_groups_cached(
    database_path: str,
    database_stamp: _FileStamp,
    wal_stamp: _FileStamp,
) -> tuple[tuple[str, ...], ...]:
    projects, _, _ = _load_chroma_header_catalog((database_path, database_stamp, wal_stamp))
    return tuple(tuple(derive_customer_aliases_from_project(project)) for project in set(projects))


def load_project_alias_groups() -> tuple[tuple[str, ...], ...]:
    return _load_current_snapshot(_load_project_alias_groups_cached, tuple())


@lru_cache(maxsize=1)
def _get_customer_alias_groups_cached(
    database_path: str,
    database_stamp: _FileStamp,
    wal_stamp: _FileStamp,
) -> tuple[tuple[str, ...], ...]:
    groups = [
        list(dict.fromkeys([key, *aliases]))
        for key, aliases in CUSTOMER_ALIASES.items()
    ]

    for dynamic_aliases in _load_project_alias_groups_cached(database_path, database_stamp, wal_stamp):
        normalized_dynamic = {normalize_for_match(alias) for alias in dynamic_aliases}
        project_owner = next((
            owner for alias in dynamic_aliases if (owner := customer_project_owner(alias))
        ), None)
        merged = False
        for group in groups:
            if project_owner and group[0] != project_owner:
                continue
            normalized_group = {normalize_for_match(alias) for alias in group}
            if project_owner or normalized_dynamic & normalized_group:
                # 114Solomon(아이엠증권)의 옛 표시명이 현재 하이투자 별칭으로
                # 다시 유입되어 두 프로젝트가 합쳐지지 않게 한다.
                group.extend(
                    alias for alias in dynamic_aliases if alias not in group
                    and (not project_owner or customer_alias_owner(alias) in {None, project_owner})
                )
                merged = True
                break
        if not merged:
            groups.append(list(dynamic_aliases))

    return tuple(tuple(dict.fromkeys(group)) for group in groups)


def get_customer_alias_groups() -> List[List[str]]:
    fallback = tuple(tuple(dict.fromkeys([key, *aliases])) for key, aliases in CUSTOMER_ALIASES.items())
    return [list(group) for group in _load_current_snapshot(_get_customer_alias_groups_cached, fallback)]


def extract_status_constraints(text: str) -> List[str]:
    text = re.sub(r"개발\s*(?:끝났|끝난|끝낸|끝냈)(?:는데|지만|건|거)?", "개발완료 ", text)
    # 미종료 범위에서 "개발완료도 포함"은 해당 상태만 고르라는 조건이
    # 아니라 개발완료가 미종료에 속한다는 설명이다.
    nonclosed_wording = bool(
        PENDING_WORK_QUERY_PATTERN.search(text)
        or any(pattern.search(normalize_for_match(text)) for pattern in NON_CLOSED_STATUS_PATTERNS)
    )
    inclusive_dev_done = bool(
        nonclosed_wording
        and re.search(r"개발\s*완료\s*(?:상태|이슈|건)?도[^.?!]{0,30}?포함", text)
    )
    status_text = re.sub(
        r"확인\s*(?:해\s*(?:줘|주세요|줄래(?:요)?)|부탁(?:해|드려)?|할\s*수\s*있)",
        " ", remove_date_field_phrases(text),
    )
    pending_work = bool(PENDING_WORK_QUERY_PATTERN.search(status_text))
    # 부정된 '해결/완료'를 긍정 상태로 중복 추출하지 않는다.
    status_text = PENDING_WORK_QUERY_PATTERN.sub(" ", status_text)
    normalized_text = normalize_for_match(status_text)
    statuses = []
    for status_key, aliases in STATUS_ALIASES.items():
        alias_text = normalized_text
        if status_key == "closed":
            # 복합 상태명의 '완료'는 종료가 아니다. 별도로 적은 종료는 유지한다.
            alias_text = re.sub(r"개발완료", "", alias_text)
        # 확인·해결·검토는 상태명 외에도 문서 작성 지시에서 매우 자주 쓰인다.
        # 상태 문맥이나 "확인된 이슈"처럼 상태 대상을 함께 말한 경우에만
        # 구조화 조건으로 사용한다.
        if status_key in {"confirmed", "resolved", "review"}:
            explicit_status_patterns = {
                "confirmed": (
                    r"(?<![가-힣])확인(?:된)?\s*상태(?:인|인건|인거|로|의|만)?(?=\s|$|[,.;!?])",
                    r"상태\s*(?:가|는|만|를|=|:)?\s*(?:확인(?:된)?|confirmed)(?:인|으로)?(?=\s|$|[,.;!?])",
                    r"(?:확인된|confirmed)\s*(?:상태|이슈|건|거|것|항목)",
                    r"(?<![A-Za-z0-9_])confirmed(?![A-Za-z0-9_])",
                ),
                "resolved": (
                    r"(?<![가-힣])해결(?:된)?\s*상태(?:인|인건|인거|로|의|만)?(?=\s|$|[,.;!?])",
                    r"상태\s*(?:가|는|만|를|=|:)?\s*(?:해결(?:된)?|resolved)(?:인|으로)?(?=\s|$|[,.;!?])",
                    r"(?:해결된|resolved)\s*(?:상태|이슈|건|거|것|항목)",
                    r"(?<![A-Za-z0-9_])resolved(?![A-Za-z0-9_])",
                ),
                "review": (
                    r"상태\s*(?:가|는|만|를|=|:)?\s*(?:검토|acknowledged)(?:인|으로)?(?=\s|$|[,.;!?])",
                    r"(?:이슈\s*검토|검토\s*(?:상태|이슈|건|항목)|(?<![A-Za-z0-9_])acknowledged(?![A-Za-z0-9_]))",
                ),
            }[status_key]
            if any(re.search(pattern, status_text, re.IGNORECASE) for pattern in explicit_status_patterns):
                statuses.append(status_key)
            continue
        if any(
            bool(re.search(
                r"(?:상태\s*(?:가|는|만|를|=|:)?\s*feedback\b|"
                r"\bfeedback\s*(?:인|상태(?:인)?|로|으로)(?=\s|$|[,.;!?]))",
                status_text, re.IGNORECASE,
            )) if alias == "feedback" else (
                bool(re.search(r"(?<![A-Za-z0-9_])" + re.escape(alias) + r"(?![A-Za-z0-9_])",
                               status_text, re.IGNORECASE))
                if re.fullmatch(r"[A-Za-z_]+", alias) else normalize_for_match(alias) in alias_text
            )
            for alias in aliases
        ):
            statuses.append(status_key)
    if pending_work or any(pattern.search(normalized_text) for pattern in NON_CLOSED_STATUS_PATTERNS):
        statuses = [status for status in statuses if status != "closed"]
        if inclusive_dev_done:
            return ["open"]
        # "종료 안 된 할당 이슈"처럼 더 구체적인 미종료 상태가 함께 있으면
        # 그 상태를 유지하고, 구체 상태가 없을 때만 전체 미종료(open)로 넓힌다.
        if not statuses:
            statuses.append("open")
    return statuses


def extract_issue_ids(text: str) -> List[int]:
    issue_ids = set()
    for pattern in (
        r"(?<![\w])0*(\d{3,7})\s*번\s*(?:이슈|건|티켓)",
        r"이슈\s*#?0*(\d{3,7})\s*번",
        r"(?:이슈\s*)?#\s*(\d{3,})",
        r"(?:이슈\s*ID|id)\s*:?\s*#?\s*(\d{3,})",
        r"^\s*0*(\d{3,7})\s*(?:번\s*)?(?:이슈\s*)?(?:알려\s*줘|알려주세요|보여\s*줘|보여주세요|조회(?:해\s*줘|해주세요)?)\s*[?.!]?\s*$",
        r"\b0*(\d{5,7})\b",
        r"(?<![\w])0*(\d{5,7})(?=과|와|은|는|을|를)",
        r"\b(30\d{3}|29\d{3}|28\d{3}|27\d{3}|26\d{3})\b",
    ):
        for match in re.finditer(pattern, text, re.IGNORECASE):
            if re.search(r"화면(?:\s*번호)?\s*[:#]?\s*$", text[:match.start()]):
                continue
            issue_ids.add(int(match.group(1)))
    return sorted(issue_ids, reverse=True)


def extract_excluded_issue_ids(text: str) -> List[int]:
    """Return issue IDs explicitly followed by an exclusion expression."""
    excluded = set()
    for match in re.finditer(
        r"(?:이슈\s*)?#?\s*(\d{3,})\s*(?:번\s*)?(?:은|는|을|를)?\s*제외",
        text,
        re.IGNORECASE,
    ):
        excluded.add(int(match.group(1)))
    return sorted(excluded, reverse=True)


def extract_screen_numbers(text: str) -> List[str]:
    screen_numbers = set()
    for match in re.finditer(r"\[(\d{3,5})\]", text):
        screen_numbers.add(match.group(1))
    for match in re.finditer(r"(?:화면번호|화면|screen)\s*#?:?\s*(\d{3,5})", text, re.IGNORECASE):
        screen_numbers.add(match.group(1))
    return sorted(screen_numbers)


def infer_person_role(text: str) -> str:
    normalized_text = normalize_for_match(text)
    if any(
        marker in normalized_text
        for marker in (
            "한테할당",
            "에게할당",
            "한테배정",
            "에게배정",
            "할당된",
            "배정된",
        )
    ):
        return "handler"
    for role, aliases in PERSON_ROLE_ALIASES.items():
        if any(normalize_for_match(alias) in normalized_text for alias in aliases):
            return role
    return "any"


def expression_is_excluded(text: str, value: str) -> bool:
    """값 주변의 일반적인 한국어 제외 표현을 판별한다.

    필드마다 ``MTS는 빼고`` 같은 예외 정규식을 복제하지 않도록 고객사,
    플랫폼, 상태, 키워드가 함께 사용하는 최소 공통 판별기다.
    """
    normalized_text = normalize_for_match(text)
    normalized_value = normalize_for_match(value)
    if not normalized_value:
        return False
    particle = r"(?:은|는|을|를|이|가)?"
    bridge = (
        r"(?:(?:상태|조건)(?:인|은|는|가|만|을|를)?"
        r"|(?:된|인)?(?:것|건|것도|건도|것은|건은)?)"
    )
    negative = r"(?:제외|빼고|빼줘|빼주세요|말고|아닌|아니고|없이|제외한|제외하고|미포함)"
    return bool(
        re.search(re.escape(normalized_value) + particle + bridge + negative, normalized_text)
        # 'NH 제외 미종료'에서 제외는 NH에 붙는다. 앞 단어의 제외를 다음
        # 상태/매체로 넘기지 않도록 전치형은 문장 시작에서만 허용한다.
        or re.search(r"^(?:제외|미포함)" + particle + re.escape(normalized_value), normalized_text)
    )


_TITLE_DATE_BASIS_INSTRUCTION = re.compile(
    r'''제목(?:에\s*적힌|에|의)?\s*[^"'“‘”’,.?!]{0,40}?(?:월|날짜|일자)(?:과|와)?\s*'''
    r"관계\s*없이\s*(?:등록|접수|생성)일\s*기준(?:으)?로(?:\s*세(?:고|어|줘|주세요))?"
)


def strip_title_date_basis_instruction(text: str) -> str:
    """제목 속 날짜 대신 등록일로 집계하라는 설명을 검색 조건에서 제거한다."""
    return _TITLE_DATE_BASIS_INSTRUCTION.sub(" ", text)


def strip_conditional_presence_instruction(text: str) -> str:
    """존재 여부 질의의 출력 지시는 제목·키워드 조건이 아니다."""
    text = re.sub(
        r"(?<![가-힣])(?:있으면|있다면)\s*(?:(?:이슈\s*)?번호|제목|목록)"
        r"[^.?!]{0,100}?(?:없으면|없다면)\s*"
        r"(?:(?:해당\s*)?(?:조건|결과)(?:은|가|에)?\s*)?없다고\s*(?:알려|말해|답해)[^.?!]*",
        " ", text,
    )
    return re.sub(
        r"(?<![가-힣])(?:이슈|건|게|거|것)(?:이|가)?\s*"
        r"(?:있는지|있어|있나|있나요)\s*"
        r"(?:확인해\s*(?:줘|주세요)|알려\s*(?:줘|주세요)|말해\s*(?:줘|주세요))?[.?!]?",
        " ", text,
    )


_TITLE_BRACKET_EXCLUSION = re.compile(
    r"제목(?:에|에서|에는)\s*(\[[^\]\n]{1,40}\])(?:가|이)?\s*"
    r"(?:포함된|들어간|있는)\s*(?:것|건|이슈)(?:은|는|을|만)?\s*"
    r"(?:제외해\s*(?:줘|주세요)|제외|빼고)",
)
_TITLE_BRACKET_INCLUSION = re.compile(
    r"제목(?:에|에서|에는)\s*(\[[^\]\n]{1,40}\])(?:가|이)?\s*"
    r"(?:라고\s*적힌|포함된|들어간|있는)\s*(?:것|건|이슈)(?:은|는|을|를|만)?"
    r"(?:\s*(?:골라\s*줘|찾아\s*줘|보여\s*줘))?",
)
_TITLE_BRACKET_INCLUSIVE_SCOPE = re.compile(
    r"제목(?:에|에서|에는)\s*\[[^\]\n]{1,40}\](?:가|이)?\s*"
    r"(?:붙은|적힌|포함된|들어간|있는)\s*(?:것|건|이슈)(?:도|까지)\s*"
    r"포함(?:해서|하여|해)?",
)
_TITLE_BRACKET_INCLUSIVE_PREFIX = re.compile(
    r"\[[^\]\n]{1,40}\]\s*제목(?:도|을|은|는)?\s*"
    r"(?:빼지\s*말고|제외하지\s*말고)\s*포함(?:해\s*(?:줘|주세요)|해서|하여|해)?",
)


def strip_title_bracket_exclusion_instruction(text: str) -> str:
    """제목에서 제외할 표기는 매체 범위나 별도 검색어가 아니다."""
    return _TITLE_BRACKET_EXCLUSION.sub(" ", text)


def strip_title_bracket_platform_instruction(text: str) -> str:
    """제목에 적힌 매체 표기를 실제 플랫폼 범위로 취급하지 않는다."""
    return _TITLE_BRACKET_INCLUSIVE_PREFIX.sub(
        " ", _TITLE_BRACKET_INCLUSIVE_SCOPE.sub(
            " ", _TITLE_BRACKET_INCLUSION.sub(" ", strip_title_bracket_exclusion_instruction(text))
        )
    )


def extract_explicit_keyword_filter_clauses(text: str) -> List[Dict[str, Any]]:
    """제목/본문/키워드 문맥에서 명시한 포함·제외 단어만 구조화한다."""
    text = strip_conditional_presence_instruction(strip_title_date_basis_instruction(text))
    if not re.search(r"제목|본문|내용|키워드", text, re.IGNORECASE):
        return []
    clauses: List[Dict[str, Any]] = []
    for match in re.finditer(r'''(제목|본문|내용|키워드)(?:에|에서|에는|은|는)?\s*["'“‘]([^"'”’]{1,160})["'”’]\s*((?:은|는|이|가)?\s*(?:없(?:는|고)?|제외|빼고|미포함|있(?:는|고)?|포함(?:된)?))?''', text):
        field = "title" if match.group(1) == "제목" else "keyword"
        negative = bool(re.search(r"없|제외|빼고|미포함", match.group(3) or ""))
        add_filter_clause(clauses, field, "not_contains" if negative else "contains", match.group(2))
    for match in re.finditer(
        r"제목(?:에|에서|에는)\s*([가-힣A-Za-z0-9_.-]{2,40}?)(?:이|가)?\s*"
        r"(?:들어간|포함된)\s*(?:건|이슈|것)(?:은|는|만)?\s*(?:빼고|빼면|제외)",
        text,
    ):
        add_filter_clause(clauses, "title", "not_contains", match.group(1))
    for match in re.finditer(
        r"제목(?:이|가|은|는)\s*([가-힣A-Za-z0-9_.-]{2,40}?)인\s*"
        r"(?:건|이슈|것)(?:은|는|을|를)?\s*(?:빼고|제외)", text,
    ):
        add_filter_clause(clauses, "title", "not_contains", match.group(1))
    for match in _TITLE_BRACKET_EXCLUSION.finditer(text):
        add_filter_clause(clauses, "title", "not_contains", match.group(1))
    for match in _TITLE_BRACKET_INCLUSION.finditer(text):
        add_filter_clause(clauses, "title", "contains", match.group(1))
    text = strip_title_bracket_platform_instruction(text)
    token = r"([가-힣A-Za-z0-9_.-]{2,40}?)"
    for match in re.finditer(token + r"(?:은|는|이|가)?\s*(?:없(?:는|고)?|제외|빼고|미포함)", text):
        if match.group(1) in {"건", "건은", "건이", "건을", "건만", "이슈", "이슈는", "이슈가",
                              "것", "것은", "빠짐"}:
            continue
        add_filter_clause(clauses, "keyword", "not_contains", match.group(1))
    for match in re.finditer(token + r"(?:은|는|이|가)?\s*(?:있(?:는|고)?|포함)", text):
        if match.group(1) in {"아니면", "아니고"}:
            continue
        # "정기점검도 포함"은 기존 검색 범위에 더하라는 뜻이지
        # 정기점검만 찾으라는 뜻이 아니다. 별도 제목/키워드 조건은 유지한다.
        if match.group(1).endswith("도") and not re.search(
            r"(?:제목|본문|내용|키워드)(?:에|에서|에는|은|는)\s*$",
            text[:match.start()],
        ):
            continue
        # "정기점검까지 포함한 전체 건수"는 전체 범위에 정기점검을
        # 빼지 말라는 뜻이다. 명시적인 제목/키워드 조건과 구분한다.
        if match.group(1).endswith("까지") and re.match(
            r"\s*(?:한|해서|하여|해)?\s*(?:전체|모든|전부|총)\s*(?:건수|이슈|건|항목)",
            text[match.end():],
        ):
            continue
        if not expression_is_excluded(text, match.group(1)):
            add_filter_clause(clauses, "keyword", "contains", match.group(1))
    return clauses


# "나한테 배정된"에서 "나한테"가 이름으로 잡히면 존재하지 않는 담당자로
# 검색해 결과가 0건이 된다. 화자를 가리키는 말은 이름 후보에서 뺀다.
# (로그인 사용자로 치환하는 일은 상위 계층이 담당한다.)
_FIRST_PERSON_TOKENS = frozenset({
    "나", "내", "저", "제", "본인",
    "나한테", "내게", "나에게", "저한테", "저에게", "본인에게",
    "내가", "제가", "나의", "저의",
})

_EXPLICIT_HANDLER_PATTERN = re.compile(
    r"(?:담당자(?:가|는|를|도)?\s*[:=]?\s*|담당\s*[:=]\s*)"
    r"(?:"
    # 조사가 뒤의 문법 표지와 함께 나타날 때만 이름에서 분리한다. 단순히 '은/이'로
    # 끝나는 실제 이름(예: 이지은)을 조사로 오인하지 않도록 bare-name보다 앞에서
    # 제한된 후속 표현을 요구한다.
    r"(?P<korean_possessive>[가-힣]{2,4}?)의(?=\s|$|[,.?!:;)\]}])"
    r"|(?P<korean_copular>[가-힣]{2,4}?)(?:인|이고|이며|이면서)(?=\s|$|[,.?!:;)\]}])"
    r"|(?P<korean_subject>[가-힣]{2,4}?)(?:이|가|은|는)"
    r"(?=\s+(?:맡은|담당|처리|할당|배정))"
    r"|(?P<directional_name>[가-힣]{2,4}?|[A-Za-z][A-Za-z0-9_.-]{1,31})"
    r"(?:으로|로)(?=\s*(?:바꿔|변경|수정))"
    r"|(?P<korean_name>[가-힣]{2,4})(?=\s|$|[,.?!:;)\]}])"
    r"|(?P<mixed_name>[A-Za-z][A-Za-z0-9_.-]*[가-힣]{1,8}?)(?:의|이|가|은|는)(?=\s|$|[,.?!:;)\]}])"
    r"|(?P<english_name>[A-Za-z][A-Za-z0-9_.-]{1,31})(?=(?:의|이|가|은|는)?(?:\s|$|[,.?!:;)\]}]))"
    r")",
    re.IGNORECASE,
)


def extract_people_constraints(text: str) -> Dict[str, Any]:
    # ``담당자별로`` and similar phrases specify result presentation, not a
    # person named "별로" nor the preceding object (for example ``항목을``).
    # Remove those phrases before applying person-name patterns.
    person_text = re.sub(
        r"(?:담당자|담당)\s*(?:별(?:로)?|기준(?:으)?로|마다)",
        " ",
        text,
    )
    handlers, reporters = load_known_people()
    from backend.team_scope import TEAM_MEMBERS
    known_people = sorted(set(handlers) | set(reporters) | set(TEAM_MEMBERS), key=len, reverse=True)
    pairs: List[tuple[str, str]] = []
    # 존칭이 붙은 이름은 명부 전체에서 유일할 때만 성을 보완한다.
    for match in re.finditer(r"(?<![가-힣])([가-힣]{2,4})\s*님", person_text):
        short = match.group(1)
        candidates = [name for name in known_people if name == short or name[1:] == short]
        if len(candidates) == 1:
            pairs.append((candidates[0], "known"))
        elif len(candidates) > 1:
            pairs.extend((name, "ambiguous_alias") for name in candidates)
    for person in known_people:
        if person and is_valid_handler_name(person) and text_contains_person_name(person_text, person):
            pairs.append((person, roster_person_origin(person)))

    explicit_handler = _EXPLICIT_HANDLER_PATTERN.search(person_text)
    if explicit_handler:
        name = next(
            explicit_handler.group(group)
            for group in (
                "korean_possessive",
                "korean_copular",
                "korean_subject",
                "directional_name",
                "korean_name",
                "mixed_name",
                "english_name",
            )
            if explicit_handler.group(group)
        ).strip()
        if explicit_handler.group("korean_copular"):
            # '김지인'처럼 짧은 실명이나 명부의 전체 이름을 '김지'로 자르지 않는다.
            # 명부 밖의 두 글자 이름 + '인'은 실명과 구별할 수 없어 전체를 보존한다.
            if person_text[explicit_handler.end() - 1] == "인" and (
                name + "인" in known_people or (len(name) == 2 and name not in known_people)
            ):
                name += "인"
        if explicit_handler.group("directional_name"):
            # '로'로 끝나는 실제 명부 이름은 이름 전체를 우선한다.
            name = next((known for known, origin in pairs
                         if origin == "known" and known in {name + "로", name + "으로"}), name)
        korean_candidate = bool(re.fullmatch(r"[가-힣]{2,4}", name))
        mixed_candidate = bool(
            re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*[가-힣][가-힣A-Za-z0-9_.-]*", name)
        )
        if (
            (is_valid_handler_name(name) or mixed_candidate)
            and name not in ROLE_FOLLOWING_NOUNS
            and (not korean_candidate or plausible_explicit_person_name(name))
        ):
            known_origin = next(
                (origin for known_name, origin in pairs if normalize_for_match(known_name) == normalize_for_match(name)),
                None,
            )
            pairs.append((name, "known" if known_origin == "known" else "explicit_unverified"))

    if not build_people(pairs, infer_person_role(person_text))["names"]:
        for match in re.finditer(
            r"([가-힣]{2,4})\s*(?:선임|차장|과장|대리|주임|수석|매니저|팀장|부장|사원|한테|에게|담당|할당|할당된|배정|배정된)",
            person_text,
        ):
            name = match.group(1).strip()
            stem = strip_korean_particle(name)
            if name in _FIRST_PERSON_TOKENS or stem in _FIRST_PERSON_TOKENS:
                continue
            if not plausible_explicit_person_name(name):
                continue
            if not is_valid_person_name(name) or not is_valid_person_name(stem):
                continue
            pairs.append((name, "inferred"))

    role = infer_person_role(person_text)
    if role == "any" and re.search(r"님.*(?:뭐\s*하고|건|업무)", person_text):
        role = "handler"
    return build_people(pairs, role)


def is_simple_greeting(text: str) -> bool:
    normalized = normalize_for_match(re.sub(r"[!?.,~]+", "", str(text or "").strip()))
    return normalized in {normalize_for_match(term) for term in SIMPLE_GREETING_TERMS}


def extract_unknown_customers(text: str) -> List[str]:
    """질문이 가리키는 고객사 중 별칭 목록에 없는 이름을 찾는다.

    미등록 고객사를 조용히 버리면 조건 없는 검색이 되어 전혀 다른 고객사
    이슈가 답으로 나간다. 이름을 따로 남겨 "찾지 못했다"고 답하게 한다."""
    unknown: List[str] = []
    for match in CUSTOMER_LIKE_PATTERN.finditer(str(text or "")):
        name = match.group(0).strip()
        if normalize_for_match(name) in {
            normalize_for_match(word) for word in GENERIC_CUSTOMER_WORDS
        }:
            continue
        if any(
            text_matches_customer_aliases(name, aliases)
            for aliases in get_customer_alias_groups()
        ):
            continue
        if name not in unknown:
            unknown.append(name)
    return unknown


__all__ = [
    "extract_mantis_keyword",
    "load_known_people",
    "load_project_alias_groups",
    "get_customer_alias_groups",
    "extract_status_constraints",
    "extract_issue_ids",
    "extract_excluded_issue_ids",
    "extract_screen_numbers",
    "infer_person_role",
    "expression_is_excluded",
    "extract_explicit_keyword_filter_clauses",
    "extract_people_constraints",
    "is_simple_greeting",
    "extract_unknown_customers",
]
