"""DB 소유자별 기존 연결 옵션. 읽기 URI의 mode는 호출자가 보존한다.

값이 같아도 DB별 이름을 두어 향후 한 DB의 변경이 다른 DB로 번지지 않게 한다.
BEGIN/commit/rollback과 user_version·schema 정책은 프로필에 포함하지 않는다.
"""
import sqlite3

from backend.storage.sqlite import ConnectionProfile

AUTH = ConnectionProfile()
AUTH_ROWS = ConnectionProfile(row_factory=sqlite3.Row)
AUDIT = ConnectionProfile()
AUDIT_ROWS = ConnectionProfile(row_factory=sqlite3.Row)
WORKSPACE = ConnectionProfile()
WORKSPACE_ROWS = ConnectionProfile(row_factory=sqlite3.Row)
MIGRATIONS = ConnectionProfile(pragmas=('PRAGMA busy_timeout = 5000',))
NOTION_ACTIONS = ConnectionProfile(timeout=5)
# 초기화의 WAL/FULL은 기존 chmod 이후 순서를 지키기 위해 schema 소유자에 남긴다.
NOTION_BODY_INIT = ConnectionProfile(timeout=30)
NOTION_BODY = ConnectionProfile(timeout=30, uri=True, row_factory=sqlite3.Row,
                                pragmas=('PRAGMA foreign_keys=ON', 'PRAGMA synchronous=FULL'))
MANTIS_STORE_READ = ConnectionProfile(uri=True)
MANTIS_STORE_STREAM = ConnectionProfile(uri=True, check_same_thread=False)
MANTIS_STORE_BUILD = ConnectionProfile()
MANTIS_INDEX_READ = ConnectionProfile(uri=True)
MANTIS_INDEX_BUILD = ConnectionProfile(pragmas=(
    'PRAGMA foreign_keys = ON', 'PRAGMA journal_mode = OFF', 'PRAGMA synchronous = OFF',
))
MANTIS_CHUNK_READ = ConnectionProfile(uri=True, row_factory=sqlite3.Row)
MANTIS_CHUNK_BUILD = ConnectionProfile()
# 외부 Chroma 소유 DB의 기존 일반 연결과 명시적 URI 읽기를 구분한다.
CHROMA = ConnectionProfile()
CHROMA_READ = ConnectionProfile(uri=True)
CHROMA_TITLE_READ = ConnectionProfile(uri=True)
CHROMA_TITLE_BUILD = ConnectionProfile()
HEALTH_READ = ConnectionProfile(uri=True)
BACKUP_READ = ConnectionProfile(uri=True)
BACKUP_WRITE = ConnectionProfile()
