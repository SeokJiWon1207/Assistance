"""Mantis 스냅샷에서 담당자·요청자·프로젝트 명단을 같은 세대로 읽는다."""
import json
import os
import sqlite3 as sqlite3
import tempfile
import threading
from contextlib import closing
from functools import lru_cache
from pathlib import Path
from typing import Callable, List, TypeVar
from backend.settings.paths import CHROMA_DB_PATH, RUNTIME_DIR
from backend.settings.catalogs import get_customer_aliases
from backend.customer_identity import customer_alias_owner, customer_project_owner, derive_customer_aliases_from_project
from backend.domain.people import is_valid_handler_name, is_valid_person_name
from backend.utils.text import normalize_for_match

from backend.storage.sqlite import open_sqlite
from backend.storage.profiles import CHROMA_READ


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
        with closing(open_sqlite(path.as_uri() + "?mode=ro", profile=CHROMA_READ)) as connection, connection:
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
        for key, aliases in get_customer_aliases().items()
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
    fallback = tuple(tuple(dict.fromkeys([key, *aliases])) for key, aliases in get_customer_aliases().items())
    return [list(group) for group in _load_current_snapshot(_get_customer_alias_groups_cached, fallback)]
