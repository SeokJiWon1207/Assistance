# SQLite 소유권과 연결·트랜잭션 정책

P6 기준 지도다. `runtime`은 `ASSISTANT_RUNTIME_DIR` 또는 기존 기본 경로를 뜻한다. 이 작업은 파일 경로·마이그레이션 번호·DDL·행 내용·보관 정책을 변경하지 않는다.

## DB별 소유자

| DB / 기존 경로 | 스키마·버전 소유자 | 읽기·쓰기 소비자 | 연결·잠금·게시 정책 |
|---|---|---|---|
| users: `AUTH_USERS_DB_PATH` / runtime/users.sqlite3 | `auth.py`, 공용 runner의 `user_version=3` | auth 사용자 조회·초대·권한·비밀번호 상태 | `AUTH`, `AUTH_ROWS`; 5초, 조회만 Row. 기존 경로별 초기화 캐시, with connection commit/rollback. migration 후 0600 |
| audit: `AUDIT_DB_PATH` / runtime/assistant_audit.sqlite3 | `audit.py`, 공용 runner의 `user_version=3` | audit 조회/보관, evaluation 최종화·실행, quality API | `AUDIT`, `AUDIT_ROWS`; 5초, 필요한 조회만 Row. 기존 보관·사용자 범위와 호출자 트랜잭션. migration 후 0600 |
| workspace: `WORKSPACE_DB_PATH` / runtime/workspace.sqlite3 | `storage/workspace_schema.py`, 공용 runner의 `user_version=5` | conversation_history, workspace 알림 읽음, workspace_notifications 이벤트, playbooks, workspace_repository 브리핑 | `WORKSPACE`, `WORKSPACE_ROWS`; 5초. 대화 저장·일괄 삭제·관찰은 기존 BEGIN IMMEDIATE, 목록 스냅샷은 기존 BEGIN. migration 후 0600 |
| runtime/notion_actions.sqlite3 | `notion/actions.py`, CREATE IF NOT EXISTS; 공용 runner 밖 | 작업 저장·확인·취소·완료 결과 재사용 | `NOTION_ACTIONS`; 5초. 기존 schema commit·0600. 확인은 BEGIN IMMEDIATE, 외부 쓰기는 기존처럼 DB 트랜잭션 밖, 완료 저장은 별도 트랜잭션 |
| runtime/notion_body_index.sqlite3 | `notion/body_index.py`; metadata schema_version="1", content policy=2 | 본문 수집/수정과 제목·본문 후보 조회 | `NOTION_BODY_INIT`, `NOTION_BODY`; 30초. 초기화 시 chmod 뒤 WAL/FULL 순서 유지. 일반 연결은 기존 rw/ro URI, Row·foreign_keys ON·FULL. 읽기 BEGIN, 쓰기 BEGIN IMMEDIATE |
| runtime/mantis_complete.sqlite3 | `sources/mantis_store.py`와 `mantis_search_projection.py`; metadata/검색 projection=2 | 저장본 갱신·검색·대시보드·알림 노트 식별자 조회 | `MANTIS_STORE_READ/STREAM/BUILD`; 5초. 읽기 ro URI·기존 BEGIN. 스트림만 check_same_thread=False. 쓰기 파일 잠금·임시 파일·원자적 교체·세대/연결 검증 유지 |
| `MANTIS_INDEX_DB_PATH` / Chroma 옆 mantis_index.sqlite3 | `sources/mantis_index.py`; metadata schema_version="2" | 저장본 필터 조회, 추세 조회, 구축·검증 스크립트 | `MANTIS_INDEX_READ/BUILD`; 5초. 조회 ro URI. 임시 구축에서만 foreign_keys ON, journal_mode OFF, synchronous OFF. 기존 교체·원본 서명 검증 |
| runtime/mantis_chunks.sqlite3 또는 호출자가 지정한 target | `sources/mantis_chunk_index.py`; metadata version=2 | hybrid 검색, chunk 구축 | `MANTIS_CHUNK_READ/BUILD`; 5초. 읽기 ro URI·Row·BEGIN. 기존 파일 잠금·임시 구축·세대 확인·원자적 교체 |
| runtime/chroma_titles.sqlite3 또는 호출자가 지정한 target | `sources/chroma_titles.py`; metadata version=1 | lexical/title 검색, projection 구축 | `CHROMA_TITLE_READ/BUILD`; 5초. 읽기 ro URI, 기존 source signature 검증·임시 구축·원자적 교체 |
| `CHROMA_DB_PATH` / data/mantis_chroma_db/chroma.sqlite3 | 외부 Chroma 라이브러리 | chroma, mantis 원문/신선도, people catalog, snapshot reader, 인덱스/벡터 구축 스크립트 | `CHROMA`는 기존 일반 연결 유지, 명시적 ro 조회만 `CHROMA_READ`. 앱 runner에 편입하지 않고 외부 스키마·버전에 관여하지 않음 |

`api/operations.py`의 상태 점검은 `HEALTH_READ`, 배포·정확도 점검의 복제본은 `BACKUP_READ/WRITE`를 사용한다. SQLite backup·integrity_check·0600·원자적 파일 교체와 검사 대상 DB 선택은 기존 스크립트에 남아 있다. `scripts/runtime_databases.py`는 직접 실행할 때도 앱 경로를 설정해 표준 라이브러리만 사용하는 연결 모듈을 불러온다.

## 연결 생성과 트랜잭션의 경계

`storage/sqlite.py`의 `open_sqlite(path, *, profile)`만 `sqlite3.connect`를 호출한다. `storage/profiles.py`는 DB별 기존 설정을 선언한다. 값이 같아도 DB별 프로필 이름을 분리했다.

- 기본값은 timeout=5초, uri=False, row_factory=None, isolation_level="", autocommit=LEGACY_TRANSACTION_CONTROL, check_same_thread=True다. 현재 Python 3.12 동작을 명시한다.
- helper는 연결과 해당 연결의 PRAGMA만 설정한다. BEGIN·commit·rollback·schema 생성·디렉터리 생성·chmod를 자동 실행하지 않는다.
- `closing(connection)`은 close, `with connection`은 commit/rollback, 명시적 BEGIN/BEGIN IMMEDIATE는 기존 호출자가 담당한다. with connection만으로 연결이 닫힌다고 가정하지 않는다.
- 프로필 적용이 실패하면 아직 반환되지 않은 연결을 닫고 원래 예외를 다시 던진다. 정상 반환된 연결의 수명은 호출자에게 있다.
- URI 문자열과 mode는 호출자가 그대로 전달한다. 기존 일반 Chroma 연결을 일괄 read-only로 바꾸거나, 모든 DB에 WAL·긴 timeout을 적용하지 않는다.
- 기존 body index 초기화의 WAL/FULL은 chmod 이후 실행 순서를 보존하기 위해 schema 초기화 함수에 남아 있다. 일반 body 연결의 foreign_keys/FULL, Mantis index 구축 PRAGMA, migration busy_timeout만 기존 값 그대로 프로필로 이동했다.
- 현재 mypy의 SQLite stub이 Python 3.12의 빈 isolation_level과 -1 autocommit 상수를 누락해 해당 인수 두 곳에만 arg-type 예외를 둔다. 실제 연결 속성은 계약 테스트로 검증한다.

## 스키마와 마이그레이션

공용 `run_sqlite_migrations`는 users/audit/workspace만 관리한다. migration별 기존 BEGIN IMMEDIATE 안에서 버전을 다시 확인하고 해당 한 버전의 DDL·DML·user_version을 함께 commit한다. 실패한 버전은 rollback하고, 이미 완료된 앞 버전은 유지한다. 지원 버전보다 새 DB는 기존처럼 거절한다.

workspace의 기존 순서는 다음과 같다.

1. conversations·notification_reads
2. weekly_team_briefs·weekly_team_brief_reads
3. resolution_playbooks
4. conversation_messages와 기존 JSON 메시지 이전
5. workspace_observations·workspace_events

메시지 검증·행 직렬화는 `storage/conversation_rows.py`, 이슈 요약은 `storage/workspace_rows.py`가 소유한다. schema가 대화 서비스나 알림 조회를 역참조하지 않는다. 브리핑 생성·갱신 판단은 reports에, 첫 작성자 보존/교체·사용자별 읽음 SQL은 `workspace_repository.py`에 둔다.

다른 캐시·색인의 CREATE IF NOT EXISTS, metadata 버전, 지연 테이블 확장, 기존 재구축 정책을 유지한다. 이들을 공용 runner에 자동 등록하거나 DB 파일을 재생성하지 않는다. 코드 롤백을 위해 DB 삭제나 migration 되감기를 수행하지 않는다.

## 지속 검증

`test_storage_contracts.py`가 연결 생성 위치, storage의 의존 방향, 전체 Python SCC 0, 기존 함수 경로·객체 동일성을 검사한다. 빈/구버전 DB 14개 사례의 SQL·행·버전·권한은 `fixtures/storage_migrations_golden.json`과 비교한다. 운영 DB를 테스트 대상으로 사용하지 않는다.

실제 임시 DB로 rollback·재실행·동시 초기화, URI 읽기 전용, timeout·row_factory·PRAGMA·스레드 옵션, 연결 종료, 브리핑 첫 작성자·갱신·사용자별 읽음 경계를 검증한다. 기존 대화·인증·알림·Notion·Mantis 테스트가 사용자 격리, stale 저장 방지, 충돌, 삭제/undo, snapshot 세대와 실패 처리를 계속 담당한다.
