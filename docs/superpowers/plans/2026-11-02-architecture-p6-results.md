# P6 DB 소유권·SQLite 정책 정리 결과

## 범위와 기준선

P6를 완료했다. 선행 미커밋 변경을 포함한 코드·테스트·설정·문서 611개 파일과 해시를 `assistant_web/runtime/architecture-p6/`에 보관했다. 변경 전 전체 테스트는 2,768개, 커버리지는 90%였다. `.env`나 운영 DB는 복사하거나 수정하지 않았다.

AGENTS.md에 따라 메인 에이전트가 구현·검토했다. 기존 작업 공간에서 선행 변경을 보존했으며 P7은 시작하지 않았다. 커밋·서버 재시작·배포는 수행하지 않았다.

DB 파일·스키마 소유자·버전·소비자·잠금·연결 정책의 상세 표는 [SQLite 소유권 지도](2026-11-02-sqlite-ownership.md)에 있다.

## 변경 내용

- `storage/sqlite.py`가 SQLite 연결 생성을 소유하고, `storage/profiles.py`가 DB별 기존 옵션을 선언한다. backend와 운영/검증 스크립트의 연결 79곳을 helper로 이전했다. 직접 `sqlite3.connect`를 호출하는 제품 코드 위치는 이 helper 한 곳이다.
- timeout, URI/mode, row_factory, check_same_thread, isolation_level, autocommit을 기존 값으로 유지했다. body index의 30초 대기·WAL, Mantis index 임시 구축의 OFF 설정 등을 다른 DB에 확대하지 않았다.
- 기존 closing/with connection/BEGIN/BEGIN IMMEDIATE, commit/rollback, 파일 권한과 원자적 교체 경계는 그대로 둔다. 프로필 설정 실패 때는 반환되지 않은 연결을 닫는다.
- workspace의 경로·5개 migration은 `workspace_schema.py`, 팀 브리핑 읽기·저장·읽음은 `workspace_repository.py`, 이슈 행 변환은 `workspace_rows.py`로 옮겼다. 공통 시간 함수는 `utils/dates.py`에 둔다.
- 메시지 검증·행 직렬화는 `conversation_rows.py`로 분리했다. schema가 conversation_history를 다시 import해 새 순환을 만들지 않도록 하기 위해서다. 메시지 migration과 알림 schema도 workspace schema 소유자로 모았다.
- 기존 workspace·conversation_history·workspace_notifications·team_brief의 import 경로는 정적 재노출로 유지했다. 새 patch 탐색기나 모듈 바꿔치기는 없다.
- 팀 브리핑은 보고서 생성·캐시 판단을 유지하면서 저장소를 직접 사용한다. workspace의 팀 브리핑 소비는 그대로 두어 의존 방향이 한쪽으로 흐르게 했다.
- auth/audit 및 Notion/Mantis의 별도 schema 체계는 기존 소유자에 남겼다. 공용 migration runner에 새 DB를 편입하지 않았으며 schema 번호·SQL·user_version·데이터를 변경하지 않았다. 외부 Chroma schema는 건드리지 않았다.

## 변경 전후 비교와 검토

| 검증 | 결과 |
|---|---|
| 기존 고정 문항 317개 | 조건·계획·출처·답변·검증 결과 317/317 동일 |
| 고정 문항의 모델 제안 61개 / 결정형 답변 269개 | 저장된 입력과 같은 검증·응답 결과 유지 |
| 빈 DB·구버전 DB 14개 | users v0–3, audit v0–3, workspace v0–5의 최신 schema·전체 행·user_version·0600 권한 동일 |
| migration 재실행 | 각 14개 사례의 두 번째 초기화 결과 동일 |
| 연결 75곳 + 이동한 브리핑 4곳 | 경로·timeout·uri·스레드 옵션 보존; 브리핑은 기존 기본값과 읽기 Row 유지 |
| backend SQL 리터럴 272개 | 이동·들여쓰기에 따른 공백 정규화 후 다중집합 동일 |

DB 기준선은 합성 사용자·대화·감사·브리핑·지식 행을 넣은 임시 SQLite 파일에서 생성했다. 운영 DB를 열지 않았다. `tests/fixtures/storage_migrations_golden.json`을 상시 회귀 테스트에 연결했다.

새 계약 테스트 12개가 전체 SCC 0과 연결 생성 위치·하위 저장소 의존 방향·기존 함수 객체를 검사한다. 실제 임시 DB로 아래 경계를 검증한다.

- 기본 연결은 자동 commit/close를 하지 않으며 파일 권한을 바꾸지 않는다. 호출자 with/closing은 기존대로 성공 commit·실패 rollback·close를 수행한다.
- ro URI의 쓰기 거절, 5/30초 대기, Row, foreign_keys·FULL, Mantis 스트림의 스레드 옵션을 보존한다.
- 연결 프로필 설정 실패, 조회 종료, 저장 SQL 실패 후 연결을 사용할 수 없을 만큼 닫혔는지 확인한다.
- migration의 DDL·행·버전 rollback, 실패 뒤 재시작, 비멱등 migration의 동시 시작 4개를 검증한다. 기존 test_db_migrations의 최신 버전 거절도 유지한다.
- 브리핑 첫 작성자 보존, 명시적 refresh 교체, 사용자별 읽음 격리를 확인한다.
- 기존 인증·대화·일괄 삭제·알림·Notion·Mantis 테스트로 사용자 격리, stale 저장의 부활 차단, optimistic conflict, 삭제/undo와 원본·파생 색인의 버전·실패 처리를 확인한다.

원본 결과와 검토 기록은 `runtime/architecture-p6/`의 `before/after-questions.json`, `before/after-storage.json`, `connection-inventory.json`, `connection-migration.json`, `storage-review.json`에 있다.

317/317은 새 정답률이 아니라 변경 전후 결과 일치율이다. 실제 모델 API 요청은 0회이며 Luna/Gemini의 새 답변 품질이나 네트워크 응답 시간은 이번 검증 대상에 포함하지 않았다.

## 구조와 전체 검증

- 전체 Python 런타임 SCC **1→0**, import 시점 SCC **0 유지**.
- `team_brief↔workspace` 순환을 해소했다. 구조 목록에서 해당 순환 간선 예외 2건만 제거했고 새 예외는 추가하지 않았다.
- 최종 `assistant_web/scripts/check.sh` 종료 코드 **0**.
- 테스트 **2,780개 통과**, 커버리지 **90%**.
- compile, TypeScript·Bklit 빌드 확인, shell, ruff 통과.
- mypy **249개 소스 파일** 통과.
- 보안 audit 통과: Python 기존 예외 **5건** 유지, npm **0건**. 예외 정책·기한과 잠금 파일을 바꾸지 않았다.

중간 검증에서 새 schema 모듈의 RUNTIME_DIR import 위치를 실제 소유자인 settings.paths로 바로잡았다. mypy의 SQLite stub이 Python 3.12의 빈 isolation_level과 -1 autocommit 상수를 누락해 인수 두 곳에만 타입 예외를 두고 실제 속성을 테스트했다. 연결 종료 테스트의 실패 주입은 INSERT OR IGNORE가 NOT NULL 위반을 무시하는 기존 동작을 고려해 일반 upsert 경로로 조정했다. 서비스 SQL을 바꿔 테스트를 맞추지 않았다.

최종 전체 검사 로그는 `runtime/architecture-p6/full-check.log`다. 코드 변경만 되돌릴 수 있으며 DB 삭제나 migration 되감기는 필요하지 않다.

## 다음 범위

다음은 P7-A: 문자열을 변형하는 프런트 테스트를 실제 ES 모듈 연결 검사로 이전하는 작업이다. 이번 P6에서는 프런트 코드·상태 구조를 수정하지 않았다.
