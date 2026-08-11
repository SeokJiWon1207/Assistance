# 아키텍처 분리 및 스냅샷 인덱스 계획

## 목표

현재 동작과 API 계약을 유지하면서 대형 모듈을 기능 경계로 나누고, 요청마다 Chroma 내부 테이블을 전건 순회하는 경로를 구조화된 보조 인덱스로 교체한다.

현재 기준은 다음과 같다.

| 대상 | 규모 | 공개 의존 범위 |
| --- | ---: | ---: |
| `backend/static/app.js` | 4,156줄 | 브라우저 전체 UI |
| `backend/understanding.py` | 1,990줄 / 함수 62개 | 백엔드·테스트 14개 파일 |
| `backend/sources/notion.py` | 2,194줄 / 함수 81개 | 백엔드·테스트 7개 파일 |
| `backend/reports/evaluation.py` | 1,334줄 / 함수 16개 | 백엔드·테스트 4개 파일 |
| Chroma 스냅샷 직접 참조 | 54개 지점 | 검색·집계·신선도·재구축 |

## 진행 상태 (2026-08-11)

- 1단계 계약 테스트를 추가해 공개 심볼, query plan, Notion 응답, 프런트엔드 DOM/API 계약을 고정했다.
- 2~4단계는 기존 import와 monkeypatch 계약을 유지하는 호환 파사드와 기능별 패키지를 추가했다. 기존 구현은 각 패키지의 `core.py`에 보존했으며, 기능별 모듈로 실제 구현을 옮기는 2차 분리가 남아 있다.
- 5단계는 `app.js`를 ES 모듈 진입점으로 전환하고 기능별 모듈 경계를 추가했다. 기존 UI 구현은 `modules/bootstrap.js`에 보존했으며, 각 기능 구현을 해당 모듈로 옮기는 2차 분리가 남아 있다.
- 6단계 구조화 스냅샷 인덱스 생성·검증·shadow read를 구현했다. 배포 기본값은 `shadow`이며, 기존 Chroma 결과를 반환하면서 보조 인덱스 결과가 다를 때 경고 로그를 남긴다.
- 실제 31,004건 스냅샷의 건수·문서 해시·최대 ID·날짜 범위·`PRAGMA quick_check` 검증을 통과했다. 운영 표본 4종은 기존 결과와 100% 일치했고 6.4~698.4배 빨랐다.
- 10만 건 결정형 fixture의 집계 p95는 기존 0.7589초, 구조화 인덱스 0.0915초로 8.3배 개선됐다.
- 2026-08-11 전체 갱신에서 31,006건을 재검증하고 갱신 전후 대표 조건이 모두 일치해 운영을 `primary`로 전환했다. 인덱스 누락을 강제한 운영 폴백도 동일한 결과를 반환했다.
- 증분 갱신도 임시 스냅샷 복제본에서 Chroma와 보조 인덱스를 함께 갱신·검증한 뒤 디렉터리를 원자 교체한다.

따라서 구조화 인덱스의 생성·shadow 관찰·기본 전환·폴백·원자 갱신은 완료됐다. 다음 작업은 `core.py`·`bootstrap.js`의 구현을 기능별 모듈로 이동하는 것이다.

## 변경 원칙

1. 기존 import 경로는 각 단계가 끝날 때까지 파사드에서 재노출한다.
2. 코드 이동과 동작 변경을 같은 커밋에 섞지 않는다.
3. 각 단계는 263개 결정형 테스트, mypy, Ruff, 커버리지 68% 기준을 통과해야 한다.
4. 새 인덱스 결과는 기존 전건 순회 결과와 shadow 비교한 뒤 기본 경로로 전환한다.
5. 배포 실패 시 이전 릴리스의 코드와 스냅샷 디렉터리로 함께 롤백한다.

## 실행 순서

### 1. 계약 고정

- `understanding`, `notion`, `evaluation`의 외부 import 심볼 목록을 테스트로 고정한다.
- 대표 질문 fixture에 대해 query plan, source stage, 최종 응답의 golden 결과를 추가한다.
- `app.js`의 전역 함수·DOM 이벤트 계약을 브라우저 독립 테스트로 고정한다.

완료 조건: 코드 이동 없이 계약 테스트만 추가되고 기존 결과와 완전히 일치한다.

### 2. `understanding.py` 분리

새 패키지는 다음 경계를 사용한다.

- `backend/intent/entities.py`: 고객사·플랫폼·담당자·상태 추출
- `backend/intent/dates.py`: 절대·상대 날짜 해석
- `backend/intent/constraints.py`: 필터 절 병합과 검증
- `backend/intent/context.py`: 대화 상속과 서수·지시어 참조
- `backend/intent/matching.py`: 문서·이슈 제약 매칭

기존 `backend/understanding.py`는 위 모듈을 재노출하는 파사드로 유지한다. 호출부 import 변경은 다음 커밋에서 기능군별로 수행한다.

완료 조건: 파사드가 250줄 이하이고 공개 심볼 및 query plan 출력이 동일하다.

### 3. `sources/notion.py` 분리

- `backend/notion/client.py`: HTTP, 재시도, TTL 캐시
- `backend/notion/schema.py`: DB 스키마·속성 탐색
- `backend/notion/mapping.py`: Notion 속성과 내부 값 변환
- `backend/notion/search.py`: 페이지 조회·필터·정규화
- `backend/notion/write.py`: 생성·수정·허용 DB 검증
- `backend/notion/formatting.py`: 결정형 응답 포맷

`backend/sources/notion.py`는 호환 파사드로 남긴다. 보안 정책 함수는 `write.py` 한 곳을 통과하게 한다.

완료 조건: 읽기/쓰기 보안 테스트가 유지되고 파사드가 200줄 이하이다.

### 4. `reports/evaluation.py` 분리

- `backend/evaluation/verification.py`: 검색·근거 검증
- `backend/evaluation/comparison.py`: A/B 비교
- `backend/evaluation/cases.py`: 회귀 케이스 실행
- `backend/evaluation/analytics.py`: 정확도 추세·실패 집계
- `backend/evaluation/finalization.py`: 감사 로그와 최종 응답 조립

완료 조건: 감사 로그 스키마와 API 응답이 동일하고 파사드가 150줄 이하이다.

### 5. 프런트엔드 분리

`app.js`는 초기화만 담당하고 아래 ES 모듈을 로드한다.

- `static/modules/api.js`: CSRF·오류·429/Retry-After 처리
- `static/modules/auth.js`: 로그인·로그아웃·권한 UI
- `static/modules/chat.js`: 스트리밍·취소·메시지 렌더링
- `static/modules/workspace.js`: 대화·즐겨찾기·개인 화면
- `static/modules/notion.js`: Notion 조회·수정 UI
- `static/modules/admin.js`: 사용자·정확도·감사 화면

완료 조건: `app.js`가 300줄 이하이고 스트리밍·인증·workspace DOM 테스트가 모두 유지된다.

### 6. 구조화 스냅샷 인덱스 추가

Chroma가 관리하는 `chroma.sqlite3`에 애플리케이션 테이블을 추가하지 않는다. 같은 버전의 스냅샷 디렉터리에 `mantis_index.sqlite3` 보조 DB를 생성한다.

핵심 스키마:

```sql
CREATE TABLE issues (
  issue_id INTEGER PRIMARY KEY,
  created_at TEXT,
  updated_at TEXT,
  status TEXT,
  handler TEXT,
  reporter TEXT,
  customer TEXT,
  platform TEXT,
  title TEXT NOT NULL,
  document TEXT NOT NULL,
  document_hash TEXT NOT NULL
);
CREATE INDEX idx_issues_created_at ON issues(created_at);
CREATE INDEX idx_issues_status ON issues(status);
CREATE INDEX idx_issues_customer ON issues(customer);
CREATE INDEX idx_issues_platform ON issues(platform);
CREATE INDEX idx_issues_handler ON issues(handler);
```

재구축 흐름:

1. 임시 디렉터리에 Chroma DB와 보조 인덱스를 함께 생성한다.
2. 이슈 수, 최대 ID, 날짜 범위, 문서 해시, `PRAGMA quick_check`를 검증한다.
3. 기존 전건 순회 결과와 count/group/filter 결과를 shadow 비교한다.
4. 두 DB를 포함한 스냅샷 디렉터리를 한 번에 원자 교체한다.
5. `MANTIS_INDEX_DB_PATH`가 없거나 검증에 실패하면 기존 Chroma 순회로 폴백한다.

완료 조건:

- fixture 및 운영 표본에서 기존 결과와 100% 일치
- count/filter 요청의 SQLite 조회가 인덱스를 사용함을 `EXPLAIN QUERY PLAN` 테스트로 확인
- 10만 건 fixture 기준 집계 p95가 기존 대비 5배 이상 개선
- 신규 인덱스가 없어도 이전 릴리스가 정상 동작

## 커밋 및 배포 단위

1. 계약 테스트
2. intent 모듈 이동
3. Notion 모듈 이동
4. evaluation 모듈 이동
5. 프런트엔드 모듈 이동
6. 보조 인덱스 생성 및 shadow read
7. 보조 인덱스 기본 전환

각 단위는 독립적으로 되돌릴 수 있어야 하며, 6번과 7번 사이에는 최소 한 번의 운영 스냅샷 재구축 및 결과 비교 기간을 둔다.
